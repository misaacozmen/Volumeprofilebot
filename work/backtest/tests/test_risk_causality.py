from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.risk import apply_pair_risk_rule


def assert_raises_value_error(match: str, callback) -> None:
    try:
        callback()
    except ValueError as exc:
        assert match in str(exc)
        return
    raise AssertionError("Expected ValueError")


def frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    result = pd.DataFrame(rows)
    result["group"] = "pair"
    result["label"] = result["symbol"]
    return result


def test_future_loss_cannot_suppress_earlier_pair_entry() -> None:
    trades = frame(
        [
            {
                "symbol": "NQ",
                "date": "2025-02-26",
                "entry_time": "2025-02-26T10:24:00-05:00",
                "exit_time": "2025-02-26T10:27:00-05:00",
                "r_multiple": -1.0,
            },
            {
                "symbol": "SPX",
                "date": "2025-02-26",
                "entry_time": "2025-02-26T10:25:00-05:00",
                "exit_time": "2025-02-26T10:30:00-05:00",
                "r_multiple": -1.0,
            },
        ]
    )

    capped = apply_pair_risk_rule(trades, -1.0)

    assert capped["symbol"].tolist() == ["NQ", "SPX"]


def test_realized_loss_suppresses_later_pair_entry() -> None:
    trades = frame(
        [
            {
                "symbol": "SPX",
                "date": "2025-03-07",
                "entry_time": "2025-03-07T10:00:00-05:00",
                "exit_time": "2025-03-07T10:15:00-05:00",
                "r_multiple": -1.0,
            },
            {
                "symbol": "NQ",
                "date": "2025-03-07",
                "entry_time": "2025-03-07T10:24:00-05:00",
                "exit_time": "2025-03-07T10:30:00-05:00",
                "r_multiple": 3.0,
            },
        ]
    )

    capped = apply_pair_risk_rule(trades, -1.0)

    assert capped["symbol"].tolist() == ["SPX"]


def test_terminal_time_is_required_for_causal_cap() -> None:
    trades = frame(
        [
            {
                "symbol": "NQ",
                "date": "2025-01-02",
                "entry_time": "2025-01-02T10:00:00-05:00",
                "r_multiple": -1.0,
            }
        ]
    )

    assert_raises_value_error("terminal", lambda: apply_pair_risk_rule(trades, -1.0))


def test_terminal_before_entry_is_rejected() -> None:
    trades = frame(
        [
            {
                "symbol": "NQ",
                "date": "2025-01-02",
                "entry_time": "2025-01-02T10:00:00-05:00",
                "exit_time": "2025-01-02T09:59:00-05:00",
                "r_multiple": -1.0,
            }
        ]
    )

    assert_raises_value_error("earlier", lambda: apply_pair_risk_rule(trades, -1.0))


def test_bar_label_is_shifted_to_close_time_when_timeframe_is_available() -> None:
    trades = frame(
        [
            {
                "symbol": "SPX",
                "timeframe": "5m",
                "date": "2025-01-02",
                "entry_time": "2025-01-02T10:00:00-05:00",
                "exit_time": "2025-01-02T10:15:00-05:00",
                "r_multiple": -1.0,
            },
            {
                "symbol": "NQ",
                "timeframe": "3m",
                "date": "2025-01-02",
                "entry_time": "2025-01-02T10:18:00-05:00",
                "exit_time": "2025-01-02T10:30:00-05:00",
                "r_multiple": 3.0,
            },
        ]
    )

    capped = apply_pair_risk_rule(trades, -1.0)

    assert capped["symbol"].tolist() == ["SPX", "NQ"]
