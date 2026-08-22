from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.manual_state import (
    CisdEvent,
    ContextTrigger,
    HtfArrayLifecycle,
    HtfTransition,
    ManualStateConfig,
    ThesisEpisode,
    add_optional_structure_trigger,
    build_authority_aware_cisd_structure_lifecycle,
    build_premarket_context,
    build_cisd_structure_lifecycle,
    build_authoritative_context_triggers,
    build_context_authority_lifecycle,
    build_liquidity_selection,
    build_thesis_episodes,
    collapse_directional_context_triggers,
    detect_cisd_events,
    detect_manual_htf_arrays,
    detect_va_flip_triggers,
    evaluate_htf_array,
    make_decision,
    next_context_change_index,
    pending_order_terminal,
    select_controlling_array,
)
from backtest.config import SymbolConfig
from backtest.strategy import HtfArray, LiquidityLevel, SweepEvent, find_fvg_or_ifvg


def bar(clock: str, open_: float, high: float, low: float, close: float) -> SimpleNamespace:
    return SimpleNamespace(
        time=pd.Timestamp(f"2025-01-02 {clock}", tz="America/New_York"),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


def test_cisd_detector_emits_anchor_confirm_and_invalidation() -> None:
    rows = [
        bar("09:30", 101.0, 102.0, 98.0, 99.0),
        bar("09:33", 99.0, 102.5, 98.5, 102.0),
        bar("09:36", 102.0, 103.0, 101.0, 102.5),
    ]
    events = detect_cisd_events(rows)
    bullish = next(event for event in events if event.direction == "long")
    assert bullish.anchor_index == 0
    assert bullish.confirm_index == 1
    assert bullish.body_level == 101.0
    assert bullish.body_stop == 99.0
    assert bullish.invalidation_level == 98.0
    assert bullish.displacement_points == 1.0
    assert pd.Timestamp(bullish.confirm_known_time) == rows[1].time + pd.Timedelta(minutes=3)


def test_thesis_episode_opposite_cisd_invalidates_without_granting_new_direction() -> None:
    rows = [
        bar("09:30", 101.0, 102.0, 98.0, 99.0),
        bar("09:33", 99.0, 102.5, 98.5, 102.0),
        bar("09:36", 102.0, 103.0, 98.0, 98.5),
    ]
    events = detect_cisd_events(rows)
    episodes = build_thesis_episodes(events, "long")
    assert [episode.direction for episode in episodes] == ["long"]
    assert episodes[0].status == "INVALIDATED"
    assert episodes[0].terminal_reason == "OPPOSITE_CISD"


def test_htf_array_lifecycle_tracks_body_close_and_flip() -> None:
    times = pd.date_range("2025-01-02 09:00", periods=4, freq="15min", tz="America/New_York")
    htf = pd.DataFrame(
        {
            "time": times,
            "open": [104.0, 104.0, 99.0, 100.0],
            "high": [105.0, 105.0, 103.0, 102.0],
            "low": [103.0, 99.0, 98.0, 99.0],
            "close": [104.0, 100.0, 99.0, 99.5],
        }
    )
    array = HtfArray("fvg", "bullish", 100.0, 102.0, times[0] + pd.Timedelta(minutes=15))
    lifecycle = evaluate_htf_array(array, htf, times[-1] + pd.Timedelta(minutes=15), 1)
    assert any(transition.state == "BODY_CLOSED" for transition in lifecycle.transitions)
    assert lifecycle.current_state == "FLIPPED_RESISTANCE"
    assert lifecycle.role == "RESISTANCE"


def test_va_flip_trigger_replaces_liquidity_requirement() -> None:
    rows = [
        bar("09:30", 99.0, 99.5, 98.5, 99.0),
        bar("09:33", 99.0, 101.0, 98.8, 100.5),
        bar("09:36", 100.5, 101.5, 99.8, 101.0),
    ]
    triggers = detect_va_flip_triggers(rows, vah=100.0, val=95.0, tolerance=0.25)
    assert len(triggers) == 1
    assert triggers[0].gate == "REPLACED_BY_VA_FLIP"
    assert triggers[0].preferred_direction == "long"


def test_controlling_array_requires_real_reaction_not_only_proximity() -> None:
    fresh = HtfArrayLifecycle(
        "fresh",
        "fvg",
        "bullish",
        99.0,
        101.0,
        "2025-01-02T08:00:00-05:00",
        "FRESH",
        "SUPPORT",
        (),
    )
    respected = HtfArrayLifecycle(
        "respected",
        "order_block",
        "bullish",
        99.5,
        100.5,
        "2025-01-02T07:00:00-05:00",
        "RESPECTED",
        "SUPPORT",
        (HtfTransition("RESPECTED", "2025-01-02T08:30:00-05:00", 101.0),),
    )
    selected = select_controlling_array([fresh, respected], vah=100.0, val=95.0, tolerance=1.0)
    assert selected is not None
    assert selected.array_id == "respected"


def test_latest_tap_without_reaction_removes_controlling_status() -> None:
    tapped_after_old_reaction = HtfArrayLifecycle(
        "waiting",
        "fvg",
        "bearish",
        99.0,
        101.0,
        "2025-01-02T07:00:00-05:00",
        "TAPPED",
        "RESISTANCE",
        (
            HtfTransition("RESPECTED", "2025-01-02T08:00:00-05:00", 98.0),
            HtfTransition("TAPPED", "2025-01-02T09:15:00-05:00", 100.0),
        ),
    )
    assert select_controlling_array([tapped_after_old_reaction], vah=100.0, val=95.0, tolerance=1.0) is None


def test_ny_reaction_cannot_create_controlling_gate_by_default() -> None:
    assert ManualStateConfig().allow_ny_htf_reaction_gate is False
    assert ManualStateConfig().cancel_entry_on_terminal_candle is True
    assert ManualStateConfig().intrabar_ambiguity_policy == "reject"


def test_manual_decision_carries_exact_exit_and_r_multiple() -> None:
    cisd = CisdEvent(
        "long",
        0,
        "2025-01-02T09:30:00-05:00",
        1,
        "2025-01-02T09:33:00-05:00",
        100.0,
        99.0,
        98.0,
        1.0,
        1.0,
    )
    episode = ThesisEpisode(1, "long", cisd)
    trigger = ContextTrigger(
        0,
        "2025-01-02T09:30:00-05:00",
        "REQUIRED_SATISFIED",
        "long",
        "TEST",
        "swing_low_1",
    )
    exact_r = -0.333333333333

    decision = make_decision(
        SymbolConfig("TEST", "3m", 1.0, 0.0, 0.0),
        pd.Timestamp("2025-01-02").date(),
        trigger,
        episode,
        "VALID",
        "FILLED",
        "TAKE",
        "SL",
        "FILLED_REDUCED_LOSS",
        "2025-01-02T10:00:00-05:00",
        "2025-01-02T09:36:00-05:00",
        "bullish_fvg",
        "2025-01-02T09:39:00-05:00",
        100.0,
        99.0,
        103.0,
        exit_price=99.666666666667,
        r_multiple=exact_r,
    )

    assert decision.exit_price == 99.666666666667
    assert decision.r_multiple == exact_r


def test_premarket_context_treats_reacted_htf_as_block_not_direction_source() -> None:
    rows = [bar("09:30", 100.0, 101.0, 99.0, 100.5)]
    controlling = HtfArrayLifecycle(
        "support",
        "fvg",
        "bullish",
        99.0,
        101.0,
        "2025-01-02T08:00:00-05:00",
        "RESPECTED",
        "SUPPORT",
        (HtfTransition("RESPECTED", "2025-01-02T09:00:00-05:00", 101.5),),
    )
    config = SymbolConfig("TEST", "3m", 1.0, 0.0, 0.0)
    context = build_premarket_context(rows, [], 100.0, 95.0, [], [], controlling, config)
    assert context.gate == "HTF_BLOCK_ONLY"
    assert context.preferred_direction == ""
    assert context.blocked_direction == "short"
    assert context.controlling_decision_role == "BLOCK_ONLY"


def test_premarket_context_requires_single_selected_liquidity_without_htf() -> None:
    rows = [bar("09:30", 100.0, 101.0, 99.0, 100.5)]
    levels = [
        LiquidityLevel("far_high", "high", 110.0),
        LiquidityLevel("near_high", "high", 100.5),
        LiquidityLevel("near_low", "low", 95.0),
    ]
    config = SymbolConfig("TEST", "3m", 1.0, 0.0, 0.0)
    context = build_premarket_context(rows, [], 100.0, 95.0, levels, [], None, config)
    assert context.gate == "REQUIRED"
    assert context.preferred_direction == "short"
    assert context.selected_liquidity_name == "near_high"


def test_order_block_requires_displacement_backed_fvg() -> None:
    times = pd.date_range("2025-01-02 08:00", periods=3, freq="15min", tz="America/New_York")
    no_fvg = pd.DataFrame(
        {
            "time": times,
            "open": [101.0, 100.0, 102.0],
            "high": [102.0, 104.0, 104.0],
            "low": [99.0, 99.5, 101.0],
            "close": [100.0, 103.0, 103.0],
        }
    )
    assert not any(array.array_type == "order_block" for array in detect_manual_htf_arrays(no_fvg))

    with_fvg = no_fvg.copy()
    with_fvg.loc[2, "low"] = 103.0
    with_fvg.loc[2, "open"] = 103.0
    with_fvg.loc[2, "close"] = 104.0
    arrays = detect_manual_htf_arrays(with_fvg)
    assert any(array.array_type == "order_block" and array.direction == "bullish" for array in arrays)


def test_single_premarket_va_reaction_does_not_override_selected_liquidity() -> None:
    rows = [bar("09:30", 100.5, 101.0, 100.0, 100.8)]
    premarket = [bar("09:27", 99.8, 100.5, 99.7, 100.3)]
    levels = [LiquidityLevel("near_high", "high", 100.25)]
    config = SymbolConfig("TEST", "3m", 0.5, 0.0, 0.0)
    context = build_premarket_context(rows, premarket, 100.0, 95.0, levels, [], None, config)
    assert context.gate == "REQUIRED"
    assert context.preferred_direction == "short"
    assert context.blocked_direction == ""


def test_htf_block_rejects_conflicting_va_reaction_without_authorizing_other_side() -> None:
    rows = [bar("09:30", 100.0, 101.0, 99.0, 100.5)]
    premarket = [bar("09:27", 100.2, 100.5, 99.5, 99.7)]
    controlling = HtfArrayLifecycle(
        "support",
        "fvg",
        "bullish",
        99.0,
        101.0,
        "2025-01-02T08:00:00-05:00",
        "RESPECTED",
        "SUPPORT",
        (HtfTransition("RESPECTED", "2025-01-02T09:00:00-05:00", 101.5),),
    )
    config = SymbolConfig("TEST", "3m", 0.5, 0.0, 0.0)
    levels = [LiquidityLevel("near_high", "high", 100.25)]
    context = build_premarket_context(rows, premarket, 100.0, 95.0, levels, [], controlling, config)
    assert context.gate == "BLOCKED_HTF"
    assert context.preferred_direction == ""
    assert context.blocked_direction == "short"


def test_same_direction_requalification_does_not_terminate_pending_order() -> None:
    rows = [
        bar("09:30", 101.0, 102.0, 98.0, 99.0),
        bar("09:33", 99.0, 102.5, 98.5, 102.0),
        bar("09:36", 102.0, 102.5, 100.5, 101.0),
        bar("09:39", 101.0, 103.5, 100.8, 103.0),
        bar("09:42", 103.0, 103.2, 99.0, 100.0),
    ]
    episodes = build_thesis_episodes(detect_cisd_events(rows), "long")
    assert episodes[0].terminal_reason == "SAME_DIRECTION_CISD_REQUALIFICATION"
    terminal_index, terminal_reason, _ = pending_order_terminal(episodes, 0)
    assert terminal_index == 4
    assert terminal_reason == "OPPOSITE_CISD"


def test_post_cisd_fvg_can_use_cisd_anchor_as_first_candle() -> None:
    rows = [
        bar("11:42", 100.0, 101.0, 98.0, 99.0),
        bar("11:45", 99.0, 106.0, 98.5, 105.0),
        bar("11:48", 105.0, 108.0, 103.0, 107.0),
    ]
    result = find_fvg_or_ifvg(
        rows,
        cisd_index=1,
        direction="long",
        entry_mode="start",
        setup_type_filter="all",
        min_fvg_points=0.0,
        cisd_invalidation_level=98.0,
        window=5,
        search_start_index=0,
    )
    assert result[0] == 2
    assert result[1] == 103.0


def test_preexisting_cisd_cannot_use_fvg_completed_before_new_authority() -> None:
    rows = [
        bar("11:42", 100.0, 101.0, 98.0, 99.0),
        bar("11:45", 99.0, 106.0, 98.5, 105.0),
        bar("11:48", 105.0, 108.0, 103.0, 107.0),
    ]
    result = find_fvg_or_ifvg(
        rows,
        cisd_index=1,
        direction="long",
        entry_mode="start",
        setup_type_filter="all",
        min_fvg_points=0.0,
        cisd_invalidation_level=98.0,
        window=5,
        search_start_index=0,
        minimum_completion_index=3,
    )
    assert result[0] is None


def test_opposite_cisd_is_internal_until_active_protected_body_is_closed_through() -> None:
    rows = [
        bar("10:00", 101.0, 102.0, 98.0, 99.0),
        bar("10:03", 99.0, 104.0, 98.5, 103.0),
        bar("10:06", 102.0, 104.0, 101.0, 103.0),
        bar("10:09", 103.0, 104.0, 100.0, 101.0),
    ]
    events = detect_cisd_events(rows)
    lifecycle = build_cisd_structure_lifecycle(rows, events)
    assert lifecycle[0].lifecycle_state == "START"
    assert lifecycle[1].lifecycle_state == "INTERNAL_OPPOSITE"


def test_opposite_cisd_reverses_after_protected_body_close() -> None:
    rows = [
        bar("10:00", 101.0, 102.0, 98.0, 99.0),
        bar("10:03", 99.0, 104.0, 98.5, 103.0),
        bar("10:06", 103.0, 104.0, 97.0, 98.0),
    ]
    events = detect_cisd_events(rows)
    lifecycle = build_cisd_structure_lifecycle(rows, events)
    assert lifecycle[-1].lifecycle_state == "REVERSAL"
    assert lifecycle[-1].direction_change_qualified is True


def test_va_flip_cannot_bypass_htf_block_on_same_invalidation_candle() -> None:
    rows = [
        bar("09:30", 99.0, 99.5, 98.5, 99.0),
        bar("09:33", 99.0, 101.0, 98.8, 100.5),
        bar("09:36", 100.5, 101.0, 99.8, 100.2),
    ]
    controlling = HtfArrayLifecycle(
        "resistance",
        "fvg",
        "bearish",
        99.0,
        101.0,
        "2025-01-02T08:00:00-05:00",
        "RESPECTED",
        "RESISTANCE",
        (
            HtfTransition("RESPECTED", "2025-01-02T09:00:00-05:00", 98.5),
            HtfTransition("BODY_CLOSED", "2025-01-02T09:36:00-05:00", 100.2),
        ),
    )
    config = SymbolConfig("TEST", "3m", 0.25, 0.0, 0.0)
    context = build_premarket_context(rows, [], 100.0, 95.0, [], [], controlling, config)
    triggers = build_authoritative_context_triggers(
        rows,
        100.0,
        95.0,
        context,
        [controlling],
        config,
        ManualStateConfig(),
        prior_close=99.0,
    )
    assert not any(trigger.preferred_direction == "long" for trigger in triggers)


def test_liquidity_selector_prefers_boundary_distance_before_name_priority() -> None:
    rows = [bar("09:30", 100.0, 101.0, 99.0, 100.0)]
    levels = [
        LiquidityLevel("london_high", "high", 101.5),
        LiquidityLevel("swing_high_1", "high", 100.25),
    ]
    selection = build_liquidity_selection(rows, "AT_VAH", 100.0, 95.0, levels, [], 1.0)
    assert selection.selected_name == "swing_high_1"
    assert selection.requirement == "REQUIRED"


def test_farther_swept_liquidity_does_not_satisfy_closest_selected_level() -> None:
    rows = [bar("09:30", 100.0, 101.0, 99.0, 100.0)]
    closest = LiquidityLevel("swing_high_1", "high", 100.25)
    farther = LiquidityLevel("london_high", "high", 101.5)
    sweeps = [SweepEvent(0, farther, False, "opening_premarket")]
    selection = build_liquidity_selection(
        rows,
        "AT_VAH",
        100.0,
        95.0,
        [closest, farther],
        sweeps,
        1.0,
    )
    assert selection.selected_name == "swing_high_1"
    assert selection.sweep_state == "UNSWEPT"
    assert selection.requirement == "REQUIRED"


def test_context_authority_separates_block_unlock_and_direction_grant() -> None:
    rows = [bar("09:30", 100.0, 101.0, 99.0, 100.0)]
    controlling = HtfArrayLifecycle(
        "support",
        "fvg",
        "bullish",
        99.0,
        101.0,
        "2025-01-02T08:00:00-05:00",
        "RESPECTED",
        "SUPPORT",
        (HtfTransition("RESPECTED", "2025-01-02T09:00:00-05:00", 101.0),),
    )
    config = SymbolConfig("TEST", "3m", 0.5, 0.0, 0.0)
    context = build_premarket_context(rows, [], 100.0, 95.0, [], [], controlling, config)
    triggers = [
        ContextTrigger(1, "2025-01-02T09:33:00-05:00", "HTF_CONTROL_INVALIDATED", "", "HTF", "unlock"),
        ContextTrigger(2, "2025-01-02T09:36:00-05:00", "REPLACED_BY_VA_FLIP", "short", "VAL_FLIP", "grant short"),
    ]
    authority = build_context_authority_lifecycle(rows, context, triggers)
    assert [event.action for event in authority] == ["BLOCK", "UNLOCK", "GRANT"]


def test_neutral_unlock_does_not_end_active_direction_authority() -> None:
    triggers = [
        ContextTrigger(0, "2025-01-02T09:30:00-05:00", "REQUIRED_SATISFIED", "short", "PREMARKET", "short"),
        ContextTrigger(5, "2025-01-02T09:45:00-05:00", "HTF_CONTROL_INVALIDATED", "", "HTF", "unlock"),
        ContextTrigger(8, "2025-01-02T09:54:00-05:00", "REPLACED_BY_VA_FLIP", "long", "VA", "long"),
    ]
    assert next_context_change_index(triggers, 0, "short") == 8


def test_authority_aware_cisd_does_not_let_blocked_direction_own_structure() -> None:
    rows = [
        bar("09:30", 100.0, 101.0, 99.0, 100.0),
        bar("09:33", 101.0, 102.0, 98.0, 99.0),
        bar("09:36", 99.0, 103.0, 98.0, 102.0),
    ]
    events = [
        CisdEvent("short", 0, rows[0].time.isoformat(), 1, rows[1].time.isoformat(), 100.0, 101.0, 102.0, 1.0, 1.0),
        CisdEvent("long", 1, rows[1].time.isoformat(), 2, rows[2].time.isoformat(), 101.0, 99.0, 98.0, 1.0, 1.0),
    ]
    triggers = [
        ContextTrigger(0, rows[0].time.isoformat(), "REPLACED_BY_VA_FLIP", "long", "VA", "long"),
    ]
    lifecycle = build_authority_aware_cisd_structure_lifecycle(rows, events, triggers)
    assert [item.lifecycle_state for item in lifecycle] == ["BLOCKED_BY_CONTEXT", "START"]
    assert lifecycle[1].event.direction == "long"


def test_optional_liquidity_context_leaves_direction_to_structure() -> None:
    rows = [bar("09:30", 101.0, 102.0, 99.0, 101.0)]
    config = SymbolConfig("TEST", "3m", 0.5, 0.0, 0.0)
    context = build_premarket_context(rows, [], 100.0, 95.0, [], [], None, config)
    assert context.gate == "OPTIONAL_VA_LOCATION"
    assert context.preferred_direction == ""
    assert "first qualified structure chooses direction" in context.context_reason


def test_optional_structure_trigger_uses_first_qualified_cisd_direction() -> None:
    rows = [
        bar("09:30", 101.0, 102.0, 98.0, 99.0),
        bar("09:33", 99.0, 104.0, 98.5, 103.0),
    ]
    qualifications = build_cisd_structure_lifecycle(rows, detect_cisd_events(rows))
    triggers = add_optional_structure_trigger([], qualifications)
    assert len(triggers) == 1
    assert triggers[0].gate == "OPTIONAL_FIRST_STRUCTURE"
    assert triggers[0].preferred_direction == "long"


def test_optional_structure_allows_qualified_protected_body_reversal() -> None:
    rows = [
        bar("10:00", 101.0, 102.0, 98.0, 99.0),
        bar("10:03", 99.0, 104.0, 98.5, 103.0),
        bar("10:06", 103.0, 104.0, 97.0, 98.0),
    ]
    qualifications = build_cisd_structure_lifecycle(rows, detect_cisd_events(rows))
    triggers = add_optional_structure_trigger([], qualifications)
    assert [(item.gate, item.preferred_direction) for item in triggers] == [
        ("OPTIONAL_FIRST_STRUCTURE", "long"),
        ("OPTIONAL_STRUCTURE_REVERSAL", "short"),
    ]


def test_same_direction_context_grants_collapse_to_one_thesis_authority() -> None:
    triggers = [
        ContextTrigger(0, "2025-01-02T09:30:00-05:00", "REQUIRED_SATISFIED", "short", "PREMARKET", "short"),
        ContextTrigger(3, "2025-01-02T09:39:00-05:00", "REPLACED_BY_VA_FLIP", "short", "VA", "short"),
        ContextTrigger(4, "2025-01-02T09:42:00-05:00", "HTF_CONTROL_INVALIDATED", "", "HTF", "unlock"),
        ContextTrigger(6, "2025-01-02T09:48:00-05:00", "REPLACED_BY_VA_FLIP", "long", "VA", "long"),
    ]
    collapsed = collapse_directional_context_triggers(triggers)
    assert [(item.index, item.preferred_direction) for item in collapsed] == [(0, "short"), (6, "long")]
