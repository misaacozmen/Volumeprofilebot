from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import threading
from pathlib import Path

import pandas as pd
import pytest

from backtest.evaluation_window import EvaluationWindow, EvaluationWindowError, classify_sessions
from backtest.live.approval import ApprovalError, ApprovalStore
from backtest.live.broker_facts import BrokerFactsBuilder, BrokerFactsError
from backtest.live.contracts import BrokerSnapshot, InstrumentContract, LiveRiskPolicy
from backtest.live.instruments import InstrumentContractError, InstrumentRegistry, validate_current_tick, validate_economic_semantics
from backtest.live.retry import AllowedTransportError, NonRetryableReadError, RetryPolicy, write_once
from backtest.live.risk_guard import RiskGuard
from backtest.live.settings import SettingsError, load_settings
from backtest.live.strategy_health import LockedOOSBaseline, StrategyHealth
from backtest.numeric_contracts import FinancialMathError, normalize_r, profit_factor, quantize_price, safe_divide
from backtest.optimization import StudyConfig, StudyConfigError, optimize
from backtest.risk_xray import build_risk_xray
from backtest.signals import SignalProposal


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def contract(instrument_id: str = "nq", symbol: str = "US100Cash") -> InstrumentContract:
    return InstrumentContract(
        instrument_id, "index", "MT5", "XM server", symbol, "America/New_York",
        "USD", "USD", "USD", 2, 0.01, 0.01, 1.0, 0.01, 100.0, 0.01, 0.01, "price", 0,
    )


def semantic_contract() -> InstrumentContract:
    return InstrumentContract(
        "nq", "index", "MT5", "XM server", "US100Cash", "America/New_York",
        "USD", "USD", "USD", 2, 0.01, 0.01, 1.0, 0.01, 100.0, 0.01, 0.01, "price", 0,
        canonical_underlying="NASDAQ_100", expected_company="XM Global Limited",
        broker_path_regex=r"^Indices\\US Indices$", broker_description_regex=r"^US100 Cash Index$",
        session_calendar_id="US_EQUITY_RTH", expected_one_tick_value_at_min_volume=0.01,
        economic_value_tolerance=0.0001,
    )


def metadata(symbol: str = "US100Cash") -> dict[str, object]:
    return {
        "name": symbol, "digits": 2, "point": 0.01, "tick_size": 0.01,
        "contract_size": 1.0, "volume_min": 0.01, "volume_max": 100.0,
        "volume_step": 0.01, "base_currency": "USD", "profit_currency": "USD",
        "margin_currency": "USD", "trade_calc_mode": 0,
    }


def test_account_daily_counts_include_foreign_campaign_and_unknown_facts_fail_closed() -> None:
    spx = contract("spx", "US500Cash")
    rows = {
        "account_info": {"login": 123, "equity": 10_000.0, "margin_free": 5_000.0},
        "positions_get": (), "orders_get": (),
        "history_deals_get": ({
            "deal_id": "foreign-entry", "order": "foreign-order", "entry": "IN", "time": int((NOW - pd.Timedelta(minutes=1)).timestamp()),
            "symbol": "US500Cash", "magic": 999,
        },),
    }
    builder = BrokerFactsBuilder(
        read=lambda operation, *_args: rows[operation],
        order_calc_profit=lambda *_args: -1.0,
        order_calc_margin=lambda *_args: 1.0,
        halt_reader=lambda: False, strategy_health_reader=lambda: "ACTIVE",
        starting_risk_reader=lambda _position_id: None,
        strategy_matcher=lambda row: row.get("magic") == 7,
        contract_resolver=lambda symbol: {"US500Cash": spx}[symbol],
    )
    snapshot = builder.build(
        now=NOW, contract=contract(), candidate_hash="c" * 64,
        deals_start=NOW - timedelta(days=1), deals_end=NOW,
    )
    assert snapshot.total_entry_count == 1
    assert snapshot.entry_counts_by_instrument == {"nq": 0, "spx": 1}

    rows["orders_get"] = ({"symbol": "UNKNOWN.SUFFIX", "type": 2, "volume": 1.0, "price_open": 100.0, "sl": 99.0},)
    with pytest.raises(BrokerFactsError, match="no signed contract"):
        builder.build(now=NOW, contract=contract(), candidate_hash="c" * 64,
                      deals_start=NOW - timedelta(days=1), deals_end=NOW)


