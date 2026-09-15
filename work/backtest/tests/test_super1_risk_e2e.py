from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtest.live.approval import ApprovalStore, wire_request_hash
from backtest.live.audit_ledger import AuditLedger
from backtest.live.broker_facts import BrokerFactsBuilder, BrokerFactsError
from backtest.live.contracts import BrokerSnapshot, BrokerEvidence, InstrumentContract, LiveRiskPolicy
from backtest.live.halt import HaltController
from backtest.live.instruments import InstrumentRegistry
from backtest.live.order_state import OrderStateMachine
from backtest.live.production_flow import ProductionDependencies, ProductionFlowError, ProductionOrderFlow
from backtest.live.risk_guard import RiskGuard
from backtest.live.settings import RuntimeSettings
from backtest.live.strategy_health import LockedOOSBaseline, StrategyHealth
from scripts.mt5_read_only_acceptance import (
    ReadOnlyAcceptanceError,
    WRITE_API_NAMES,
    install_read_only_traps,
    run_read_only_acceptance,
)
from backtest.signals import SignalProposal


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "[REDACTED"
CANDIDATE = "c" * 64


def _contract(instrument_id: str = "nq", symbol: str = "US100Cash") -> InstrumentContract:
    return InstrumentContract(
        instrument_id, "index", "MT5", "XM server", symbol, "America/New_York",
        "USD", "USD", "USD", 2, 0.01, 0.01, 1.0, 0.01, 100.0, 0.01, 0.01, "price", 0,
    )


def _policy() -> LiveRiskPolicy:
    return LiveRiskPolicy(
        -1.0, (("nq", 1), ("spx", 2)), 3,
        (("nq", "US100Cash"), ("spx", "US500Cash")), 1, 2, 300,
        (("nq", 100.0), ("spx", 100.0)),
    )


def _proposal(name: str = "proposal") -> SignalProposal:
    return SignalProposal(
        name, CANDIDATE, "nq", "long", 100.0, 99.0, 103.0,
        NOW - timedelta(seconds=1), NOW + timedelta(minutes=5), "e" * 64,
    )


def _builder(*, deals=(), positions=(), history_missing: bool = False, resolver=None) -> BrokerFactsBuilder:
    def read(operation, *_args, **_kwargs):
        if operation == "history_deals_get":
            return None if history_missing else tuple(deals)
        return {
            "account_info": {"login": ACCOUNT, "equity": 10_000.0, "margin_free": 5_000.0, "leverage": 2.0},
            "positions_get": tuple(positions), "orders_get": (),
        }[operation]

    contracts = {"US100Cash": _contract(), "US500Cash": _contract("spx", "US500Cash")}
    return BrokerFactsBuilder(
        read=read,
        order_calc_profit=lambda _action, _symbol, volume, entry, stop: -abs(entry - stop) * volume * 10.0,
        order_calc_margin=lambda _action, _symbol, volume, _entry: volume * 100.0,
        halt_reader=lambda: False,
        strategy_health_reader=lambda: "ACTIVE",
        starting_risk_reader=lambda position_id: {"p-1": 100.0}.get(position_id),
        strategy_matcher=lambda _row: True,
        whitelist_instrument_ids=("nq", "spx"),
        contract_resolver=resolver or (lambda symbol: contracts[symbol]),
    )


def _snapshot(builder: BrokerFactsBuilder) -> BrokerSnapshot:
    return builder.build(
        now=NOW, contract=_contract(), candidate_hash=CANDIDATE,
        deals_start=NOW - timedelta(days=2), deals_end=NOW,
        approval=True, policy_hash=_policy().policy_hash,
    )


class _NoSendAdapter:
    def __init__(self) -> None:
        self.send_calls = 0

    def request_for_order(self, _order):
        return {"symbol": "US100Cash"}

    def validate_request(self, _order) -> None:
        return None

    def send(self, _order):
        self.send_calls += 1
        raise AssertionError("negative risk case reached order_send")

    def reconcile(self, _result, _order):
        return BrokerEvidence("RECONCILE", 10009, ticket=1)


