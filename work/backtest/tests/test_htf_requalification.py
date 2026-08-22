from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.strategy import HtfArray, detect_htf_arrays, htf_array_closed_through, htf_array_near_value_area


def test_detects_bullish_fvg_and_body_close() -> None:
    times = pd.date_range("2024-01-01 09:00", periods=3, freq="15min", tz="America/New_York")
    frame = pd.DataFrame(
        {
            "time": times,
            "open": [100.0, 102.0, 105.0],
            "high": [101.0, 104.0, 107.0],
            "low": [99.0, 101.5, 103.0],
            "close": [100.5, 103.5, 106.0],
        }
    )
    arrays = detect_htf_arrays(frame, "fvg")
    assert arrays == [HtfArray("fvg", "bullish", 101.0, 103.0, times[2] + pd.Timedelta(minutes=15))]
    assert htf_array_closed_through(arrays[0], 100.9)
    assert not htf_array_closed_through(arrays[0], 101.0)


def test_detects_order_block_without_selector_weight() -> None:
    times = pd.date_range("2024-01-01 09:00", periods=2, freq="15min", tz="America/New_York")
    frame = pd.DataFrame(
        {
            "time": times,
            "open": [102.0, 100.0],
            "high": [103.0, 105.0],
            "low": [99.0, 99.5],
            "close": [100.0, 104.0],
        }
    )
    arrays = detect_htf_arrays(frame, "fvg_ob")
    assert HtfArray("order_block", "bullish", 100.0, 102.0, times[1] + pd.Timedelta(minutes=15)) in arrays


def test_value_area_relevance_uses_existing_symbol_tolerance() -> None:
    array = HtfArray(
        "fvg",
        "bearish",
        100.0,
        101.0,
        pd.Timestamp("2024-01-01 09:30", tz="America/New_York"),
    )
    assert htf_array_near_value_area(array, vah=102.0, val=90.0, tolerance=1.0)
    assert not htf_array_near_value_area(array, vah=103.0, val=90.0, tolerance=1.0)
