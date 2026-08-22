from __future__ import annotations

import pandas as pd

from scripts.research_execution_replay import compute_causal_m1_volume_profile, replay_limit_order_m1


def decision(**updates) -> dict[str, object]:
    value = {
        "order_id": "order-1",
        "direction": "long",
        "entry_price": 100.0,
        "stop_price": 99.0,
        "target_price": 102.0,
        "fvg_known_time": "2026-07-29T13:30:00Z",
        "terminal_known_time": "2026-07-29T14:00:00Z",
    }
    value.update(updates)
    return value


def minutes(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"time": time, "open": 100.5, "high": high, "low": low, "close": 100.5, "volume": 1.0}
            for time, low, high in rows
        ]
    )


def test_m1_replay_marks_entry_stop_sequence_unknown() -> None:
    result = replay_limit_order_m1(
        decision(),
        minutes([("2026-07-29T13:30:00Z", 98.5, 100.5)]),
    )
    assert result.state == "AMBIGUOUS"
    assert result.outcome == "WATCH"
    assert result.ambiguity == "ENTRY_AND_STOP_TOUCHED_SAME_M1"


def test_m1_replay_resolves_order_across_separate_minutes() -> None:
    result = replay_limit_order_m1(
        decision(),
        minutes(
            [
                ("2026-07-29T13:30:00Z", 99.8, 100.5),
                ("2026-07-29T13:31:00Z", 100.1, 102.2),
            ]
        ),
    )
    assert result.state == "FILLED"
    assert result.outcome == "TP"
    assert result.ambiguity == ""


def test_causal_m1_profile_excludes_bars_unknown_at_cutoff() -> None:
    frame = pd.DataFrame(
        [
            {"time": "2026-07-29T13:28:00Z", "known_time": "2026-07-29T13:29:00Z", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 10},
            {"time": "2026-07-29T13:29:00Z", "known_time": "2026-07-29T13:31:00Z", "open": 100, "high": 110, "low": 90, "close": 100, "volume": 1000},
        ]
    )
    profile = compute_causal_m1_volume_profile(
        frame,
        pd.Timestamp("2026-07-29T09:00:00-04:00"),
        pd.Timestamp("2026-07-29T09:30:00-04:00"),
        rows=10,
    )
    assert profile is not None
    assert profile.vah < 20