def _flow(tmp_path: Path, adapter: _NoSendAdapter, *, clock=None) -> ProductionOrderFlow:
    contract = _contract()
    baseline = LockedOOSBaseline(CANDIDATE, 100, 100, (2022, 2023, 2024), 0.0, 1.0, 2.0, 7)
    health = StrategyHealth("ACTIVE", baseline=baseline, expected_candidate_hash=CANDIDATE)
    store = ApprovalStore(tmp_path / "approvals.sqlite3", now=lambda: NOW)
    ledger = AuditLedger(tmp_path / "audit.sqlite3", campaign_id="campaign", account_key=ACCOUNT)
    return ProductionOrderFlow(ProductionDependencies(
        risk_guard=RiskGuard(policy=_policy(), clock=clock or (lambda: NOW)),
        order_state=OrderStateMachine(), audit_ledger=ledger,
        halt_controller=HaltController(tmp_path / "runtime"),
        instrument_registry=InstrumentRegistry([contract, _contract("spx", "US500Cash")]),
        approval_store=store, runtime_settings=RuntimeSettings("DEMO_ORDER"),
        strategy_health=health, execution_adapter=adapter,
    ))


def _approval_for(flow: ProductionOrderFlow, proposal: SignalProposal) -> str:
    staged = {
        "proposal_id": proposal.proposal_id, "candidate_hash": proposal.candidate_hash,
        "instrument_id": proposal.instrument_id, "direction": proposal.direction,
        "entry_price": proposal.entry_price, "stop_price": proposal.stop_price,
        "target_price": proposal.target_price, "decision_time": proposal.decision_time.isoformat(),
        "expires_at": proposal.expires_at.isoformat(), "evidence_hash": proposal.evidence_hash,
        "wire_request": {"symbol": "US100Cash"},
    }
    staged["wire_request_hash"] = wire_request_hash(staged["wire_request"])
    store = flow.dependencies.approval_store
    store.stage(staged, campaign_id="campaign", account_key=ACCOUNT, release_id="release", candidate_hash=CANDIDATE)
    approval = store.approve(
        proposal.proposal_id,
        lease={
            "state": "ACTIVE", "lease_id": "lease", "invocation_nonce": "nonce",
            "runner_sid": "S-1-5-18", "authorized_operator_sid": "S-1-5-19",
            "campaign_id": "campaign", "account": ACCOUNT, "release_id": "release",
        },
        operator_sid="S-1-5-19", release_id="release", candidate_hash=CANDIDATE, now=NOW,
    )
    return approval.approval_id


def _assert_no_send(snapshot_provider, tmp_path: Path, name: str = "proposal", expected_reason: str | None = None, *, clock=None) -> None:
    adapter = _NoSendAdapter()
    flow = _flow(tmp_path, adapter, clock=clock)
    proposal = _proposal(name)
    approval_id = _approval_for(flow, proposal)
    with pytest.raises((BrokerFactsError, ProductionFlowError)) as caught:
        flow.send(
            proposal, snapshot_provider=snapshot_provider,
            approval_id=approval_id, campaign_id="campaign",
            account_key=ACCOUNT, release_id="release", candidate_hash=CANDIDATE,
        )
    if expected_reason is not None:
        assert str(caught.value) == expected_reason
    assert adapter.send_calls == 0


def test_production_flow_partial_close_costs_and_daily_loss_are_no_send(tmp_path: Path) -> None:
    deal = {
        "deal_id": "partial-1", "position_id": "p-1", "entry": "OUT", "symbol": "US100Cash",
        "magic": 999, "time_msc": int((NOW - timedelta(minutes=1)).timestamp() * 1000),
        "profit": -90.0, "commission": -5.0, "swap": -4.0, "fee": -2.0,
    }
    position = {"symbol": "US100Cash", "type": 0, "volume_current": 0.5, "price_open": 100.0, "sl": 99.0}
    snapshot = _snapshot(_builder(deals=(deal,), positions=(position,)))
    assert snapshot.daily_realized_r == pytest.approx(-1.01)
    _assert_no_send(lambda: snapshot, tmp_path, expected_reason="open-position limit is active")


def test_production_flow_history_entry_drives_cooldown_and_is_no_send(tmp_path: Path) -> None:
    deal = {
        "deal_id": "foreign-entry", "order": "foreign-order", "position_id": "foreign-position",
        "entry": "IN", "symbol": "US500Cash", "magic": 777,
        "time": int((NOW - timedelta(seconds=299)).timestamp()),
    }
    snapshot = _snapshot(_builder(deals=(deal,)))
    assert snapshot.last_accepted_entry_at == NOW - timedelta(seconds=299)
    _assert_no_send(lambda: snapshot, tmp_path, expected_reason="entry cooldown is active")


def test_production_flow_stale_snapshot_is_no_send(tmp_path: Path) -> None:
    snapshot = _snapshot(_builder())
    stale = replace(snapshot, as_of=NOW - timedelta(seconds=61), broker_query_id="stale-query")
    _assert_no_send(lambda: stale, tmp_path, expected_reason="broker snapshot is stale")