def test_risk_guard_rejects_account_cap_and_outside_whitelist() -> None:
    nq = contract()
    policy = LiveRiskPolicy(
        daily_loss_cap_r=-1.0, max_trades_per_day_by_instrument=(("nq", 1), ("spx", 2)),
        max_total_trades_per_day=3, instrument_whitelist=(("nq", "US100Cash"), ("spx", "US500Cash")),
        max_open_positions_by_instrument=1, max_total_open_positions=2, entry_cooldown_seconds=300,
        max_position_volume_by_instrument=(("nq", 100.0), ("spx", 100.0)),
    )
    snapshot = BrokerSnapshot(
        NOW, 10_000.0, 5_000.0, daily_realized_r=0.0, strategy_health="ACTIVE", halt=False,
        instrument_contract=nq, total_stop_risk=0.0, snapshot_hash="a" * 64,
        policy_hash=policy.policy_hash, instrument_contract_hash=nq.contract_hash,
        order_calc_profit=lambda *_args: -100.0, order_calc_margin=lambda *_args: 1.0,
        total_entry_count=3, entry_counts_by_instrument={"nq": 1},
        open_position_counts_by_instrument={}, pending_order_counts_by_instrument={},
    )
    proposal = SignalProposal("p", "c" * 64, "nq", "long", 100.0, 99.0, 101.0, NOW, NOW.replace(hour=13), "e" * 64)
    guard = RiskGuard(policy=policy, clock=lambda: NOW)
    assert not guard.precheck(proposal, snapshot).approved

    outside = replace(snapshot, entry_counts_by_instrument={"other": 1}, total_entry_count=1)
    assert not guard.precheck(proposal, outside).approved


def test_entry_slot_reservation_is_atomic_across_two_connections(tmp_path: Path) -> None:
    db = tmp_path / "approvals.sqlite3"
    ApprovalStore(db).close()
    barrier = threading.Barrier(2)

    def reserve(order_id: str) -> str:
        store = ApprovalStore(db)
        try:
            barrier.wait(timeout=10)
            store.connection.execute("BEGIN IMMEDIATE")
            store._reserve_entry_slot(
                account_key="account", trade_date_ny="2026-09-08", order_id=order_id,
                instrument_id="nq", campaign_id="campaign", instrument_limit=1,
                total_limit=1, at_utc=NOW.isoformat(),
            )
            store.connection.commit()
            return "RESERVED"
        except ApprovalError:
            if store.connection.in_transaction:
                store.connection.rollback()
            return "BLOCKED"
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(reserve, ("order-a", "order-b")))
    assert outcomes == ["BLOCKED", "RESERVED"]


def test_retry_only_retries_explicit_read_transport_and_write_is_once() -> None:
    sleeps: list[float] = []
    attempts = 0

    def read() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AllowedTransportError(status_code=429, retry_after=2)
        return "ok"

    assert RetryPolicy(sleeper=sleeps.append, max_attempts=3).read(read) == "ok"
    assert attempts == 2 and sleeps == [2.0]
    def bad_read() -> None:
        raise ValueError("schema")

    with pytest.raises(NonRetryableReadError):
        RetryPolicy(sleeper=sleeps.append).read(bad_read)
    sent = 0

    def write() -> None:
        nonlocal sent
        sent += 1
        raise RuntimeError("ambiguous")

    with pytest.raises(RuntimeError):
        write_once(write)
    assert sent == 1


def test_symbol_metadata_tick_and_economic_fingerprint_are_exact() -> None:
    expected = semantic_contract()
    observed = {**metadata(), "path": r"Indices\US Indices", "description": "US100 Cash Index"}
    registry = InstrumentRegistry([expected])
    registry.symbol_info("US100Cash", observed)
    assert validate_current_tick(expected, {"bid": 100.0, "ask": 100.01, "time_msc": 1_000}) == pytest.approx(100.005)
    validate_economic_semantics(expected, lambda *_args: 0.01, reference_price=100.005)
    with pytest.raises(InstrumentContractError):
        registry.symbol_info("US100Cash", {**observed, "name": "US100Cash.pro"})
    with pytest.raises(InstrumentContractError):
        validate_current_tick(expected, {"bid": 100.0, "ask": float("nan"), "time_msc": 1_000})


def test_warmup_is_context_only_and_planned_closure_is_not_missing_data() -> None:
    window = EvaluationWindow(date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 3), warmup_bars=2)
    frame = pd.DataFrame({"time": pd.to_datetime([
        "2026-01-02T14:00:00Z", "2026-01-02T14:05:00Z", "2026-01-03T14:00:00Z",
    ])})
    source, evaluation = window.split_frame(frame)
    assert len(source) == 3 and len(evaluation) == 1
    with pytest.raises(EvaluationWindowError, match="outside evaluation"):
        window.assert_trades_in_window(pd.DataFrame({"date": [date(2026, 1, 2)]}))
    classes = classify_sessions(
        [date(2026, 1, 3), date(2026, 1, 4)], source_dates=[date(2026, 1, 3)],
        planned_closed=[date(2026, 1, 4)],
    )
    assert classes[date(2026, 1, 4)] == "PLANNED_CLOSED"


