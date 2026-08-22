from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.config import SymbolConfig
import backtest.engine_pipeline as engine_pipeline
from backtest.engine_pipeline import (
    EngineLeg,
    feed_name,
    run_canonical_pair_pipeline,
    stable_frame_hash,
    validate_leg_contract,
)


def test_feed_name_is_explicit() -> None:
    assert feed_name("DUKASCOPY_USATECHIDXUSD") == "DUKASCOPY"
    assert feed_name("NQ") == "UNSPECIFIED"


def test_frame_hash_is_row_order_invariant() -> None:
    frame = pd.DataFrame([{"a": 2, "b": 1}, {"a": 1, "b": 2}])
    assert stable_frame_hash(frame) == stable_frame_hash(frame.iloc[::-1])


def test_timeframe_contract_accepts_matching_cadence() -> None:
    frame = pd.DataFrame(
        {
            "time": pd.date_range("2025-01-02 09:30", periods=3, freq="3min", tz="America/New_York"),
            "open": [1, 1, 1],
            "high": [2, 2, 2],
            "low": [0, 0, 0],
            "close": [1, 1, 1],
            "volume": [1, 1, 1],
        }
    )
    leg = EngineLeg("nq", frame, SymbolConfig("DUKASCOPY_NQ", "3m", 1.0, 0.0, 0.0))

    validate_leg_contract(leg)


def market_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": pd.date_range("2025-01-02 09:30", periods=3, freq="3min", tz="America/New_York"),
            "open": [1.0, 1.0, 1.0],
            "high": [2.0, 2.0, 2.0],
            "low": [0.0, 0.0, 0.0],
            "close": [1.0, 1.0, 1.0],
            "volume": [1.0, 1.0, 1.0],
        }
    )


def test_leg_contract_rejects_nonmonotonic_and_duplicate_timestamps() -> None:
    config = SymbolConfig("DUKASCOPY_NQ", "3m", 1.0, 0.0, 0.0)
    nonmonotonic = market_frame().iloc[[1, 0, 2]].reset_index(drop=True)
    duplicate = market_frame().copy()
    duplicate.loc[1, "time"] = duplicate.loc[0, "time"]

    with pytest.raises(ValueError, match="NON_MONOTONIC_TIME"):
        validate_leg_contract(EngineLeg("nq", nonmonotonic, config))
    with pytest.raises(ValueError, match="DUPLICATE_BAR"):
        validate_leg_contract(EngineLeg("nq", duplicate, config))


def test_pair_pipeline_rejects_duplicate_leg_keys_before_execution() -> None:
    leg = EngineLeg("nq", market_frame(), SymbolConfig("DUKASCOPY_NQ", "3m", 1.0, 0.0, 0.0))

    with pytest.raises(ValueError, match="unique"):
        run_canonical_pair_pipeline([leg, leg], [])


def test_pipeline_preserves_partial_r_and_does_not_realize_open_trade(monkeypatch) -> None:
    decisions = pd.DataFrame(
        [
            {
                "symbol": "DUKASCOPY_NQ",
                "date": "2025-01-02",
                "order_state": "FILLED",
                "outcome": "SL",
                "order_id": "partial-loss",
                "entry_time": "2025-01-02T10:00:00-05:00",
                "terminal_known_time": "2025-01-02T10:10:00-05:00",
                "exit_price": 99.5,
                "r_multiple": -0.5,
            },
            {
                "symbol": "DUKASCOPY_NQ",
                "date": "2025-01-02",
                "order_state": "FILLED",
                "outcome": "OPEN",
                "order_id": "open-trade",
                "entry_time": "2025-01-02T10:20:00-05:00",
                "terminal_known_time": "2025-01-02T12:05:00-05:00",
                "exit_price": 99.75,
                "r_multiple": -0.25,
            },
        ]
    )
    monkeypatch.setattr(
        engine_pipeline,
        "run_manual_state_backtest",
        lambda frame, config, dates, state_config: type("Result", (), {"days": [config.symbol]})(),
    )
    monkeypatch.setattr(engine_pipeline, "decisions_to_frame", lambda days: decisions.copy())
    leg = EngineLeg("nq", market_frame(), SymbolConfig("DUKASCOPY_NQ", "3m", 1.0, 0.0, 0.0))

    result = run_canonical_pair_pipeline([leg], [pd.Timestamp("2025-01-02").date()])
    filled = result.filled_after_pair_cap.set_index("order_id")

    assert filled.loc["partial-loss", "r_multiple"] == -0.5
    assert pd.isna(filled.loc["open-trade", "r_multiple"])
    assert filled.loc["open-trade", "mark_to_market_r_multiple"] == -0.25
    assert result.decisions.set_index("order_id").loc["open-trade", "r_multiple"] == -0.25


def test_pair_cap_keys_orders_by_leg_and_order_id(monkeypatch) -> None:
    rows = {
        "DUKASCOPY_A": {
            "symbol": "DUKASCOPY_A",
            "date": "2025-01-02",
            "order_state": "FILLED",
            "outcome": "SL",
            "order_id": "shared-id",
            "entry_time": "2025-01-02T10:00:00-05:00",
            "terminal_known_time": "2025-01-02T10:10:00-05:00",
            "exit_price": 99.0,
            "r_multiple": -1.0,
        },
        "DUKASCOPY_B": {
            "symbol": "DUKASCOPY_B",
            "date": "2025-01-02",
            "order_state": "FILLED",
            "outcome": "TP",
            "order_id": "shared-id",
            "entry_time": "2025-01-02T10:20:00-05:00",
            "terminal_known_time": "2025-01-02T10:30:00-05:00",
            "exit_price": 101.0,
            "r_multiple": 1.0,
        },
    }
    monkeypatch.setattr(
        engine_pipeline,
        "run_manual_state_backtest",
        lambda frame, config, dates, state_config: type("Result", (), {"days": [config.symbol]})(),
    )
    monkeypatch.setattr(
        engine_pipeline,
        "decisions_to_frame",
        lambda days: pd.DataFrame([rows[days[0]]]),
    )
    legs = [
        EngineLeg("a", market_frame(), SymbolConfig("DUKASCOPY_A", "3m", 1.0, 0.0, 0.0)),
        EngineLeg("b", market_frame(), SymbolConfig("DUKASCOPY_B", "3m", 1.0, 0.0, 0.0)),
    ]

    result = run_canonical_pair_pipeline(legs, [pd.Timestamp("2025-01-02").date()])

    assert result.filled_after_pair_cap["leg_key"].tolist() == ["a"]
    assert result.suppressed_by_pair_cap["leg_key"].tolist() == ["b"]
    assert result.decisions.set_index("leg_key")["pair_risk_state"].to_dict() == {
        "a": "ALLOWED",
        "b": "SUPPRESSED_DAILY_CAP",
    }