def test_production_flow_missing_full_history_is_no_send(tmp_path: Path) -> None:
    builder = _builder(history_missing=True)
    _assert_no_send(lambda: _snapshot(builder), tmp_path, expected_reason="broker history_deals_get returned unknown state")


def test_unconvertible_historical_close_is_no_send(tmp_path: Path) -> None:
    deal = {
        "deal_id": "bad-close", "position_id": "p-1", "entry": "OUT", "symbol": "US100Cash",
        "time": int((NOW - timedelta(days=2)).timestamp()), "profit": -10.0,
        "commission": None, "swap": 0.0, "fee": 0.0,
    }
    _assert_no_send(lambda: _snapshot(_builder(deals=(deal,))), tmp_path, expected_reason="broker deal bad-close cannot be converted to R: commission")


def test_two_campaigns_see_account_wide_daily_cap_and_both_are_no_send(tmp_path: Path) -> None:
    deals = tuple({
        "deal_id": f"entry-{index}", "order": f"order-{index}", "position_id": f"position-{index}",
        "entry": "IN", "symbol": "US100Cash", "magic": 1000 + index,
        "time": int((NOW - timedelta(minutes=index + 1)).timestamp()),
    } for index in range(3))
    builder = _builder(deals=deals)
    snapshot = _snapshot(builder)
    assert snapshot.total_entry_count == 3
    adapters = [_NoSendAdapter(), _NoSendAdapter()]

    def run(index: int) -> str:
        flow = _flow(tmp_path / str(index), adapters[index])
        proposal = _proposal(f"campaign-{index}")
        approval_id = _approval_for(flow, proposal)
        try:
            flow.send(
                proposal, snapshot_provider=lambda: snapshot,
                approval_id=approval_id, campaign_id="campaign-{index}",
                account_key=ACCOUNT, release_id="release", candidate_hash=CANDIDATE,
            )
        except ProductionFlowError as exc:
            assert str(exc) == "daily broker trade-count limit is active"
            return "NO_SEND"
        return "SENT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(run, (0, 1)))
    assert outcomes == ["NO_SEND", "NO_SEND"]
    assert all(adapter.send_calls == 0 for adapter in adapters)


def _fresh_second(first: BrokerSnapshot, **changes: object) -> BrokerSnapshot:
    assert first.query_completed_at is not None
    started = first.query_completed_at + timedelta(microseconds=1)
    values: dict[str, object] = {
        "broker_query_id": "approval-final-query",
        "query_started_at": started,
        "query_completed_at": started + timedelta(microseconds=1),
        "broker_query_sequence": int(first.broker_query_sequence or 0) + 1,
    }
    values.update(changes)
    return replace(first, **values)


def test_second_snapshot_minus_one_r_is_no_send(tmp_path: Path) -> None:
    first = _snapshot(_builder())
    second = _fresh_second(first, daily_realized_r=-1.0)
    _assert_no_send(iter((first, second)).__next__, tmp_path, expected_reason="pair daily loss cap is active")


def test_second_snapshot_partial_close_costs_pushes_loss_to_minus_one_r(tmp_path: Path) -> None:
    first = _snapshot(_builder())
    partial_close = {
        "deal_id": "partial-after-approval", "position_id": "p-1", "entry": "OUT",
        "symbol": "US100Cash", "magic": 999,
        "time_msc": int((NOW - timedelta(minutes=1)).timestamp() * 1000),
        "profit": -90.0, "commission": -5.0, "swap": -4.0, "fee": -2.0,
    }
    second = _fresh_second(first, daily_realized_r=-1.01, deals=(partial_close,))
    _assert_no_send(iter((first, second)).__next__, tmp_path, expected_reason="pair daily loss cap is active")


def test_second_snapshot_cooldown_is_no_send(tmp_path: Path) -> None:
    first = _snapshot(_builder())
    second = _fresh_second(first, last_accepted_entry_at=NOW - timedelta(seconds=299))
    _assert_no_send(iter((first, second)).__next__, tmp_path, expected_reason="entry cooldown is active")


def test_second_snapshot_daily_count_is_no_send(tmp_path: Path) -> None:
    first = _snapshot(_builder())
    second = _fresh_second(
        first,
        total_entry_count=3,
        entry_counts_by_instrument={"nq": 1, "spx": 2},
    )
    _assert_no_send(iter((first, second)).__next__, tmp_path, expected_reason="daily broker trade-count limit is active")