def _study(path: Path, *, step_sessions: int = 2) -> None:
    path.write_text(
        '{"seed":1,"symbol":"CAPITALCOM_NAS100","timeframe":"5m",'
        '"search_space":{"reward_r":[1.0]},"train_sessions":2,"oos_sessions":2,'
        f'"step_sessions":{step_sessions},"anchored":true,"max_evals":1,'
        '"minimum_closed_trades":1,"objective_metric":"daily_r_sharpe_rf0"}',
        encoding="utf-8",
    )


def test_research_study_rejects_overlap_and_never_publishes_promotable_empty_result(tmp_path: Path) -> None:
    path = tmp_path / "study.json"
    _study(path, step_sessions=1)
    with pytest.raises(StudyConfigError, match="overlap"):
        StudyConfig.load(path)
    _study(path)
    study = StudyConfig.load(path)
    manifest = optimize(study, lambda _params: {"fill_count": 0, "daily_r_sharpe_rf0": None}, tmp_path / "out")
    assert manifest["status"] == "NO_VALID_TRIALS_NON_PROMOTABLE"
    assert manifest["selection_status"] == "BLOCKED_NO_VALID_TRIAL"


def test_risk_xray_uses_null_reasons_for_undefined_metrics_and_rejects_open_fills() -> None:
    with pytest.raises(FinancialMathError, match="closed terminal"):
        build_risk_xray(pd.DataFrame([{"r_multiple": 1.0, "result": "OPEN", "terminal_known_time": "2026-01-02T15:00:00Z"}]))
    dates = ["2026-01-02", "2026-01-05"]
    xray = build_risk_xray(
        pd.DataFrame([
            {"r_multiple": 0.0, "date": dates[0], "symbol": "nq", "direction": "long", "terminal_known_time": "2026-01-02T15:00:00Z"},
        ]),
        eligible_dates=dates,
        provenance_hashes={"input_hash": "x" * 64, "code_hash": "0" * 64, "coverage_hash": "1" * 64},
    )
    assert xray["profit_factor"] is None
    assert xray["profit_factor_reason"] == "NO_GROSS_LOSS"
    assert xray["daily_r_sharpe_rf0"] is None
    assert xray["daily_r_sharpe_rf0_reason"] == "ZERO_VARIANCE"
    assert xray["provenance_complete"] is False


def test_strategy_health_blocks_late_terminal_deal_after_cursor() -> None:
    baseline = LockedOOSBaseline("c" * 64, 100, 100, (2022, 2023, 2024), 0.0, 1.0, 2.0, 7)
    health = StrategyHealth(baseline=baseline, expected_candidate_hash=baseline.candidate_hash, strict_broker_order=True)
    health.activate(broker_deals=[{"deal_id": "d1", "r_multiple": 1.0, "closed_at": "2026-01-02T15:00:00Z"}])
    decision = health.evaluate(broker_deals=[
        {"deal_id": "late", "r_multiple": 0.1, "closed_at": "2026-01-01T15:00:00Z"},
        {"deal_id": "d1", "r_multiple": 1.0, "closed_at": "2026-01-02T15:00:00Z"},
    ])
    assert "late" in decision.reason
    assert not health.allows_new_position()


def test_settings_reject_unknown_project_env_but_redact_secrets() -> None:
    config = {"account_mode": "RESEARCH", "expected_server": "server", "terminal_path": ""}
    with pytest.raises(SettingsError, match="unknown project"):
        load_settings(config, environment={"SUPER1_UNKNOWN": "value"}, enforce_required=False)
    settings = load_settings(config, environment={"XM_MT5_PASSWORD": "secret", "XM_MT5_SERVER": "server"}, enforce_required=False)
    assert "secret" not in repr(settings)
    assert settings.to_public_dict()["password"] == "***"


def test_financial_contracts_reject_unsafe_denominators_and_nonfinite_quantization() -> None:
    with pytest.raises(FinancialMathError):
        safe_divide(1.0, -1.0, reason="undefined")
    with pytest.raises(FinancialMathError):
        normalize_r(1.0, 0.0)
    with pytest.raises(FinancialMathError):
        quantize_price(float("inf"), 0.01)
    assert profit_factor(10.0, 0.0) == (None, "NO_GROSS_LOSS")
