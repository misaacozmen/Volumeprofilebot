from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtest.live.approval import ApprovalStore
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


def _flow(tmp_path: Path, adapter: _NoSendAdapter) -> ProductionOrderFlow:
    contract = _contract()
    baseline = LockedOOSBaseline(CANDIDATE, 100, 100, (2022, 2023, 2024), 0.0, 1.0, 2.0, 7)
    health = StrategyHealth("ACTIVE", baseline=baseline, expected_candidate_hash=CANDIDATE)
    store = ApprovalStore(tmp_path / "approvals.sqlite3", now=lambda: NOW)
    ledger = AuditLedger(tmp_path / "audit.sqlite3", campaign_id="campaign", account_key=ACCOUNT)
    return ProductionOrderFlow(ProductionDependencies(
        risk_guard=RiskGuard(policy=_policy(), clock=lambda: NOW),
        order_state=OrderStateMachine(), audit_ledger=ledger,
        halt_controller=HaltController(tmp_path / "runtime"),
        instrument_registry=InstrumentRegistry([contract, _contract("spx", "US500Cash")]),
        approval_store=store, runtime_settings=RuntimeSettings("DEMO_ORDER"),
        strategy_health=health, execution_adapter=adapter,
    ))


def _assert_no_send(snapshot_provider, tmp_path: Path, name: str = "proposal") -> None:
    adapter = _NoSendAdapter()
    flow = _flow(tmp_path, adapter)
    with pytest.raises((BrokerFactsError, ProductionFlowError)):
        flow.send(
            _proposal(name), snapshot_provider=snapshot_provider,
            approval_id="missing-approval", campaign_id="campaign",
            account_key=ACCOUNT, release_id="release", candidate_hash=CANDIDATE,
        )
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
    _assert_no_send(lambda: snapshot, tmp_path)


def test_production_flow_history_entry_drives_cooldown_and_is_no_send(tmp_path: Path) -> None:
    deal = {
        "deal_id": "foreign-entry", "order": "foreign-order", "position_id": "foreign-position",
        "entry": "IN", "symbol": "US500Cash", "magic": 777,
        "time": int((NOW - timedelta(seconds=299)).timestamp()),
    }
    snapshot = _snapshot(_builder(deals=(deal,)))
    assert snapshot.last_accepted_entry_at == NOW - timedelta(seconds=299)
    _assert_no_send(lambda: snapshot, tmp_path)


def test_production_flow_stale_snapshot_is_no_send(tmp_path: Path) -> None:
    snapshot = _snapshot(_builder())
    stale = replace(snapshot, as_of=NOW - timedelta(seconds=61), broker_query_id="stale-query")
    _assert_no_send(lambda: stale, tmp_path)


def test_production_flow_missing_full_history_is_no_send(tmp_path: Path) -> None:
    builder = _builder(history_missing=True)
    _assert_no_send(lambda: _snapshot(builder), tmp_path)


def test_unconvertible_historical_close_is_no_send(tmp_path: Path) -> None:
    deal = {
        "deal_id": "bad-close", "position_id": "p-1", "entry": "OUT", "symbol": "US100Cash",
        "time": int((NOW - timedelta(days=2)).timestamp()), "profit": -10.0,
        "commission": None, "swap": 0.0, "fee": 0.0,
    }
    _assert_no_send(lambda: _snapshot(_builder(deals=(deal,))), tmp_path)


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
        try:
            flow.send(
                _proposal(f"campaign-{index}"), snapshot_provider=lambda: snapshot,
                approval_id="missing-approval", campaign_id=f"campaign-{index}",
                account_key=ACCOUNT, release_id="release", candidate_hash=CANDIDATE,
            )
        except (BrokerFactsError, ProductionFlowError):
            return "NO_SEND"
        return "SENT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(run, (0, 1)))
    assert outcomes == ["NO_SEND", "NO_SEND"]
    assert all(adapter.send_calls == 0 for adapter in adapters)
