from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backtest.live.broker_facts import BrokerFactsBuilder, BrokerFactsError
from backtest.live.deal_ingestion import TerminalFactSnapshot
from backtest.live.contracts import InstrumentContract
from backtest.live.risk_guard import RiskGuard
from backtest.signals import SignalProposal


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _contract() -> InstrumentContract:
    return InstrumentContract(
        "i1", "index", "MT5", "server", "US100Cash", "UTC", "USD", "USD", "USD", 2,
        0.01, 0.01, 1.0, 0.1, 100.0, 0.1, 0.01, "price", 0,
    )


def _builder(*, positions=(), pending=(), deals=(), account=None) -> BrokerFactsBuilder:
    values = {
        "account_info": account or {"login": 1, "equity": 10_000.0, "margin_free": 5_000.0, "leverage": 2.0},
        "positions_get": tuple(positions),
        "orders_get": tuple(pending),
    }
    realized = sum(
        float(row.get("profit", 0)) + float(row.get("commission", 0)) + float(row.get("swap", 0)) + float(row.get("fee", 0))
        for row in deals
    ) / 100.0
    facts = TerminalFactSnapshot(
        NOW, "d" * 64, NOW, 1, 1, Decimal(str(realized)), len(deals), {"i1": len(deals)},
        tuple(str(row.get("deal_id")) for row in deals), tuple(deals),
    )
    return BrokerFactsBuilder(
        read=lambda operation, *_args, **_kwargs: values[operation],
        order_calc_profit=lambda _action, _symbol, volume, entry, stop: -(abs(entry - stop) * volume * 10.0),
        order_calc_margin=lambda _action, _symbol, volume, _entry: volume * 100.0,
        halt_reader=lambda: False,
        strategy_health_reader=lambda: "ACTIVE",
        terminal_fact_reader=lambda _now: facts,
        strategy_matcher=lambda row: row.get("magic") == 7,
    )


def _snapshot(builder: BrokerFactsBuilder):
    return builder.build(
        now=NOW, contract=_contract(), candidate_hash="c" * 64,
        approval=False,
    )


def test_broker_facts_derive_realized_r_from_partial_position_exits_and_costs() -> None:
    snapshot = _snapshot(_builder(deals=(
        {"deal_id": 1, "position_id": "p-1", "entry": 1, "magic": 7, "profit": 50.0, "commission": -2.0, "swap": -1.0, "fee": 0.0},
        {"deal_id": 2, "position_id": "p-1", "entry": "OUT", "magic": 7, "profit": 25.0, "commission": -1.0, "swap": 0.0, "fee": -0.5},
    )))
    assert snapshot.daily_realized_r == pytest.approx(0.705)
    assert snapshot.total_stop_risk == 0.0


def test_daily_loss_cap_is_evaluated_from_broker_facts() -> None:
    snapshot = _snapshot(_builder(deals=(
        {"deal_id": 1, "position_id": "p-1", "entry": 1, "magic": 7, "profit": -80.0, "commission": -15.0, "swap": -10.0, "fee": -1.0},
    )))
    proposal = SignalProposal("p", "c" * 64, "i1", "long", 100.0, 99.0, 101.0, NOW, NOW.replace(hour=13), "e" * 64)
    guard = RiskGuard(pair_cap_r=-1.0, daily_loss_cap_r=-0.5, clock=lambda: NOW)
    assert not guard.precheck(proposal, snapshot).approved


def test_sl_less_exposure_and_nan_fail_closed() -> None:
    with pytest.raises(BrokerFactsError, match="stop loss"):
        _snapshot(_builder(positions=({"symbol": "US100Cash", "type": 0, "volume": 1.0, "price_open": 100.0},)))
    with pytest.raises(BrokerFactsError, match="non-finite"):
        _snapshot(_builder(account={"login": 1, "equity": float("nan"), "margin_free": 5_000.0, "leverage": 2.0}))


def test_pending_orders_are_included_in_pair_exposure_and_concentration() -> None:
    pending = ({"symbol": "US100Cash", "type": 2, "volume_current": 2.0, "price_open": 100.0, "sl": 99.0},)
    snapshot = _snapshot(_builder(pending=pending))
    assert snapshot.pair_exposure_percent == pytest.approx(2.0)
    assert snapshot.concentration_percent == pytest.approx(100.0)


def test_builder_uses_canonical_terminal_snapshot_not_ad_hoc_history_query() -> None:
    observed: list[str] = []

    def read(operation, *args, **_kwargs):
        if operation == "history_deals_get":
            observed.append(operation)
            raise AssertionError("ad-hoc deal query is forbidden")
        return {
            "account_info": {"login": 1, "equity": 10_000.0, "margin_free": 5_000.0, "leverage": 2.0},
            "positions_get": (), "orders_get": (),
        }[operation]

    builder = BrokerFactsBuilder(
        read=read, order_calc_profit=lambda *_: 0.0, order_calc_margin=lambda *_: 0.0,
        halt_reader=lambda: False, strategy_health_reader=lambda: "ACTIVE",
        terminal_fact_reader=lambda _now: TerminalFactSnapshot(NOW, "d" * 64, NOW, 0, 0, Decimal(0), 0, {}, (), ()),
        strategy_matcher=lambda _row: True,
    )
    builder.build(
        now=NOW, contract=_contract(), candidate_hash="c" * 64,
    )
    assert observed == []