def test_second_snapshot_total_exposure_is_no_send(tmp_path: Path) -> None:
    first = _snapshot(_builder())
    second = _fresh_second(
        first,
        open_positions=(
            {"symbol": "US500Cash", "volume_current": 0.01, "price_open": 100.0, "sl": 99.0},
            {"symbol": "US500Cash", "volume_current": 0.01, "price_open": 100.0, "sl": 99.0},
        ),
        open_position_counts_by_instrument={"nq": 0, "spx": 2},
    )
    _assert_no_send(iter((first, second)).__next__, tmp_path, expected_reason="open-position limit is active")


def test_second_snapshot_stale_at_final_clock_is_no_send(tmp_path: Path) -> None:
    first = replace(
        _snapshot(_builder()),
        as_of=NOW,
        query_started_at=NOW - timedelta(seconds=4),
        query_completed_at=NOW - timedelta(seconds=3),
        broker_query_id="precheck-query",
        broker_query_sequence=1,
    )
    second = _fresh_second(
        first,
        query_started_at=NOW + timedelta(seconds=1),
        query_completed_at=NOW + timedelta(seconds=2),
    )
    clock_values = iter((NOW, NOW, NOW + timedelta(seconds=61)))
    _assert_no_send(
        iter((first, second)).__next__,
        tmp_path,
        expected_reason="broker snapshot is stale",
        clock=lambda: next(clock_values),
    )


def test_cached_second_query_is_fail_closed_and_no_send(tmp_path: Path) -> None:
    first = _snapshot(_builder())
    _assert_no_send(lambda: first, tmp_path, expected_reason="approval final check reused the prior broker query")


def test_second_query_same_sequence_is_fail_closed_and_no_send(tmp_path: Path) -> None:
    first = _snapshot(_builder())
    second = _fresh_second(first, broker_query_sequence=first.broker_query_sequence)
    _assert_no_send(iter((first, second)).__next__, tmp_path, expected_reason="approval final broker query sequence did not advance")


def test_second_query_started_before_approval_is_fail_closed_and_no_send(tmp_path: Path) -> None:
    first = replace(
        _snapshot(_builder()),
        as_of=NOW,
        query_started_at=NOW - timedelta(seconds=4),
        query_completed_at=NOW - timedelta(seconds=3),
        broker_query_id="precheck-query",
        broker_query_sequence=1,
    )
    second = _fresh_second(
        first,
        query_started_at=NOW - timedelta(seconds=1),
        query_completed_at=NOW,
    )
    _assert_no_send(iter((first, second)).__next__, tmp_path, expected_reason="final broker query started before approval validation")


class _ProcessExecutionAdapter:
    def request_for_order(self, _order):
        return {"symbol": "US100Cash"}

    def validate_request(self, _order) -> None:
        return None

    def send(self, _order):
        return SimpleNamespace(retcode=10009, order=1, deal=1)

    def reconcile(self, _result, _order):
        return BrokerEvidence("SEND", 10009, ticket=1, deal_id=1, broker_state="READBACK_CONFIRMED")


def _shared_approval(store: ApprovalStore, proposal: SignalProposal) -> str:
    request = {"symbol": "US100Cash"}
    staged = {
        "proposal_id": proposal.proposal_id, "candidate_hash": proposal.candidate_hash,
        "instrument_id": proposal.instrument_id, "direction": proposal.direction,
        "entry_price": proposal.entry_price, "stop_price": proposal.stop_price,
        "target_price": proposal.target_price, "decision_time": proposal.decision_time.isoformat(),
        "expires_at": proposal.expires_at.isoformat(), "evidence_hash": proposal.evidence_hash,
        "wire_request": request, "wire_request_hash": wire_request_hash(request),
    }
    store.stage(staged, campaign_id="campaign", account_key=ACCOUNT, release_id="release", candidate_hash=CANDIDATE)
    return store.approve(
        proposal.proposal_id,
        lease={
            "state": "ACTIVE", "lease_id": f"lease-{proposal.proposal_id}", "invocation_nonce": f"nonce-{proposal.proposal_id}",
            "runner_sid": "S-1-5-18", "authorized_operator_sid": "S-1-5-19",
            "campaign_id": "campaign", "account": ACCOUNT, "release_id": "release",
        },
        operator_sid="S-1-5-19", release_id="release", candidate_hash=CANDIDATE, now=NOW,
    ).approval_id


