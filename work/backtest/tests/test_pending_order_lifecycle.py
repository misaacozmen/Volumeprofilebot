from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.strategy import find_entry_fill, find_entry_resolution


def candle(clock: str, low: float, high: float, close: float) -> SimpleNamespace:
    return SimpleNamespace(
        time=pd.Timestamp(f"2025-01-02 {clock}", tz="America/New_York"),
        low=low,
        high=high,
        close=close,
    )


def test_legacy_fill_wrapper_still_returns_index() -> None:
    rows = [
        candle("10:00", 99.0, 101.0, 100.0),
        candle("10:03", 99.5, 100.5, 100.2),
    ]
    assert find_entry_fill(rows, 0, "long", 100.0, 110.0) == 1


def test_target_before_fill_has_explicit_terminal_reason() -> None:
    rows = [
        candle("10:00", 99.0, 101.0, 100.0),
        candle("10:03", 105.0, 111.0, 110.0),
    ]
    resolution = find_entry_resolution(rows, 0, "long", 100.0, 110.0)
    assert resolution.entry_index is None
    assert resolution.terminal_reason == "CANCELLED_TARGET_BEFORE_FILL"
    assert resolution.terminal_time == rows[1].time


def test_synthetic_target_can_be_ignored_when_manual_target_is_not_authoritative() -> None:
    rows = [
        candle("10:00", 99.0, 101.0, 100.0),
        candle("10:03", 105.0, 111.0, 110.0),
        candle("10:06", 99.5, 101.0, 100.0),
    ]
    resolution = find_entry_resolution(
        rows,
        0,
        "long",
        100.0,
        110.0,
        cancel_target_before_fill=False,
    )
    assert resolution.entry_index == 2
    assert resolution.terminal_reason == "FILLED"


def test_opposite_cisd_cancels_pending_order() -> None:
    rows = [
        candle("10:00", 99.0, 101.0, 100.0),
        candle("10:03", 101.0, 104.0, 103.0),
    ]
    resolution = find_entry_resolution(
        rows,
        0,
        "short",
        110.0,
        90.0,
        cancel_on_opposite_cisd=True,
        cisd_invalidation_level=102.0,
    )
    assert resolution.entry_index is None
    assert resolution.terminal_reason == "CANCELLED_OPPOSITE_CISD"


def test_unfilled_order_reaches_trade_window_terminal() -> None:
    rows = [
        candle("10:00", 99.0, 101.0, 100.0),
        candle("10:03", 101.0, 102.0, 101.5),
    ]
    resolution = find_entry_resolution(rows, 0, "long", 95.0, 110.0)
    assert resolution.entry_index is None
    assert resolution.terminal_reason == "CANCELLED_TRADE_WINDOW_END"
    assert resolution.terminal_time == rows[-1].time
