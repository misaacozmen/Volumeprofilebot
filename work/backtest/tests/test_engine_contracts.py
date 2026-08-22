from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.manual_state import stable_state_id
from backtest.strategy import (
    blocked_by_swing_first30_directionality,
    build_session_liquidity_levels,
    compute_first30_directionality,
    find_entry_resolution,
    is_tick_aligned,
    previous_completed_ny_session_date,
    round_price_to_tick,
    timestamp_on_day,
)
from backtest.config import SymbolConfig


def test_intrabar_entry_target_conflict_is_rejected_by_default() -> None:
    rows = [
        SimpleNamespace(
            time=pd.Timestamp("2025-01-02 10:00", tz="America/New_York"),
            low=99.0,
            high=101.0,
            close=100.0,
        ),
        SimpleNamespace(
            time=pd.Timestamp("2025-01-02 10:03", tz="America/New_York"),
            low=99.0,
            high=111.0,
            close=105.0,
        ),
    ]

    resolution = find_entry_resolution(rows, 0, "long", 100.0, 110.0)

    assert resolution.entry_index is None
    assert resolution.terminal_reason == "CANCELLED_AMBIGUOUS_INTRABAR"
    assert resolution.intrabar_ambiguity == "ENTRY_AND_TARGET_TOUCHED_SAME_BAR"


def test_intrabar_entry_target_conflict_can_be_explicitly_labeled() -> None:
    rows = [
        SimpleNamespace(
            time=pd.Timestamp("2025-01-02 10:00", tz="America/New_York"),
            low=99.0,
            high=101.0,
            close=100.0,
        ),
        SimpleNamespace(
            time=pd.Timestamp("2025-01-02 10:03", tz="America/New_York"),
            low=99.0,
            high=111.0,
            close=105.0,
        ),
    ]

    resolution = find_entry_resolution(
        rows,
        0,
        "long",
        100.0,
        110.0,
        reject_entry_target_same_bar=False,
    )

    assert resolution.entry_index == 1
    assert resolution.intrabar_ambiguity == "ENTRY_AND_TARGET_TOUCHED_SAME_BAR"


def test_tick_rounding_and_alignment_are_deterministic() -> None:
    assert round_price_to_tick(100.13, 0.25) == 100.25
    assert is_tick_aligned(100.25, 0.25)
    assert not is_tick_aligned(100.13, 0.25)


def test_state_ids_are_replay_stable() -> None:
    first = stable_state_id("ORD", "NQ", "2025-01-02", "10:00", "long")
    second = stable_state_id("ORD", "NQ", "2025-01-02", "10:00", "long")
    different = stable_state_id("ORD", "NQ", "2025-01-02", "10:03", "long")
    assert first == second
    assert first != different


def test_ny_session_time_tracks_dst_without_changing_wall_clock() -> None:
    before = timestamp_on_day(pd.Timestamp("2025-03-07").date(), "09:30")
    after = timestamp_on_day(pd.Timestamp("2025-03-10").date(), "09:30")

    assert before.strftime("%H:%M%z") == "09:30-0500"
    assert after.strftime("%H:%M%z") == "09:30-0400"


def test_first30_directionality_is_not_available_from_future_bars() -> None:
    times = pd.date_range("2025-01-02 09:30", periods=6, freq="5min", tz="America/New_York")
    frame = pd.DataFrame(
        {
            "time": times,
            "open": [100.0] * 6,
            "high": [102.0, 103.0, 104.0, 120.0, 130.0, 140.0],
            "low": [99.0, 98.0, 97.0, 80.0, 70.0, 60.0],
            "close": [101.0, 102.0, 103.0, 80.0, 70.0, 60.0],
        }
    )
    as_of = pd.Timestamp("2025-01-02 09:45", tz="America/New_York")
    causal = compute_first30_directionality(frame, as_of.date(), as_of=as_of)
    changed_future = frame.copy()
    changed_future.loc[changed_future["time"] >= as_of, ["high", "low", "close"]] = [1000.0, 0.0, 999.0]

    assert compute_first30_directionality(changed_future, as_of.date(), as_of=as_of) == causal
    config = SymbolConfig("TEST", "5m", 1.0, 0.0, 0.0, swing_first30_directionality_min=0.9)
    assert not blocked_by_swing_first30_directionality("swing_high_1", 0.1, config, as_of=as_of)
    assert blocked_by_swing_first30_directionality(
        "swing_high_1",
        0.1,
        config,
        as_of=pd.Timestamp("2025-01-02 10:00", tz="America/New_York"),
    )


def test_previous_session_skips_weekend_data() -> None:
    regular = pd.DataFrame(
        {
            "time": pd.date_range("2025-01-03 09:30", "2025-01-03 15:55", freq="5min", tz="America/New_York"),
            "high": 110.0,
            "low": 90.0,
        }
    )
    regular.loc[regular.index[-1], ["high", "low"]] = [115.0, 85.0]
    frame = pd.concat(
        [
            regular,
            pd.DataFrame(
                [
                    {"time": pd.Timestamp("2025-01-05 18:00", tz="America/New_York"), "high": 999.0, "low": 1.0},
                    {"time": pd.Timestamp("2025-01-06 08:00", tz="America/New_York"), "high": 105.0, "low": 95.0},
                ]
            ),
        ],
        ignore_index=True,
    )

    assert previous_completed_ny_session_date(frame, pd.Timestamp("2025-01-06").date()) == pd.Timestamp(
        "2025-01-03"
    ).date()
    levels = {level.name: level.price for level in build_session_liquidity_levels(frame, pd.Timestamp("2025-01-06").date())}
    assert levels["previous_day_high"] == 115.0
    assert levels["previous_day_low"] == 85.0


def test_previous_session_skips_holiday_without_regular_session_bars() -> None:
    regular = pd.DataFrame(
        {
            "time": pd.date_range("2025-01-06 09:30", "2025-01-06 15:55", freq="5min", tz="America/New_York"),
            "high": 110.0,
            "low": 90.0,
        }
    )
    frame = pd.concat(
        [
            regular,
            pd.DataFrame(
                [
                    {"time": pd.Timestamp("2025-01-07 18:00", tz="America/New_York"), "high": 120.0, "low": 80.0},
                    {"time": pd.Timestamp("2025-01-08 08:00", tz="America/New_York"), "high": 105.0, "low": 95.0},
                ]
            ),
        ],
        ignore_index=True,
    )

    assert previous_completed_ny_session_date(frame, pd.Timestamp("2025-01-08").date()) == pd.Timestamp(
        "2025-01-06"
    ).date()


def test_previous_session_rejects_sparse_intraday_fragment() -> None:
    complete = pd.DataFrame(
        {
            "time": pd.date_range("2025-01-03 09:30", "2025-01-03 15:55", freq="5min", tz="America/New_York")
        }
    )
    fragment = pd.DataFrame(
        {
            "time": pd.date_range("2025-01-06 10:00", "2025-01-06 14:00", periods=4, tz="America/New_York")
        }
    )
    frame = pd.concat([complete, fragment], ignore_index=True)

    assert previous_completed_ny_session_date(frame, pd.Timestamp("2025-01-07").date()) == pd.Timestamp(
        "2025-01-03"
    ).date()