def _same_sqlite_process_worker(db_path: str, approval_id: str, name: str, barrier, result_queue) -> None:
    store = ApprovalStore(db_path, now=lambda: NOW)
    ledger = AuditLedger(db_path, campaign_id="campaign", account_key=ACCOUNT)
    try:
        contract = _contract()
        health = StrategyHealth(
            "ACTIVE",
            baseline=LockedOOSBaseline(CANDIDATE, 100, 100, (2022, 2023, 2024), 0.0, 1.0, 2.0, 7),
            expected_candidate_hash=CANDIDATE,
        )
        dependencies = ProductionDependencies(
            risk_guard=RiskGuard(policy=_policy(), clock=lambda: NOW),
            order_state=OrderStateMachine(), audit_ledger=ledger,
            halt_controller=HaltController(Path(db_path).parent / f"runtime-{name}"),
            instrument_registry=InstrumentRegistry([contract, _contract("spx", "US500Cash")]),
            approval_store=store, runtime_settings=RuntimeSettings("DEMO_ORDER"),
            strategy_health=health, execution_adapter=_ProcessExecutionAdapter(),
        )
        builder = _builder(deals=tuple({
            "deal_id": f"spx-entry-{index}", "order": f"spx-order-{index}",
            "position_id": f"spx-position-{index}", "entry": "IN", "symbol": "US500Cash",
            "magic": 2000 + index, "time": int((NOW - timedelta(minutes=(index + 1) * 10)).timestamp()),
        } for index in range(2)))
        first = _snapshot(builder)
        second = _snapshot(builder)
        barrier.wait(timeout=30)
        ProductionOrderFlow(dependencies).send(
            _proposal(name), snapshot_provider=iter((first, second)).__next__,
            approval_id=approval_id, campaign_id="campaign", account_key=ACCOUNT,
            release_id="release", candidate_hash=CANDIDATE,
        )
    except Exception as exc:
        result_queue.put((name, str(exc)))
    else:
        result_queue.put((name, "SENT"))
    finally:
        ledger.close()
        store.close()


def test_two_processes_same_account_same_sqlite_cannot_interleave_daily_slots(tmp_path: Path) -> None:
    db_path = tmp_path / "shared-orders.sqlite3"
    setup_store = ApprovalStore(db_path, now=lambda: NOW)
    setup_ledger = AuditLedger(db_path, campaign_id="campaign", account_key=ACCOUNT)
    approvals = [_shared_approval(setup_store, _proposal(f"process-{index}")) for index in range(2)]
    setup_ledger.close()
    setup_store.close()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(target=_same_sqlite_process_worker, args=(str(db_path), approval_id, f"process-{index}", barrier, result_queue))
        for index, approval_id in enumerate(approvals)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=45)
        assert process.exitcode == 0
    results = dict(result_queue.get(timeout=5) for _ in processes)
    assert sorted(results.values()) == ["SENT", "daily entry slot limit is active"]


class _ReadOnlyMt5Fixture:
    def __init__(self) -> None:
        self.write_calls = 0

    def account_info(self):
        return SimpleNamespace(login=ACCOUNT, server="XM-DEMO", company="XM")

    def positions_get(self):
        return ()

    def orders_get(self):
        return ()

    def history_deals_get(self, _start, _end):
        return ()

    def symbol_info(self, _symbol):
        return SimpleNamespace(name="US100Cash")

    def order_send(self, _request):
        self.write_calls += 1
        raise AssertionError("read-only acceptance reached a broker write")


def test_read_only_mt5_acceptance_uses_only_reads_and_redacts_identity() -> None:
    fixture = _ReadOnlyMt5Fixture()
    evidence = run_read_only_acceptance(fixture, symbol="US100Cash", now=NOW)
    rendered = repr(evidence)
    assert evidence["read_operations"] == ["account_info", "positions_get", "orders_get", "history_deals_get", "symbol_info"]
    assert evidence["order_send"] == 0
    assert fixture.write_calls == 0
    assert ACCOUNT not in rendered and "XM-DEMO" not in rendered and "company" not in rendered


def test_read_only_mt5_proxy_traps_every_declared_write_api() -> None:
    fixture = _ReadOnlyMt5Fixture()
    proxy = install_read_only_traps(fixture)
    for name in WRITE_API_NAMES:
        with pytest.raises(ReadOnlyAcceptanceError, match="not allowlisted"):
            getattr(proxy, name)
    with pytest.raises(ReadOnlyAcceptanceError, match="not allowlisted"):
        proxy.order_send({})
    assert fixture.write_calls == 0
