from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from backtest.signals import SignalProposal
from backtest.live.contracts import BrokerSnapshot, InstrumentContract, LiveRiskPolicy
from backtest.live.risk_guard import RiskGuard, RiskGuardError


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def contract() -> InstrumentContract:
    return InstrumentContract(
        "XM_MT5_US100CASH_SUPER1", "index", "MT5", "XMGlobal-MT5 2", "US100Cash",
        "America/New_York", "USD", "USD", "USD", 2, 0.01, 0.01, 1.0,
        0.01, 100.0, 0.01, 0.01, "price", 0,
    )


def proposal() -> SignalProposal:
    return SignalProposal(
        "p-1", "candidate-hash", "XM_MT5_US100CASH_SUPER1", "long",
        100.0, 99.0, 103.0, NOW - timedelta(seconds=1), NOW + timedelta(seconds=20), "evidence-hash",
    )


def snapshot(**updates: object) -> BrokerSnapshot:
    def profit(action, symbol, volume, entry, stop):
        return -(abs(float(entry) - float(stop)) * float(volume) * 10.0)

    def margin(action, symbol, volume, entry):
        return float(volume) * 100.0

    value = BrokerSnapshot(
        as_of=NOW, equity=10_000.0, margin_free=5_000.0,
        daily_realized_r=0.0, strategy_health="ACTIVE", approval=True,
        halt=False, instrument_contract=contract(), total_stop_risk=0.0,
        snapshot_hash="snapshot-hash", order_calc_profit=profit, order_calc_margin=margin,
    )
    return replace(value, **updates)


def test_guard_approves_and_creates_order_only_after_live_facts_are_checked() -> None:
    guard = RiskGuard(clock=lambda: NOW)
    order = guard.approve(proposal(), snapshot(), approval_id="approval-1")
    assert order.proposal.proposal_id == "p-1"
    assert order.volume > 0 and order.persisted is False and len(order.request_hash) == 64


def test_guard_denies_stale_snapshot_halt_and_nonfinite_inputs() -> None:
    guard = RiskGuard(clock=lambda: NOW)
    assert not guard.evaluate(proposal(), snapshot(as_of=NOW - timedelta(seconds=61))).approved
    assert not guard.evaluate(proposal(), snapshot(halt=True)).approved
    assert not guard.evaluate(proposal(), snapshot(equity=float("nan"))).approved


def test_guard_denies_missing_approval_health_contract_or_budget() -> None:
    guard = RiskGuard(clock=lambda: NOW)
    assert not guard.evaluate(proposal(), snapshot(approval=False)).approved
    assert not guard.evaluate(proposal(), snapshot(strategy_health="DECAYED")).approved
    assert not guard.evaluate(proposal(), snapshot(instrument_contract=None)).approved
    assert not guard.evaluate(proposal(), snapshot(daily_realized_r=-1.0)).approved
    assert not guard.evaluate(proposal(), snapshot(snapshot_hash="")).approved
    assert not guard.evaluate(proposal(), snapshot(), approval_id="").approved


def policy() -> LiveRiskPolicy:
    return LiveRiskPolicy(
        -1.0,
        (("XM_MT5_US100CASH_SUPER1", 1), ("XM_MT5_US500CASH_SUPER1", 2)),
        3,
        (("XM_MT5_US100CASH_SUPER1", "US100Cash"), ("XM_MT5_US500CASH_SUPER1", "US500Cash")),
        1,
        2,
        300,
        (("XM_MT5_US100CASH_SUPER1", 100.0), ("XM_MT5_US500CASH_SUPER1", 100.0)),
    )


def policy_snapshot(**updates: object) -> BrokerSnapshot:
    signed = policy()
    values = {
        "total_entry_count": 0, "entry_counts_by_instrument": {},
        "open_position_counts_by_instrument": {}, "pending_order_counts_by_instrument": {},
        "policy_hash": signed.policy_hash, "instrument_contract_hash": contract().contract_hash,
        "leverage": 2.0, "pair_exposure_percent": 0.0, "concentration_percent": 0.0,
        **updates,
    }
    return snapshot(**values)


def test_v3_policy_enforces_broker_trade_counts_cooldown_and_exact_hashes() -> None:
    guard = RiskGuard(policy=policy(), clock=lambda: NOW)
    assert guard.evaluate(proposal(), policy_snapshot(), approval_id="approval-1").approved
    assert not guard.evaluate(proposal(), policy_snapshot(total_entry_count=3), approval_id="approval-1").approved
    assert not guard.evaluate(
        proposal(),
        policy_snapshot(entry_counts_by_instrument={"XM_MT5_US100CASH_SUPER1": 1}),
        approval_id="approval-1",
    ).approved
    assert not guard.evaluate(
        proposal(), policy_snapshot(last_accepted_entry_at=NOW - timedelta(seconds=299)), approval_id="approval-1"
    ).approved
    assert not guard.evaluate(proposal(), policy_snapshot(policy_hash="bad"), approval_id="approval-1").approved
