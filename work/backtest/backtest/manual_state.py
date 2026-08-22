from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date
from enum import Enum
from hashlib import sha256
from typing import Iterable

import pandas as pd

from .config import SymbolConfig
from .data_inspector import parse_timeframe_minutes
from .integrity import assess_manual_state_day
from .strategy import (
    HtfArray,
    LiquidityLevel,
    SweepEvent,
    allowed_trade_date,
    apply_entry_cost,
    blocked_by_first30_range_filter,
    blocked_by_swing_first30_directionality,
    build_liquidity_context,
    build_session_liquidity_levels,
    build_swing_liquidity_levels,
    compute_first30_directionality,
    compute_first30_range,
    detect_htf_arrays,
    detect_sweep,
    find_entry_resolution,
    find_fvg_or_ifvg,
    liquidity_priority,
    near_value_area,
    result_to_manual_outcome,
    result_to_terminal_reason,
    round_price_to_tick,
    simulate_exit,
    time_slice,
    time_slice_from,
    timestamp_on_day,
)
from .volume_profile import compute_volume_profile


@dataclass(frozen=True)
class ManualStateConfig:
    trade_window_start: str = "09:30"
    trade_window_end: str = "12:00"
    cisd_anchor_lookback: int = 6
    fvg_window_candles: int = 5
    htf_timeframe_minutes: int = 15
    enable_htf_order_blocks: bool = True
    enable_va_flip_gate: bool = True
    enable_htf_optional_gate: bool = True
    allow_ny_htf_reaction_gate: bool = False
    allow_opposite_cisd_reversal_without_context: bool = False
    cancel_entry_on_terminal_candle: bool = True
    cisd_qualification_mode: str = "protected_body"
    target_before_fill_mode: str = "synthetic_r"
    enforce_symbol_config_envelope: bool = True
    reject_invalid_data: bool = True
    intrabar_ambiguity_policy: str = "reject"


class DataState(str, Enum):
    UNKNOWN = "UNKNOWN"
    VALID = "VALID"
    INVALID = "INVALID"


class PipelineStatus(str, Enum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    INFO = "INFO"


class SetupState(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    NOT_FOUND = "NOT_FOUND"


class OrderState(str, Enum):
    NOT_PLACED = "NOT_PLACED"
    CANCELLED = "CANCELLED"
    FILLED = "FILLED"


class FinalDecision(str, Enum):
    TAKE = "TAKE"
    SKIP = "SKIP"


@dataclass(frozen=True)
class CisdEvent:
    direction: str
    anchor_index: int
    anchor_time: str
    confirm_index: int
    confirm_time: str
    body_level: float
    body_stop: float
    invalidation_level: float
    displacement_points: float
    displacement_body_ratio: float
    confirm_known_time: str = ""


@dataclass(frozen=True)
class CisdQualification:
    event: CisdEvent
    lifecycle_state: str
    active_direction_before: str
    protected_body_before: float | None
    confirm_close: float
    direction_change_qualified: bool
    reason: str


@dataclass
class ThesisEpisode:
    episode_id: int
    direction: str
    cisd: CisdEvent
    status: str = "ACTIVE"
    terminal_reason: str = ""
    terminal_index: int | None = None
    terminal_time: str = ""
    authority_gate: str = ""
    authority_source: str = ""
    authority_time: str = ""
    authority_index: int = 0
    cisd_lifecycle_state: str = ""
    thesis_id: str = ""


@dataclass(frozen=True)
class HtfTransition:
    state: str
    time: str
    close: float


@dataclass(frozen=True)
class HtfArrayLifecycle:
    array_id: str
    array_type: str
    direction: str
    lower: float
    upper: float
    known_time: str
    current_state: str
    role: str
    transitions: tuple[HtfTransition, ...]


@dataclass(frozen=True)
class ContextTrigger:
    index: int
    time: str
    gate: str
    preferred_direction: str
    source: str
    liquidity_context: str
    htf_array_id: str = ""
    htf_array_state: str = ""
    event_id: str = ""
    known_time: str = ""


@dataclass(frozen=True)
class PipelineStageRecord:
    stage: str
    status: str
    reason: str
    known_time: str = ""


@dataclass(frozen=True)
class PremarketContextState:
    asof_time: str
    opening_location: str
    gate: str
    preferred_direction: str
    blocked_direction: str
    controlling_array_id: str
    controlling_array_type: str
    controlling_array_direction: str
    controlling_array_state: str
    controlling_array_role: str
    controlling_decision_role: str
    selected_liquidity_name: str
    selected_liquidity_side: str
    selected_liquidity_price: float | None
    context_reason: str


@dataclass(frozen=True)
class LiquiditySelectionState:
    asof_time: str
    opening_location: str
    active_boundary_name: str
    active_boundary_price: float
    desired_side: str
    candidate_names: tuple[str, ...]
    candidate_prices: tuple[float, ...]
    selected_name: str
    selected_side: str
    selected_price: float | None
    sweep_state: str
    requirement: str
    reason: str


@dataclass(frozen=True)
class ContextAuthorityEvent:
    index: int
    time: str
    action: str
    direction: str
    source: str
    gate: str
    replaces_direction: str
    reason: str


@dataclass(frozen=True)
class ManualStateDecision:
    symbol: str
    timeframe: str
    date: str
    setup_state: str
    order_state: str
    final_decision: str
    direction: str
    outcome: str
    terminal_reason: str
    terminal_time: str
    context_gate: str
    context_source: str
    liquidity_context: str
    htf_array_id: str
    htf_array_state: str
    thesis_episode_id: int
    cisd_anchor_time: str
    cisd_time: str
    cisd_body_level: float
    cisd_invalidation_level: float
    cisd_displacement_points: float
    fvg_time: str
    fvg_kind: str
    entry_time: str
    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    exit_price: float | None = None
    r_multiple: float | None = None
    event_id: str = ""
    thesis_id: str = ""
    order_id: str = ""
    intrabar_ambiguity: str = ""
    trace: str = ""
    event_known_time: str = ""
    cisd_known_time: str = ""
    fvg_known_time: str = ""
    entry_known_time: str = ""
    terminal_known_time: str = ""


@dataclass
class ManualStateDayResult:
    trade_date: str
    vah: float | None
    val: float | None
    premarket_context: PremarketContextState | None = None
    liquidity_selection: LiquiditySelectionState | None = None
    cisd_events: list[CisdEvent] = field(default_factory=list)
    cisd_qualifications: list[CisdQualification] = field(default_factory=list)
    thesis_episodes: list[ThesisEpisode] = field(default_factory=list)
    htf_arrays: list[HtfArrayLifecycle] = field(default_factory=list)
    context_triggers: list[ContextTrigger] = field(default_factory=list)
    context_authority: list[ContextAuthorityEvent] = field(default_factory=list)
    decisions: list[ManualStateDecision] = field(default_factory=list)
    data_state: str = DataState.UNKNOWN.value
    data_reasons: tuple[str, ...] = ()
    pipeline: list[PipelineStageRecord] = field(default_factory=list)
    effective_config: dict[str, object] = field(default_factory=dict)


@dataclass
class ManualStateBacktestResult:
    days: list[ManualStateDayResult]


def detect_cisd_events(
    rows: list,
    start_index: int = 0,
    end_index: int | None = None,
    anchor_lookback: int = 6,
) -> list[CisdEvent]:
    """Emit explainable CISD events without choosing a trading direction.

    A bullish event is a bullish body close above the body high of the most
    recent bearish anchor. A bearish event is the mirror image. Each anchor can
    confirm only once; all quality measurements are emitted instead of hidden
    behind an optimized threshold.
    """
    if not rows:
        return []
    stop = len(rows) if end_index is None else min(len(rows), end_index + 1)
    confirmed_anchors: set[tuple[str, int]] = set()
    events: list[CisdEvent] = []
    bar_duration = infer_bar_duration(rows)

    for confirm_index in range(max(1, start_index), stop):
        confirm = rows[confirm_index]
        confirm_open = float(confirm.open)
        confirm_close = float(confirm.close)
        if confirm_close == confirm_open:
            continue

        direction = "long" if confirm_close > confirm_open else "short"
        anchor_index = most_recent_opposite_body(
            rows,
            confirm_index,
            direction,
            max(start_index, confirm_index - anchor_lookback),
        )
        if anchor_index is None or (direction, anchor_index) in confirmed_anchors:
            continue
        anchor = rows[anchor_index]
        body_low = min(float(anchor.open), float(anchor.close))
        body_high = max(float(anchor.open), float(anchor.close))
        body_level = body_high if direction == "long" else body_low
        confirmed = confirm_close > body_level if direction == "long" else confirm_close < body_level
        if not confirmed:
            continue

        confirm_body = abs(confirm_close - confirm_open)
        recent_bodies = [
            abs(float(row.close) - float(row.open))
            for row in rows[max(start_index, confirm_index - anchor_lookback) : confirm_index]
            if float(row.close) != float(row.open)
        ]
        median_body = float(pd.Series(recent_bodies).median()) if recent_bodies else 0.0
        body_ratio = confirm_body / median_body if median_body > 0 else 0.0
        displacement = confirm_close - body_level if direction == "long" else body_level - confirm_close
        invalidation = float(anchor.low) if direction == "long" else float(anchor.high)
        body_stop = body_low if direction == "long" else body_high
        events.append(
            CisdEvent(
                direction=direction,
                anchor_index=anchor_index,
                anchor_time=anchor.time.isoformat(),
                confirm_index=confirm_index,
                confirm_time=confirm.time.isoformat(),
                body_level=round(body_level, 6),
                body_stop=round(body_stop, 6),
                invalidation_level=round(invalidation, 6),
                displacement_points=round(displacement, 6),
                displacement_body_ratio=round(body_ratio, 4),
                confirm_known_time=(confirm.time + bar_duration).isoformat(),
            )
        )
        confirmed_anchors.add((direction, anchor_index))

    return events


def infer_bar_duration(rows: list) -> pd.Timedelta:
    diffs = [
        rows[index].time - rows[index - 1].time
        for index in range(1, len(rows))
        if rows[index].time > rows[index - 1].time
        and rows[index].time - rows[index - 1].time <= pd.Timedelta(minutes=60)
    ]
    if not diffs:
        return pd.Timedelta(0)
    return pd.Series(diffs).median()


def most_recent_opposite_body(
    rows: list,
    confirm_index: int,
    direction: str,
    lookback_start: int,
) -> int | None:
    for index in range(confirm_index - 1, lookback_start - 1, -1):
        candle = rows[index]
        bearish = float(candle.close) < float(candle.open)
        bullish = float(candle.close) > float(candle.open)
        if direction == "long" and bearish:
            return index
        if direction == "short" and bullish:
            return index
    return None


def build_cisd_structure_lifecycle(
    rows: list,
    events: Iterable[CisdEvent],
) -> list[CisdQualification]:
    """Classify raw CISDs against the protected body of active structure.

    An opposite mathematical CISD is internal until its confirmation close
    crosses the active CISD origin body.  This is a structural lifecycle rule,
    not a displacement threshold.  Same-direction CISD requalifies the active
    protected body.
    """
    output: list[CisdQualification] = []
    active: CisdEvent | None = None
    for event in sorted(events, key=lambda item: item.confirm_index):
        confirm_close = float(rows[event.confirm_index].close)
        active_direction = "" if active is None else active.direction
        protected_body = None if active is None else active.body_stop
        if active is None:
            state = "START"
            qualified = True
            reason = "first observed CISD establishes structure"
            active = event
        elif event.direction == active.direction:
            state = "REQUALIFIED"
            qualified = False
            reason = "same-direction CISD updates the protected origin body"
            active = event
        else:
            crossed = (
                confirm_close < active.body_stop
                if active.direction == "long"
                else confirm_close > active.body_stop
            )
            if crossed:
                state = "REVERSAL"
                qualified = True
                reason = "opposite CISD body-close crossed the active protected body"
                active = event
            else:
                state = "INTERNAL_OPPOSITE"
                qualified = False
                reason = "opposite CISD did not body-close through the active protected body"
        output.append(
            CisdQualification(
                event=event,
                lifecycle_state=state,
                active_direction_before=active_direction,
                protected_body_before=protected_body,
                confirm_close=round(confirm_close, 6),
                direction_change_qualified=qualified,
                reason=reason,
            )
        )
    return output


def build_authority_aware_cisd_structure_lifecycle(
    rows: list,
    events: Iterable[CisdEvent],
    triggers: Iterable[ContextTrigger],
) -> list[CisdQualification]:
    """Classify CISD structure without letting an unauthorized side own state.

    Directional context grants define which side may establish the protected
    body. A conflicting CISD can still invalidate an active structure when it
    closes through the protected body, but it cannot become the new controlling
    structure until context grants that direction.
    """
    output: list[CisdQualification] = []
    active: CisdEvent | None = None
    active_authority = ""
    directional = collapse_directional_context_triggers(triggers)
    trigger_number = 0

    for event in sorted(events, key=lambda item: item.confirm_index):
        while (
            trigger_number < len(directional)
            and directional[trigger_number].index <= event.confirm_index
        ):
            granted = directional[trigger_number].preferred_direction
            if granted != active_authority:
                active_authority = granted
                active = None
            trigger_number += 1

        confirm_close = float(rows[event.confirm_index].close)
        active_direction = "" if active is None else active.direction
        protected_body = None if active is None else active.body_stop
        qualified = False

        if not active_authority:
            state = "BEFORE_CONTEXT_AUTHORITY"
            reason = "CISD occurred before any directional context authority"
        elif event.direction == active_authority:
            if active is None:
                state = "START"
                qualified = True
                reason = "first context-authorized CISD establishes structure"
            else:
                state = "REQUALIFIED"
                reason = "same-direction context-authorized CISD updates the protected origin body"
            active = event
        elif active is None:
            state = "BLOCKED_BY_CONTEXT"
            reason = "CISD direction is not authorized and cannot establish protected structure"
        else:
            crossed = (
                confirm_close < active.body_stop
                if active.direction == "long"
                else confirm_close > active.body_stop
            )
            if crossed:
                state = "REVERSAL"
                qualified = True
                reason = (
                    "opposite CISD invalidated the active protected body but cannot "
                    "control structure without matching context authority"
                )
                active = None
            else:
                state = "INTERNAL_OPPOSITE"
                reason = "opposite CISD did not body-close through the active protected body"

        output.append(
            CisdQualification(
                event=event,
                lifecycle_state=state,
                active_direction_before=active_direction,
                protected_body_before=protected_body,
                confirm_close=round(confirm_close, 6),
                direction_change_qualified=qualified,
                reason=reason,
            )
        )
    return output


def qualification_is_actionable(qualification: CisdQualification) -> bool:
    return qualification.lifecycle_state not in {
        "INTERNAL_OPPOSITE",
        "BLOCKED_BY_CONTEXT",
        "BEFORE_CONTEXT_AUTHORITY",
    }


def build_thesis_episodes(
    events: Iterable[CisdEvent],
    preferred_direction: str,
    allow_opposite_reversal_without_context: bool = False,
) -> list[ThesisEpisode]:
    episodes: list[ThesisEpisode] = []
    current: ThesisEpisode | None = None
    for event in sorted(events, key=lambda item: item.confirm_index):
        if current is None:
            if event.direction != preferred_direction:
                continue
            current = ThesisEpisode(len(episodes) + 1, event.direction, event)
            episodes.append(current)
            continue

        if event.direction == current.direction:
            current.status = "REQUALIFIED"
            current.terminal_reason = "SAME_DIRECTION_CISD_REQUALIFICATION"
        else:
            current.status = "INVALIDATED"
            current.terminal_reason = "OPPOSITE_CISD"
        current.terminal_index = event.confirm_index
        current.terminal_time = event.confirm_time
        if event.direction != preferred_direction and not allow_opposite_reversal_without_context:
            current = None
            continue
        current = ThesisEpisode(len(episodes) + 1, event.direction, event)
        episodes.append(current)
    return episodes


def build_independent_htf_frame(frame: pd.DataFrame, source_timeframe: str, minutes: int = 15) -> pd.DataFrame:
    if frame.empty:
        return frame.iloc[0:0].copy()
    source_minutes = int(parse_timeframe_minutes(source_timeframe) or 0)
    if source_minutes <= 0 or minutes % source_minutes != 0:
        return frame.iloc[0:0].copy()
    expected = minutes // source_minutes
    result = frame.set_index("time").resample(f"{minutes}min", label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        candle_count=("close", "count"),
    )
    result = result.dropna(subset=["open", "high", "low", "close"])
    return result[result["candle_count"] == expected].reset_index()


def build_htf_array_lifecycles(
    htf_frame: pd.DataFrame,
    asof: pd.Timestamp,
    include_order_blocks: bool = True,
    htf_minutes: int = 15,
) -> list[HtfArrayLifecycle]:
    selected = htf_frame[htf_frame["time"] + pd.Timedelta(minutes=htf_minutes) <= asof].reset_index(drop=True)
    arrays = detect_manual_htf_arrays(selected, include_order_blocks, htf_minutes)
    return [evaluate_htf_array(array, selected, asof, number, htf_minutes) for number, array in enumerate(arrays, 1)]


def detect_manual_htf_arrays(
    frame: pd.DataFrame,
    include_order_blocks: bool = True,
    htf_minutes: int = 15,
) -> list[HtfArray]:
    """Detect HTF FVGs and displacement-backed order blocks.

    An order block is not any opposite candle followed by a break. It must be
    the opposite candle at the origin of a three-candle displacement that also
    leaves an FVG. This keeps the OB vocabulary tied to observable delivery.
    """
    rows = list(frame.itertuples(index=False))
    arrays: list[HtfArray] = []
    for index in range(len(rows) - 2):
        first, displacement, third = rows[index], rows[index + 1], rows[index + 2]
        known_time = third.time + pd.Timedelta(minutes=htf_minutes)
        bullish_fvg = float(first.high) < float(third.low)
        bearish_fvg = float(first.low) > float(third.high)
        if bullish_fvg:
            arrays.append(HtfArray("fvg", "bullish", float(first.high), float(third.low), known_time))
        if bearish_fvg:
            arrays.append(HtfArray("fvg", "bearish", float(third.high), float(first.low), known_time))
        if not include_order_blocks:
            continue
        origin_bearish = float(first.close) < float(first.open)
        origin_bullish = float(first.close) > float(first.open)
        bullish_displacement = (
            float(displacement.close) > float(displacement.open)
            and float(displacement.close) > float(first.high)
        )
        bearish_displacement = (
            float(displacement.close) < float(displacement.open)
            and float(displacement.close) < float(first.low)
        )
        if origin_bearish and bullish_displacement and bullish_fvg:
            arrays.append(
                HtfArray(
                    "order_block",
                    "bullish",
                    min(float(first.open), float(first.close)),
                    max(float(first.open), float(first.close)),
                    known_time,
                )
            )
        if origin_bullish and bearish_displacement and bearish_fvg:
            arrays.append(
                HtfArray(
                    "order_block",
                    "bearish",
                    min(float(first.open), float(first.close)),
                    max(float(first.open), float(first.close)),
                    known_time,
                )
            )
    return arrays


def evaluate_htf_array(
    array: HtfArray,
    htf_frame: pd.DataFrame,
    asof: pd.Timestamp,
    number: int,
    htf_minutes: int = 15,
) -> HtfArrayLifecycle:
    state = "FRESH"
    transitions: list[HtfTransition] = []
    phase = "ORIGINAL_ACTIVE"
    rows = htf_frame[htf_frame["time"] + pd.Timedelta(minutes=htf_minutes) > array.known_time]
    rows = rows[rows["time"] + pd.Timedelta(minutes=htf_minutes) <= asof]
    for candle in rows.itertuples(index=False):
        close_time = candle.time + pd.Timedelta(minutes=htf_minutes)
        touched = float(candle.high) >= array.lower and float(candle.low) <= array.upper
        closed_through = (
            float(candle.close) < array.lower if array.direction == "bullish" else float(candle.close) > array.upper
        )
        next_state = state
        if phase == "ORIGINAL_ACTIVE":
            if closed_through:
                next_state = "BODY_CLOSED"
                phase = "AWAITING_FLIP_REACTION"
            elif touched:
                respected = (
                    float(candle.close) >= array.upper
                    if array.direction == "bullish"
                    else float(candle.close) <= array.lower
                )
                next_state = "RESPECTED" if respected else "TAPPED"
        elif phase == "AWAITING_FLIP_REACTION":
            reclaimed_original_side = (
                float(candle.close) > array.upper
                if array.direction == "bullish"
                else float(candle.close) < array.lower
            )
            if reclaimed_original_side:
                next_state = "RECLAIMED_INVALIDATED"
                phase = "TERMINAL"
            elif touched and closed_through:
                next_state = "FLIPPED_RESISTANCE" if array.direction == "bullish" else "FLIPPED_SUPPORT"
                phase = "FLIPPED_ACTIVE"
        elif phase == "FLIPPED_ACTIVE":
            flip_invalidated = (
                float(candle.close) > array.upper
                if array.direction == "bullish"
                else float(candle.close) < array.lower
            )
            if flip_invalidated:
                next_state = "FLIP_INVALIDATED"
                phase = "TERMINAL"
        if next_state != state:
            transitions.append(HtfTransition(next_state, close_time.isoformat(), round(float(candle.close), 6)))
            state = next_state

    role = htf_role(array.direction, state)
    known_label = array.known_time.strftime("%Y%m%d_%H%M")
    return HtfArrayLifecycle(
        array_id=f"HTF15_{array.array_type}_{array.direction}_{known_label}_{number}",
        array_type=array.array_type,
        direction=array.direction,
        lower=round(array.lower, 6),
        upper=round(array.upper, 6),
        known_time=array.known_time.isoformat(),
        current_state=state,
        role=role,
        transitions=tuple(transitions),
    )


def htf_role(direction: str, state: str) -> str:
    if state == "FLIPPED_RESISTANCE":
        return "RESISTANCE"
    if state == "FLIPPED_SUPPORT":
        return "SUPPORT"
    if state in {"BODY_CLOSED", "RECLAIMED_INVALIDATED", "FLIP_INVALIDATED"}:
        return "INVALIDATED"
    if direction == "bullish":
        return "SUPPORT"
    return "RESISTANCE"


def select_controlling_array(
    arrays: Iterable[HtfArrayLifecycle],
    vah: float,
    val: float,
    tolerance: float,
    reference_level: float | None = None,
) -> HtfArrayLifecycle | None:
    levels = [reference_level] if reference_level is not None else [vah, val]
    candidates = [
        array
        for array in arrays
        if array.role != "INVALIDATED"
        and htf_array_has_reaction(array)
        and any(array.lower - tolerance <= level <= array.upper + tolerance for level in levels)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            array_level_distance(item, reference_level)
            if reference_level is not None
            else array_value_distance(item, vah, val),
            -pd.Timestamp(item.known_time).value,
        ),
    )


def array_value_distance(array: HtfArrayLifecycle, vah: float, val: float) -> float:
    def distance(level: float) -> float:
        if array.lower <= level <= array.upper:
            return 0.0
        return min(abs(level - array.lower), abs(level - array.upper))

    return min(distance(vah), distance(val))


def array_level_distance(array: HtfArrayLifecycle, level: float) -> float:
    if array.lower <= level <= array.upper:
        return 0.0
    return min(abs(level - array.lower), abs(level - array.upper))


def htf_array_state_asof(array: HtfArrayLifecycle, asof: pd.Timestamp) -> tuple[str, str]:
    state = "FRESH"
    for transition in array.transitions:
        if pd.Timestamp(transition.time) > asof:
            break
        state = transition.state
    return state, htf_role(array.direction, state)


HTF_REACTION_STATES = {"RESPECTED", "FLIPPED_RESISTANCE", "FLIPPED_SUPPORT"}


def htf_array_has_reaction(array: HtfArrayLifecycle, asof: pd.Timestamp | None = None) -> bool:
    state = "FRESH"
    for transition in array.transitions:
        if asof is not None and pd.Timestamp(transition.time) > asof:
            break
        state = transition.state
    return state in HTF_REACTION_STATES


def detect_va_flip_triggers(
    rows: list,
    vah: float,
    val: float,
    tolerance: float,
    prior_close: float | None = None,
) -> list[ContextTrigger]:
    if not rows:
        return []
    triggers: list[ContextTrigger] = []
    vah_cross_index: int | None = None
    val_cross_index: int | None = None
    vah_emitted = False
    val_emitted = False
    for index, candle in enumerate(rows):
        close = float(candle.close)
        previous_close = prior_close if index == 0 else float(rows[index - 1].close)
        if previous_close is not None and previous_close < vah <= close:
            vah_cross_index = index
        if previous_close is not None and previous_close > val >= close:
            val_cross_index = index
        if (
            vah_cross_index is not None
            and index > vah_cross_index
            and not vah_emitted
            and float(candle.low) <= vah + tolerance
            and close >= vah
        ):
            triggers.append(
                ContextTrigger(
                    index,
                    candle.time.isoformat(),
                    "REPLACED_BY_VA_FLIP",
                    "long",
                    "VAH_SUPPORT_FLIP",
                    "VAH resistance-to-support flip",
                )
            )
            vah_emitted = True
        if (
            val_cross_index is not None
            and index > val_cross_index
            and not val_emitted
            and float(candle.high) >= val - tolerance
            and close <= val
        ):
            triggers.append(
                ContextTrigger(
                    index,
                    candle.time.isoformat(),
                    "REPLACED_BY_VA_FLIP",
                    "short",
                    "VAL_RESISTANCE_FLIP",
                    "VAL support-to-resistance flip",
                )
            )
            val_emitted = True
    return triggers


def opening_va_location(open_price: float, vah: float, val: float, tolerance: float) -> str:
    if abs(open_price - vah) <= tolerance:
        return "AT_VAH"
    if abs(open_price - val) <= tolerance:
        return "AT_VAL"
    if open_price > vah:
        return "ABOVE_VAH"
    if open_price < val:
        return "BELOW_VAL"
    return "INSIDE_NEAR_VAH" if abs(open_price - vah) <= abs(open_price - val) else "INSIDE_NEAR_VAL"


def direction_and_side_for_opening(location: str) -> tuple[str, str, float]:
    if location in {"AT_VAH", "ABOVE_VAH", "INSIDE_NEAR_VAH"}:
        return "short", "high", 1.0
    return "long", "low", -1.0


def select_opening_liquidity(
    levels: Iterable[LiquidityLevel],
    location: str,
    vah: float,
    val: float,
    tolerance: float,
) -> LiquidityLevel | None:
    _, desired_side, _ = direction_and_side_for_opening(location)
    boundary = vah if desired_side == "high" else val
    candidates = [
        level
        for level in levels
        if level.side == desired_side and abs(level.price - boundary) <= tolerance * 2
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda level: (abs(level.price - boundary), liquidity_priority(level.name)))


def build_liquidity_selection(
    rows: list,
    location: str,
    vah: float,
    val: float,
    liquidity_levels: list[LiquidityLevel],
    opening_sweeps: list[SweepEvent],
    tolerance: float,
) -> LiquiditySelectionState:
    location_direction, desired_side, _ = direction_and_side_for_opening(location)
    boundary_name = "VAH" if desired_side == "high" else "VAL"
    boundary = vah if desired_side == "high" else val
    candidates = sorted(
        [
            level
            for level in liquidity_levels
            if level.side == desired_side and abs(level.price - boundary) <= tolerance * 2
        ],
        key=lambda level: (abs(level.price - boundary), liquidity_priority(level.name)),
    )
    swept = sorted(
        [
            sweep.level
            for sweep in opening_sweeps
            if sweep.level.side == desired_side and abs(sweep.level.price - boundary) <= tolerance * 2
        ],
        key=lambda level: (abs(level.price - boundary), liquidity_priority(level.name)),
    )
    selected = candidates[0] if candidates else None
    selected_was_swept = selected is not None and any(level.name == selected.name for level in swept)
    if selected_was_swept:
        sweep_state = "SWEPT_PREMARKET"
        requirement = "SATISFIED"
        reason = "closest active-boundary named liquidity was swept before NY open"
    elif selected is not None:
        sweep_state = "UNSWEPT"
        requirement = "REQUIRED"
        reason = "closest untaken named liquidity at active VA boundary requires a sweep"
    else:
        sweep_state = "NOT_APPLICABLE"
        requirement = "OPTIONAL"
        reason = f"no untaken {desired_side}-side named liquidity near {boundary_name}"
    return LiquiditySelectionState(
        asof_time=rows[0].time.isoformat(),
        opening_location=location,
        active_boundary_name=boundary_name,
        active_boundary_price=round(boundary, 6),
        desired_side=desired_side,
        candidate_names=tuple(level.name for level in candidates),
        candidate_prices=tuple(round(level.price, 6) for level in candidates),
        selected_name="" if selected is None else selected.name,
        selected_side="" if selected is None else selected.side,
        selected_price=None if selected is None else round(selected.price, 6),
        sweep_state=sweep_state,
        requirement=requirement,
        reason=f"{reason}; opening location implies {location_direction}",
    )


def build_selection_liquidity_context(
    frame: pd.DataFrame,
    trade_date: date,
    trade_start: pd.Timestamp,
    config: SymbolConfig,
) -> tuple[list[LiquidityLevel], list[SweepEvent]]:
    """Build the full pre-open candidate universe before taken-level filtering.

    The active strategy helper intentionally returns only one opening sweep.
    Manual selection needs to compare every named level near the active VA
    boundary first, then ask whether that specific level was swept recently.
    """
    levels = build_session_liquidity_levels(frame, trade_date)
    if not config.session_liquidity_only:
        levels.extend(build_swing_liquidity_levels(frame, trade_start, config))
    unique = {level.name: level for level in levels}
    levels = list(unique.values())
    if config.opening_premarket_sweep_mode != "recent_as_setup":
        return levels, []
    recent_start = timestamp_on_day(trade_date, config.opening_premarket_sweep_start)
    recent = time_slice(frame, recent_start, trade_start)
    if recent.empty:
        return levels, []
    swept = [
        SweepEvent(0, level, False, "opening_premarket")
        for level in levels
        if (level.side == "high" and bool((recent["high"] >= level.price).any()))
        or (level.side == "low" and bool((recent["low"] <= level.price).any()))
    ]
    return levels, swept


def build_premarket_context(
    rows: list,
    premarket_rows: list,
    vah: float,
    val: float,
    liquidity_levels: list[LiquidityLevel],
    opening_sweeps: list[SweepEvent],
    controlling: HtfArrayLifecycle | None,
    config: SymbolConfig,
    liquidity_selection: LiquiditySelectionState | None = None,
) -> PremarketContextState:
    asof_time = rows[0].time.isoformat()
    location = opening_va_location(float(rows[0].open), vah, val, config.vah_val_tolerance)
    location_direction, desired_side, _ = direction_and_side_for_opening(location)
    selection = liquidity_selection or build_liquidity_selection(
        rows,
        location,
        vah,
        val,
        liquidity_levels,
        opening_sweeps,
        config.vah_val_tolerance,
    )
    selected = (
        None
        if not selection.selected_name
        else LiquidityLevel(selection.selected_name, selection.selected_side, float(selection.selected_price))
    )

    controlling_fields = {
        "controlling_array_id": "" if controlling is None else controlling.array_id,
        "controlling_array_type": "" if controlling is None else controlling.array_type,
        "controlling_array_direction": "" if controlling is None else controlling.direction,
        "controlling_array_state": "" if controlling is None else controlling.current_state,
        "controlling_array_role": "" if controlling is None else controlling.role,
        # A reacted array is proven as a constraint, but not yet proven as a
        # standalone direction generator.  A stronger decision role must be
        # learned from repeated manual examples rather than assumed here.
        "controlling_decision_role": "" if controlling is None else "BLOCK_ONLY",
    }
    blocked = ""
    if controlling is not None:
        blocked = "short" if controlling.role == "SUPPORT" else "long"

    def make_context(
        gate: str,
        preferred: str,
        reason: str,
        liquidity: LiquidityLevel | None = selected,
    ) -> PremarketContextState:
        if preferred and preferred == blocked:
            gate = "BLOCKED_HTF"
            reason = (
                f"{reason}; premarket reacted {controlling.array_type} "
                f"{controlling.role.lower()} blocks {preferred}"
            )
            preferred = ""
        return PremarketContextState(
            asof_time=asof_time,
            opening_location=location,
            gate=gate,
            preferred_direction=preferred,
            blocked_direction=blocked,
            **controlling_fields,
            selected_liquidity_name="" if liquidity is None else liquidity.name,
            selected_liquidity_side="" if liquidity is None else liquidity.side,
            selected_liquidity_price=None if liquidity is None else round(liquidity.price, 6),
            context_reason=reason,
        )

    if selection.sweep_state == "SWEPT_PREMARKET" and selected is not None:
        preferred = "short" if selected.side == "high" else "long"
        return make_context(
            "REQUIRED_SATISFIED",
            preferred,
            "selected liquidity was swept during premarket",
            selected,
        )

    if selection.requirement == "REQUIRED" and selected is not None:
        return make_context(
            "REQUIRED",
            location_direction,
            "nearby selected liquidity must be swept before structure",
        )

    if controlling is not None:
        return make_context(
            "HTF_BLOCK_ONLY",
            "",
            f"premarket reacted {controlling.array_type} is retained only as a {blocked} blocker",
            None,
        )
    return make_context(
        "OPTIONAL_VA_LOCATION",
        "",
        (
            "no reacted HTF array and no untaken selected liquidity near value boundary; "
            "first qualified structure chooses direction"
        ),
        None,
    )


def detect_premarket_va_reaction(
    rows: list,
    opening_location: str,
    vah: float,
    val: float,
    tolerance: float,
) -> tuple[str, str] | None:
    """Describe the latest VA touch for audit only; it is not an authority gate.

    A single touch-and-close is common and did not separate the first ten manual
    decisions.  Repeated-support/accumulation remains a WATCH feature until it
    has more labelled examples.
    """
    if not rows:
        return None
    use_vah = opening_location in {"AT_VAH", "ABOVE_VAH", "INSIDE_NEAR_VAH"}
    boundary = vah if use_vah else val
    touched = [
        candle
        for candle in rows
        if float(candle.high) >= boundary - tolerance and float(candle.low) <= boundary + tolerance
    ]
    if not touched:
        return None
    last = touched[-1]
    if use_vah:
        return ("VAH_SUPPORT", "long") if float(last.close) >= vah else ("VAH_RESISTANCE", "short")
    return ("VAL_SUPPORT", "long") if float(last.close) >= val else ("VAL_RESISTANCE", "short")


def build_authoritative_context_triggers(
    rows: list,
    vah: float,
    val: float,
    context: PremarketContextState,
    full_htf_arrays: list[HtfArrayLifecycle],
    config: SymbolConfig,
    state_config: ManualStateConfig,
    prior_close: float | None = None,
) -> list[ContextTrigger]:
    triggers: list[ContextTrigger] = []
    if context.gate != "REQUIRED" and context.preferred_direction:
        triggers.append(
            ContextTrigger(
                0,
                rows[0].time.isoformat(),
                context.gate,
                context.preferred_direction,
                "PREMARKET_CONTEXT",
                context.context_reason,
                context.controlling_array_id,
                context.controlling_array_state,
            )
        )

    if context.gate == "REQUIRED" and context.selected_liquidity_price is not None:
        for index, candle in enumerate(rows):
            swept = (
                float(candle.high) >= context.selected_liquidity_price
                if context.selected_liquidity_side == "high"
                else float(candle.low) <= context.selected_liquidity_price
            )
            if not swept:
                continue
            triggers.append(
                ContextTrigger(
                    index,
                    candle.time.isoformat(),
                    "REQUIRED_SATISFIED",
                    context.preferred_direction,
                    "SELECTED_LIQUIDITY_SWEEP",
                    f"{context.selected_liquidity_name} {context.selected_liquidity_side} sweep near VA",
                )
            )
            break

    controlling_invalidation_index = controlling_array_invalidation_index(
        rows,
        context.controlling_array_id,
        full_htf_arrays,
    )
    if controlling_invalidation_index is not None:
        candle = rows[controlling_invalidation_index]
        triggers.append(
            ContextTrigger(
                controlling_invalidation_index,
                candle.time.isoformat(),
                "HTF_CONTROL_INVALIDATED",
                "",
                "PREMARKET_HTF_LIFECYCLE",
                "premarket controlling array invalidated during NY",
                context.controlling_array_id,
                "BODY_CLOSED",
            )
        )

    if state_config.enable_va_flip_gate:
        for trigger in detect_va_flip_triggers(
            rows,
            vah,
            val,
            config.vah_val_tolerance,
            prior_close,
        ):
            controlling_still_active = (
                context.controlling_array_id
                # The invalidating HTF body-close is only known at this candle's
                # close.  A VA retest/flip stamped on the same execution candle
                # cannot use that close retroactively to bypass the blocker.
                and (controlling_invalidation_index is None or trigger.index <= controlling_invalidation_index)
            )
            if controlling_still_active and trigger.preferred_direction == context.blocked_direction:
                continue
            triggers.append(trigger)

    unique: dict[tuple[int, str, str], ContextTrigger] = {}
    for trigger in triggers:
        unique[(trigger.index, trigger.preferred_direction, trigger.source)] = trigger
    return sorted(unique.values(), key=lambda item: (item.index, context_priority(item.gate), item.source))


def build_context_authority_lifecycle(
    rows: list,
    context: PremarketContextState,
    triggers: Iterable[ContextTrigger],
) -> list[ContextAuthorityEvent]:
    events: list[ContextAuthorityEvent] = []
    if context.blocked_direction:
        events.append(
            ContextAuthorityEvent(
                0,
                rows[0].time.isoformat(),
                "BLOCK",
                context.blocked_direction,
                "PREMARKET_HTF_CONTROL",
                context.gate,
                "",
                f"{context.controlling_array_type} {context.controlling_array_role.lower()} blocks direction",
            )
        )
    active_direction = ""
    for trigger in sorted(triggers, key=lambda item: (item.index, context_priority(item.gate), item.source)):
        if not trigger.preferred_direction:
            action = "UNLOCK" if trigger.gate == "HTF_CONTROL_INVALIDATED" else "OBSERVE"
            events.append(
                ContextAuthorityEvent(
                    trigger.index,
                    trigger.time,
                    action,
                    context.blocked_direction if action == "UNLOCK" else "",
                    trigger.source,
                    trigger.gate,
                    "",
                    trigger.liquidity_context,
                )
            )
            continue
        replaces = active_direction if active_direction and active_direction != trigger.preferred_direction else ""
        action = "REPLACE" if replaces else "GRANT"
        events.append(
            ContextAuthorityEvent(
                trigger.index,
                trigger.time,
                action,
                trigger.preferred_direction,
                trigger.source,
                trigger.gate,
                replaces,
                trigger.liquidity_context,
            )
        )
        active_direction = trigger.preferred_direction
    return events


def add_optional_structure_trigger(
    triggers: Iterable[ContextTrigger],
    qualifications: Iterable[CisdQualification],
) -> list[ContextTrigger]:
    """Let qualified structure own and reverse direction while context is optional."""
    output = list(triggers)
    directional = [trigger for trigger in output if trigger.preferred_direction]
    first_directional_index = min(
        (trigger.index for trigger in directional),
        default=None,
    )
    structure_changes = [
        item.event
        for item in qualifications
        if item.direction_change_qualified
        and (
            first_directional_index is None
            or item.event.confirm_index < first_directional_index
        )
    ]
    for number, event in enumerate(structure_changes):
        output.append(
            ContextTrigger(
                event.confirm_index,
                event.confirm_time,
                "OPTIONAL_FIRST_STRUCTURE" if number == 0 else "OPTIONAL_STRUCTURE_REVERSAL",
                event.direction,
                "FIRST_QUALIFIED_STRUCTURE" if number == 0 else "QUALIFIED_STRUCTURE_REVERSAL",
                (
                    "liquidity optional; first qualified CISD chooses direction"
                    if number == 0
                    else "liquidity optional; protected-body reversal changes direction"
                ),
            )
        )
    unique = {
        (trigger.index, trigger.preferred_direction, trigger.source): trigger
        for trigger in output
    }
    return sorted(unique.values(), key=lambda item: (item.index, context_priority(item.gate), item.source))


def collapse_directional_context_triggers(
    triggers: Iterable[ContextTrigger],
) -> list[ContextTrigger]:
    """Return one thesis authority per actual direction regime.

    Neutral unlock/observe events do not replace a live direction. Repeated
    grants of the same direction confirm the existing authority instead of
    creating a parallel thesis from the same CISD/FVG.
    """
    output: list[ContextTrigger] = []
    active_direction = ""
    for trigger in sorted(triggers, key=lambda item: (item.index, context_priority(item.gate), item.source)):
        if not trigger.preferred_direction:
            continue
        if trigger.preferred_direction == active_direction:
            continue
        output.append(trigger)
        active_direction = trigger.preferred_direction
    return output


def controlling_array_invalidation_index(
    rows: list,
    controlling_array_id: str,
    arrays: Iterable[HtfArrayLifecycle],
) -> int | None:
    if not rows or not controlling_array_id:
        return None
    times = pd.Series([row.time for row in rows])
    terminal_states = {"BODY_CLOSED", "RECLAIMED_INVALIDATED", "FLIP_INVALIDATED"}
    array = next((item for item in arrays if item.array_id == controlling_array_id), None)
    if array is None:
        return None
    for transition in array.transitions:
        timestamp = pd.Timestamp(transition.time)
        if transition.state not in terminal_states or timestamp < rows[0].time or timestamp > rows[-1].time:
            continue
        index = int(times.searchsorted(timestamp, side="left"))
        return index if index < len(rows) else None
    return None


def build_context_triggers(
    rows: list,
    vah: float,
    val: float,
    liquidity_levels: list[LiquidityLevel],
    opening_sweeps: list[SweepEvent],
    controlling: HtfArrayLifecycle | None,
    htf_arrays: list[HtfArrayLifecycle],
    config: SymbolConfig,
    state_config: ManualStateConfig,
) -> list[ContextTrigger]:
    triggers: list[ContextTrigger] = []
    if controlling is not None and state_config.enable_htf_optional_gate and rows:
        preferred = "long" if controlling.role == "SUPPORT" else "short"
        triggers.append(
            ContextTrigger(
                0,
                rows[0].time.isoformat(),
                "OPTIONAL_HTF_CONTROL",
                preferred,
                "HTF_CONTROLLING_ARRAY",
                f"{controlling.role.lower()} at value area",
                controlling.array_id,
                controlling.current_state,
            )
        )
    if state_config.allow_ny_htf_reaction_gate:
        triggers.extend(htf_lifecycle_context_triggers(rows, htf_arrays, vah, val, config.vah_val_tolerance))
    if state_config.enable_va_flip_gate:
        triggers.extend(detect_va_flip_triggers(rows, vah, val, config.vah_val_tolerance))

    swept_levels: set[str] = set()
    for opening in opening_sweeps:
        preferred = "short" if opening.level.side == "high" else "long"
        triggers.append(
            ContextTrigger(
                0,
                rows[0].time.isoformat(),
                "REQUIRED_SATISFIED",
                preferred,
                "OPENING_PREMARKET_SWEEP",
                f"{opening.level.name} {opening.level.side} sweep",
            )
        )
        swept_levels.add(opening.level.name)

    for index, candle in enumerate(rows):
        if not near_value_area(candle, vah, val, config.vah_val_tolerance):
            continue
        sweep = detect_sweep(index, candle, liquidity_levels, swept_levels, vah, val, config)
        if sweep is None:
            continue
        preferred = "short" if sweep.level.side == "high" else "long"
        if controlling is not None and controlling.role in {"SUPPORT", "RESISTANCE"}:
            controlling_direction = "long" if controlling.role == "SUPPORT" else "short"
            if preferred != controlling_direction:
                continue
        triggers.append(
            ContextTrigger(
                index,
                candle.time.isoformat(),
                "REQUIRED_SATISFIED",
                preferred,
                "LIQUIDITY_SWEEP",
                f"{sweep.level.name} {sweep.level.side} sweep near VA",
                "" if controlling is None else controlling.array_id,
                "" if controlling is None else controlling.current_state,
            )
        )
        swept_levels.add(sweep.level.name)

    unique: dict[tuple[int, str, str], ContextTrigger] = {}
    for trigger in triggers:
        unique[(trigger.index, trigger.preferred_direction, trigger.source)] = trigger
    return sorted(unique.values(), key=lambda item: (item.index, context_priority(item.gate), item.source))


def htf_lifecycle_context_triggers(
    rows: list,
    arrays: Iterable[HtfArrayLifecycle],
    vah: float,
    val: float,
    tolerance: float,
) -> list[ContextTrigger]:
    if not rows:
        return []
    times = pd.Series([row.time for row in rows])
    start, end = rows[0].time, rows[-1].time
    arrays = list(arrays)
    candidate_transitions: list[tuple[pd.Timestamp, HtfArrayLifecycle, HtfTransition]] = []
    for array in arrays:
        near_value = any(array.lower - tolerance <= level <= array.upper + tolerance for level in [vah, val])
        if not near_value:
            continue
        for transition in array.transitions:
            timestamp = pd.Timestamp(transition.time)
            if timestamp < start or timestamp > end:
                continue
            candidate_transitions.append((timestamp, array, transition))

    triggers: list[ContextTrigger] = []
    for timestamp, array, transition in sorted(candidate_transitions, key=lambda item: item[0]):
        state = transition.state
        if state not in HTF_REACTION_STATES:
            continue
        active_asof: list[HtfArrayLifecycle] = []
        for candidate in arrays:
            if pd.Timestamp(candidate.known_time) > timestamp:
                continue
            if not any(candidate.lower - tolerance <= level <= candidate.upper + tolerance for level in [vah, val]):
                continue
            _, role = htf_array_state_asof(candidate, timestamp)
            if role != "INVALIDATED" and htf_array_has_reaction(candidate, timestamp):
                active_asof.append(candidate)
        controlling = select_controlling_array(active_asof, vah, val, tolerance)
        if controlling is None or controlling.array_id != array.array_id:
            continue
        index = int(times.searchsorted(timestamp, side="left"))
        if index >= len(rows):
            continue
        if state == "RESPECTED":
            preferred = "long" if array.direction == "bullish" else "short"
            gate = "OPTIONAL_HTF_RESPECT"
        elif state == "FLIPPED_RESISTANCE":
            preferred = "short"
            gate = "OPTIONAL_HTF_FLIP"
        elif state == "FLIPPED_SUPPORT":
            preferred = "long"
            gate = "OPTIONAL_HTF_FLIP"
        else:
            continue
        triggers.append(
            ContextTrigger(
                index=index,
                time=rows[index].time.isoformat(),
                gate=gate,
                preferred_direction=preferred,
                source="HTF_ARRAY_LIFECYCLE",
                liquidity_context=f"{array.array_type} {array.direction} {state.lower()}",
                htf_array_id=array.array_id,
                htf_array_state=state,
            )
        )
    return triggers


def context_priority(gate: str) -> int:
    return {
        "REQUIRED_SATISFIED": 0,
        "OPTIONAL_HTF_CONTROL": 2,
        "OPTIONAL_HTF_RESPECT": 2,
        "OPTIONAL_HTF_FLIP": 2,
        "REPLACED_BY_VA_FLIP": 3,
    }.get(gate, 9)


def stable_state_id(prefix: str, *parts: object) -> str:
    payload = "|".join(str(part) for part in parts)
    return f"{prefix}_{sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def effective_manual_state_config(
    config: SymbolConfig,
    state_config: ManualStateConfig,
) -> dict[str, object]:
    return {
        "symbol": config.symbol,
        "execution_timeframe": config.timeframe,
        "context_timeframe": config.timeframe,
        "htf_timeframe_minutes": state_config.htf_timeframe_minutes,
        "trade_window_start": (
            config.trade_window_start
            if state_config.enforce_symbol_config_envelope
            else state_config.trade_window_start
        ),
        "trade_window_end": (
            config.trade_window_end
            if state_config.enforce_symbol_config_envelope
            else state_config.trade_window_end
        ),
        "latest_entry_time": config.latest_entry_time,
        "allowed_weekdays": config.allowed_weekdays,
        "max_trades_per_day": config.max_trades_per_day,
        "direction_filter": config.direction_filter,
        "first30_range_filter": config.first30_range_filter,
        "first30_range_max": config.first30_range_max,
        "fvg_window_candles": (
            config.fvg_window_candles
            if state_config.enforce_symbol_config_envelope
            else state_config.fvg_window_candles
        ),
        "configured_stop_model": config.stop_model,
        "effective_stop_model": "cisd_protected_body",
        "tick_size_points": config.tick_size_points,
        "data_validation": state_config.reject_invalid_data,
        "intrabar_ambiguity_policy": state_config.intrabar_ambiguity_policy,
        "overridden_config_fields": (
            "stop_model->cisd_protected_body",
            "cisd_lookahead_candles->state_lifecycle",
            "cancel_pending_on_opposite_cisd->state_episode_terminal",
            "htf_body_close_requalification->state_htf_lifecycle",
        ),
        "unsupported_config_fields": (),
    }


def append_pipeline(
    day_result: ManualStateDayResult,
    stage: str,
    status: PipelineStatus,
    reason: str,
    known_time: str = "",
) -> None:
    day_result.pipeline.append(PipelineStageRecord(stage, status.value, reason, known_time))


def run_manual_state_day(
    frame: pd.DataFrame,
    config: SymbolConfig,
    trade_date: date,
    state_config: ManualStateConfig | None = None,
    *,
    htf_frame_override: pd.DataFrame | None = None,
) -> ManualStateDayResult:
    state_config = state_config or ManualStateConfig()
    if state_config.intrabar_ambiguity_policy not in {"conservative_labeled", "reject"}:
        raise ValueError("intrabar_ambiguity_policy must be 'conservative_labeled' or 'reject'.")
    effective = effective_manual_state_config(config, state_config)
    trade_start = timestamp_on_day(trade_date, str(effective["trade_window_start"]))
    trade_end = timestamp_on_day(trade_date, str(effective["trade_window_end"]))
    profile_start = pd.Timestamp(trade_date).tz_localize("America/New_York") - pd.Timedelta(hours=6)
    profile_frame = time_slice(frame, profile_start, trade_start)
    trade_frame = time_slice(frame, trade_start, trade_end)
    execution_frame = time_slice_from(frame, trade_start)
    day_result = ManualStateDayResult(str(trade_date), None, None)
    day_result.effective_config = effective
    if state_config.enforce_symbol_config_envelope and not allowed_trade_date(trade_date, config.allowed_weekdays):
        day_result.data_state = DataState.VALID.value
        append_pipeline(day_result, "ELIGIBILITY", PipelineStatus.BLOCKED, "WEEKDAY_NOT_ALLOWED")
        return day_result
    append_pipeline(day_result, "ELIGIBILITY", PipelineStatus.PASSED, "DATE_ALLOWED")
    integrity = assess_manual_state_day(frame, trade_date, config.timeframe, profile_start, trade_end)
    day_result.data_state = DataState.VALID.value if integrity.valid else DataState.INVALID.value
    day_result.data_reasons = tuple(issue.code for issue in integrity.issues)
    if not integrity.valid:
        append_pipeline(
            day_result,
            "DATA",
            PipelineStatus.BLOCKED if state_config.reject_invalid_data else PipelineStatus.INFO,
            ",".join(day_result.data_reasons),
        )
        if state_config.reject_invalid_data:
            return day_result
    else:
        append_pipeline(day_result, "DATA", PipelineStatus.PASSED, "OHLCV_AND_REQUIRED_WINDOWS_VALID")
    if profile_frame.empty or trade_frame.empty:
        append_pipeline(day_result, "PROFILE", PipelineStatus.BLOCKED, "EMPTY_PROFILE_OR_TRADE_WINDOW")
        return day_result
    profile = compute_volume_profile(profile_frame, rows=config.volume_profile_rows, value_area_pct=config.value_area_pct)
    if profile is None:
        append_pipeline(day_result, "PROFILE", PipelineStatus.BLOCKED, "VOLUME_PROFILE_UNAVAILABLE")
        return day_result
    append_pipeline(day_result, "PROFILE", PipelineStatus.PASSED, "VALUE_AREA_READY", trade_start.isoformat())
    day_result.vah = round(profile.vah, 6)
    day_result.val = round(profile.val, 6)
    rows = list(trade_frame.itertuples(index=False))
    day_result.cisd_events = detect_cisd_events(
        rows,
        anchor_lookback=state_config.cisd_anchor_lookback,
    )
    raw_cisd_qualifications = build_cisd_structure_lifecycle(rows, day_result.cisd_events)
    day_result.cisd_qualifications = raw_cisd_qualifications
    append_pipeline(
        day_result,
        "CISD_SCAN",
        PipelineStatus.PASSED if day_result.cisd_events else PipelineStatus.INFO,
        f"EVENT_COUNT={len(day_result.cisd_events)}",
    )

    htf_frame = (
        htf_frame_override
        if htf_frame_override is not None
        else build_independent_htf_frame(frame, config.timeframe, state_config.htf_timeframe_minutes)
    )
    history_start = pd.Timestamp(trade_date).tz_localize("America/New_York") - pd.Timedelta(days=3)
    htf_history = time_slice(htf_frame, history_start, trade_end)
    day_result.htf_arrays = build_htf_array_lifecycles(
        htf_history,
        trade_end,
        state_config.enable_htf_order_blocks,
        state_config.htf_timeframe_minutes,
    )
    opening_htf_history = time_slice(htf_frame, history_start, trade_start)
    opening_arrays = build_htf_array_lifecycles(
        opening_htf_history,
        trade_start,
        state_config.enable_htf_order_blocks,
        state_config.htf_timeframe_minutes,
    )
    opening_location = opening_va_location(
        float(rows[0].open),
        profile.vah,
        profile.val,
        config.vah_val_tolerance,
    )
    active_value_boundary = (
        profile.vah
        if opening_location in {"AT_VAH", "ABOVE_VAH", "INSIDE_NEAR_VAH"}
        else profile.val
    )
    controlling = select_controlling_array(
        opening_arrays,
        profile.vah,
        profile.val,
        config.vah_val_tolerance,
        active_value_boundary,
    )
    liquidity_levels, opening_sweeps = build_liquidity_context(frame, trade_date, trade_start, config)
    selection_levels, selection_opening_sweeps = build_selection_liquidity_context(
        frame,
        trade_date,
        trade_start,
        config,
    )
    day_result.liquidity_selection = build_liquidity_selection(
        rows,
        opening_location,
        profile.vah,
        profile.val,
        selection_levels,
        selection_opening_sweeps,
        config.vah_val_tolerance,
    )
    day_result.premarket_context = build_premarket_context(
        rows,
        list(profile_frame.itertuples(index=False)),
        profile.vah,
        profile.val,
        liquidity_levels,
        opening_sweeps,
        controlling,
        config,
        day_result.liquidity_selection,
    )
    day_result.context_triggers = build_authoritative_context_triggers(
        rows,
        profile.vah,
        profile.val,
        day_result.premarket_context,
        day_result.htf_arrays,
        config,
        state_config,
        float(profile_frame.iloc[-1]["close"]),
    )
    if (
        day_result.premarket_context.gate == "OPTIONAL_VA_LOCATION"
        and not day_result.premarket_context.preferred_direction
    ):
        day_result.context_triggers = add_optional_structure_trigger(
            day_result.context_triggers,
            raw_cisd_qualifications,
        )
    day_result.context_triggers = [
        replace(
            trigger,
            event_id=stable_state_id(
                "EVT",
                config.symbol,
                trade_date,
                trigger.time,
                trigger.gate,
                trigger.preferred_direction,
                trigger.source,
            ),
            known_time=(
                trigger.time
                if trigger.index == 0 and trigger.source == "PREMARKET_CONTEXT"
                else (
                    rows[trigger.index].time
                    + pd.Timedelta(minutes=float(parse_timeframe_minutes(config.timeframe) or 0))
                ).isoformat()
            ),
        )
        for trigger in day_result.context_triggers
    ]
    append_pipeline(
        day_result,
        "CONTEXT",
        PipelineStatus.PASSED if day_result.context_triggers else PipelineStatus.BLOCKED,
        f"TRIGGER_COUNT={len(day_result.context_triggers)}",
        trade_start.isoformat(),
    )
    day_result.cisd_qualifications = build_authority_aware_cisd_structure_lifecycle(
        rows,
        day_result.cisd_events,
        day_result.context_triggers,
    )
    day_result.context_authority = build_context_authority_lifecycle(
        rows,
        day_result.premarket_context,
        day_result.context_triggers,
    )

    directional_triggers = collapse_directional_context_triggers(day_result.context_triggers)
    if (
        state_config.enforce_symbol_config_envelope
        and config.direction_filter in {"long", "short"}
    ):
        directional_triggers = [
            trigger for trigger in directional_triggers if trigger.preferred_direction == config.direction_filter
        ]
    fill_count = 0
    active_until: pd.Timestamp | None = None
    first30_range = compute_first30_range(frame, trade_date)
    first30_directionality = compute_first30_directionality(frame, trade_date)
    for trigger_number, trigger in enumerate(directional_triggers):
        if fill_count >= config.max_trades_per_day:
            break
        context_end_index = next_context_change_index(
            directional_triggers,
            trigger_number,
            trigger.preferred_direction,
        )
        source_events = (
            [
                item.event
                for item in day_result.cisd_qualifications
                if qualification_is_actionable(item)
            ]
            if state_config.cisd_qualification_mode == "protected_body"
            else day_result.cisd_events
        )
        eligible_events = [
            event
            for event in source_events
            if (
                event.confirm_index > trigger.index
                or (
                    trigger.gate in {"OPTIONAL_FIRST_STRUCTURE", "OPTIONAL_STRUCTURE_REVERSAL"}
                    and event.confirm_index == trigger.index
                )
            )
            and (context_end_index is None or event.confirm_index < context_end_index)
        ]
        if trigger.gate == "REPLACED_BY_VA_FLIP":
            active_qualification = next(
                (
                    item
                    for item in reversed(day_result.cisd_qualifications)
                    if item.event.confirm_index <= trigger.index
                    and qualification_is_actionable(item)
                ),
                None,
            )
            active_before_trigger = (
                active_qualification.event
                if active_qualification is not None
                and active_qualification.event.direction == trigger.preferred_direction
                else None
            )
            if active_before_trigger is not None:
                eligible_events.append(active_before_trigger)
                eligible_events = list(
                    {
                        (event.direction, event.confirm_index): event
                        for event in eligible_events
                    }.values()
                )
        episodes = build_thesis_episodes(
            eligible_events,
            trigger.preferred_direction,
            state_config.allow_opposite_cisd_reversal_without_context,
        )
        if not episodes:
            continue
        if context_end_index is not None and episodes[-1].terminal_index is None:
            episodes[-1].status = "INVALIDATED"
            episodes[-1].terminal_reason = "CONTEXT_REPLACED"
            episodes[-1].terminal_index = context_end_index
            episodes[-1].terminal_time = rows[context_end_index].time.isoformat()
        offset = len(day_result.thesis_episodes)
        for episode in episodes:
            episode.episode_id += offset
            episode.thesis_id = stable_state_id(
                "THS",
                config.symbol,
                trade_date,
                trigger.event_id,
                episode.episode_id,
                episode.direction,
                episode.cisd.confirm_time,
            )
            episode.authority_gate = trigger.gate
            episode.authority_source = trigger.source
            episode.authority_time = trigger.time
            episode.authority_index = trigger.index + (1 if trigger.gate == "REPLACED_BY_VA_FLIP" else 0)
            episode.cisd_lifecycle_state = next(
                (
                    item.lifecycle_state
                    for item in day_result.cisd_qualifications
                    if item.event.confirm_index == episode.cisd.confirm_index
                    and item.event.direction == episode.cisd.direction
                ),
                "RAW",
            )
        day_result.thesis_episodes.extend(episodes)
        decisions = decisions_from_episodes(
            rows,
            execution_frame,
            config,
            state_config,
            trade_date,
            trigger,
            episodes,
            first30_range,
            first30_directionality,
            config.max_trades_per_day - fill_count,
            active_until,
            (
                day_result.liquidity_selection.selected_name
                if day_result.liquidity_selection is not None
                else ""
            ),
        )
        day_result.decisions.extend(decisions)
        filled = [decision for decision in decisions if decision.order_state == OrderState.FILLED.value]
        fill_count += len(filled)
        if filled:
            last_terminal = max(
                pd.Timestamp(decision.terminal_time)
                for decision in filled
                if decision.terminal_time
            )
            if config.active_trade_block_mode == "none":
                active_until = None
            elif config.active_trade_block_mode == "entry":
                active_until = max(pd.Timestamp(decision.entry_time) for decision in filled)
            elif config.active_trade_block_mode == "fvg":
                active_until = max(pd.Timestamp(decision.fvg_time) for decision in filled)
            else:
                active_until = last_terminal
    if day_result.decisions:
        append_pipeline(
            day_result,
            "ORDER",
            PipelineStatus.PASSED,
            (
                f"DECISIONS={len(day_result.decisions)};"
                f"FILLS={sum(item.order_state == OrderState.FILLED.value for item in day_result.decisions)}"
            ),
        )
    else:
        reason = "NO_CONTEXT" if not directional_triggers else "NO_QUALIFIED_CISD_OR_ENTRY_ARRAY"
        append_pipeline(day_result, "ORDER", PipelineStatus.BLOCKED, reason)
    validate_day_state_invariants(day_result)
    return day_result


def next_context_change_index(
    triggers: list[ContextTrigger],
    trigger_number: int,
    preferred_direction: str,
) -> int | None:
    current_index = triggers[trigger_number].index
    for candidate in triggers[trigger_number + 1 :]:
        if candidate.index <= current_index:
            continue
        if not candidate.preferred_direction:
            continue
        if candidate.preferred_direction != preferred_direction:
            return candidate.index
    return None


def decisions_from_episodes(
    rows: list,
    execution_frame: pd.DataFrame,
    config: SymbolConfig,
    state_config: ManualStateConfig,
    trade_date: date,
    trigger: ContextTrigger,
    episodes: list[ThesisEpisode],
    first30_range: float | None = None,
    first30_directionality: float | None = None,
    max_fills: int = 1,
    active_until: pd.Timestamp | None = None,
    liquidity_name: str = "",
) -> list[ManualStateDecision]:
    decisions: list[ManualStateDecision] = []
    fill_count = 0
    local_active_until = active_until
    for number, episode in enumerate(episodes):
        if fill_count >= max_fills:
            break
        if local_active_until is not None and pd.Timestamp(episode.cisd.confirm_time) <= local_active_until:
            continue
        setup_terminal_index = episode.terminal_index
        setup_rows = rows if setup_terminal_index is None else rows[: setup_terminal_index + 1]
        fvg = find_fvg_or_ifvg(
            setup_rows,
            episode.cisd.confirm_index,
            episode.direction,
            config.fvg_entry_mode,
            config.setup_type_filter,
            config.min_fvg_points,
            episode.cisd.invalidation_level,
            (
                config.fvg_window_candles
                if state_config.enforce_symbol_config_envelope
                else state_config.fvg_window_candles
            ),
            episode.cisd.anchor_index,
            episode.authority_index,
        )
        fvg_index, entry_price, fvg_kind, zone_lower, zone_upper, _ = fvg
        if fvg_index is None or entry_price is None or fvg_kind is None:
            continue
        adjusted_entry = apply_entry_cost(entry_price, episode.direction, config)
        stop_price = round_price_to_tick(episode.cisd.body_stop, config.tick_size_points)
        risk = abs(adjusted_entry - stop_price)
        valid_stop = stop_price < adjusted_entry if episode.direction == "long" else stop_price > adjusted_entry
        if risk <= 0 or not valid_stop:
            continue
        target_price = (
            adjusted_entry + config.reward_r * risk
            if episode.direction == "long"
            else adjusted_entry - config.reward_r * risk
        )
        target_price = round_price_to_tick(target_price, config.tick_size_points)
        pending_terminal_index, pending_terminal_reason, pending_terminal_time = pending_order_terminal(
            episodes,
            number,
        )
        pending_rows = rows if pending_terminal_index is None else rows[: pending_terminal_index + 1]
        resolution = find_entry_resolution(
            pending_rows,
            fvg_index,
            episode.direction,
            entry_price,
            target_price,
            config.allow_entry_on_setup_candle,
            config.min_entry_delay_candles_after_fvg,
            config.require_close_away_before_entry,
            zone_lower,
            zone_upper,
            timestamp_on_day(trade_date, config.latest_entry_time) if config.latest_entry_time else None,
            False,
            episode.cisd.invalidation_level,
            state_config.target_before_fill_mode == "synthetic_r",
            state_config.intrabar_ambiguity_policy == "reject",
        )
        fvg_time = rows[fvg_index].time.isoformat()
        terminal_fill_conflict = (
            state_config.cancel_entry_on_terminal_candle
            and resolution.entry_index is not None
            and pending_terminal_index is not None
            and resolution.entry_index == pending_terminal_index
            and pending_terminal_reason in {"OPPOSITE_CISD", "CONTEXT_REPLACED"}
        )
        if terminal_fill_conflict:
            decisions.append(
                make_decision(
                    config,
                    trade_date,
                    trigger,
                    episode,
                    "VALID",
                    "CANCELLED",
                    "SKIP",
                    "NO_FILL",
                    (
                        "CANCELLED_OPPOSITE_CISD_SAME_CANDLE"
                        if pending_terminal_reason == "OPPOSITE_CISD"
                        else "CANCELLED_CONTEXT_REPLACED_SAME_CANDLE"
                    ),
                    pending_terminal_time,
                    fvg_time,
                    fvg_kind,
                    "",
                    None,
                    stop_price,
                    target_price,
                    resolution.intrabar_ambiguity,
                )
            )
            continue
        if resolution.entry_index is None:
            terminal_reason = resolution.terminal_reason
            terminal_time = "" if resolution.terminal_time is None else resolution.terminal_time.isoformat()
            if pending_terminal_index is not None and terminal_reason == "CANCELLED_TRADE_WINDOW_END":
                terminal_reason = {
                    "OPPOSITE_CISD": "CANCELLED_OPPOSITE_CISD",
                    "CONTEXT_REPLACED": "CANCELLED_CONTEXT_REPLACED",
                }.get(pending_terminal_reason, "CANCELLED_TRADE_WINDOW_END")
                terminal_time = pending_terminal_time
            decisions.append(
                make_decision(
                    config,
                    trade_date,
                    trigger,
                    episode,
                    "VALID",
                    "CANCELLED",
                    "SKIP",
                    "NO_FILL",
                    terminal_reason,
                    terminal_time,
                    fvg_time,
                    fvg_kind,
                    "",
                    None,
                    stop_price,
                    target_price,
                    resolution.intrabar_ambiguity,
                )
            )
            continue

        entry = rows[resolution.entry_index]
        if resolution.intrabar_ambiguity and state_config.intrabar_ambiguity_policy == "reject":
            decisions.append(
                make_decision(
                    config,
                    trade_date,
                    trigger,
                    episode,
                    SetupState.VALID.value,
                    OrderState.CANCELLED.value,
                    FinalDecision.SKIP.value,
                    "NO_FILL",
                    "AMBIGUOUS_INTRABAR",
                    entry.time.isoformat(),
                    fvg_time,
                    fvg_kind,
                    "",
                    None,
                    stop_price,
                    target_price,
                    resolution.intrabar_ambiguity,
                )
            )
            continue
        if (
            state_config.enforce_symbol_config_envelope
            and blocked_by_first30_range_filter(entry.time, first30_range, config)
        ):
            decisions.append(
                make_decision(
                    config,
                    trade_date,
                    trigger,
                    episode,
                    SetupState.VALID.value,
                    OrderState.NOT_PLACED.value,
                    FinalDecision.SKIP.value,
                    "NO_FILL",
                    "BLOCKED_FIRST30_RANGE",
                    entry.time.isoformat(),
                    fvg_time,
                    fvg_kind,
                    "",
                    None,
                    stop_price,
                    target_price,
                )
            )
            continue
        if (
            state_config.enforce_symbol_config_envelope
            and blocked_by_swing_first30_directionality(
                liquidity_name,
                first30_directionality,
                config,
                as_of=entry.time,
            )
        ):
            decisions.append(
                make_decision(
                    config,
                    trade_date,
                    trigger,
                    episode,
                    SetupState.VALID.value,
                    OrderState.NOT_PLACED.value,
                    FinalDecision.SKIP.value,
                    "NO_FILL",
                    "BLOCKED_FIRST30_DIRECTIONALITY",
                    entry.time.isoformat(),
                    fvg_time,
                    fvg_kind,
                    "",
                    None,
                    stop_price,
                    target_price,
                )
            )
            continue
        exit_candle, exit_price, result, r_multiple = simulate_exit(
            time_slice_from(execution_frame, entry.time).itertuples(index=False),
            entry.time,
            episode.direction,
            adjusted_entry,
            stop_price,
            target_price,
            config.reward_r,
            config.stop_management,
        )
        execution_ambiguity = "|".join(
            value
            for value in [
                resolution.intrabar_ambiguity,
                "STOP_AND_TARGET_TOUCHED_SAME_BAR" if result == "loss_same_bar" else "",
            ]
            if value
        )
        decisions.append(
            make_decision(
                config,
                trade_date,
                trigger,
                episode,
                "VALID",
                "FILLED",
                "TAKE",
                result_to_manual_outcome(result),
                result_to_terminal_reason(result),
                exit_candle.time.isoformat(),
                fvg_time,
                fvg_kind,
                entry.time.isoformat(),
                adjusted_entry,
                stop_price,
                target_price,
                intrabar_ambiguity=execution_ambiguity,
                exit_price=exit_price,
                r_multiple=r_multiple,
            )
        )
        fill_count += 1
        if config.active_trade_block_mode == "none":
            local_active_until = None
        elif config.active_trade_block_mode == "entry":
            local_active_until = entry.time
        elif config.active_trade_block_mode == "fvg":
            local_active_until = rows[fvg_index].time
        else:
            local_active_until = exit_candle.time
    return decisions


def pending_order_terminal(
    episodes: list[ThesisEpisode],
    episode_number: int,
) -> tuple[int | None, str, str]:
    """Return the first true terminal after an order is created.

    Same-direction CISD can requalify later structure, but it does not revoke an
    already resting order.  We therefore walk across same-direction episode
    boundaries until an opposite CISD, context replacement, or the open trade
    window supplies the terminal.
    """
    for candidate in episodes[episode_number:]:
        if candidate.terminal_index is None:
            return None, "", ""
        if candidate.terminal_reason == "SAME_DIRECTION_CISD_REQUALIFICATION":
            continue
        return candidate.terminal_index, candidate.terminal_reason, candidate.terminal_time
    return None, "", ""


def make_decision(
    config: SymbolConfig,
    trade_date: date,
    trigger: ContextTrigger,
    episode: ThesisEpisode,
    setup_state: str,
    order_state: str,
    final_decision: str,
    outcome: str,
    terminal_reason: str,
    terminal_time: str,
    fvg_time: str,
    fvg_kind: str,
    entry_time: str,
    entry_price: float | None,
    stop_price: float | None,
    target_price: float | None,
    intrabar_ambiguity: str = "",
    exit_price: float | None = None,
    r_multiple: float | None = None,
) -> ManualStateDecision:
    bar_minutes = float(parse_timeframe_minutes(config.timeframe) or 0)
    bar_duration = pd.Timedelta(minutes=bar_minutes)

    def bar_known_time(value: str) -> str:
        return "" if not value else (pd.Timestamp(value) + bar_duration).isoformat()

    thesis_id = episode.thesis_id or stable_state_id(
        "THS",
        config.symbol,
        trade_date,
        episode.episode_id,
        episode.direction,
        episode.cisd.confirm_time,
    )
    event_id = trigger.event_id or stable_state_id(
        "EVT",
        config.symbol,
        trade_date,
        trigger.time,
        trigger.gate,
        trigger.preferred_direction,
    )
    order_id = stable_state_id(
        "ORD",
        thesis_id,
        fvg_time,
        fvg_kind,
        config.fvg_entry_mode,
    )
    trace = " -> ".join(
        item
        for item in [
            f"AUTHORITY@{trigger.known_time or trigger.time}",
            f"CISD@{episode.cisd.confirm_known_time or bar_known_time(episode.cisd.confirm_time)}",
            f"FVG@{bar_known_time(fvg_time)}" if fvg_time else "",
            f"ORDER={order_state}",
            f"TERMINAL={terminal_reason}@{bar_known_time(terminal_time)}" if terminal_reason else "",
        ]
        if item
    )
    return ManualStateDecision(
        symbol=config.symbol,
        timeframe=config.timeframe,
        date=str(trade_date),
        setup_state=setup_state,
        order_state=order_state,
        final_decision=final_decision,
        direction=episode.direction,
        outcome=outcome,
        terminal_reason=terminal_reason,
        terminal_time=terminal_time,
        context_gate=trigger.gate,
        context_source=trigger.source,
        liquidity_context=trigger.liquidity_context,
        htf_array_id=trigger.htf_array_id,
        htf_array_state=trigger.htf_array_state,
        thesis_episode_id=episode.episode_id,
        cisd_anchor_time=episode.cisd.anchor_time,
        cisd_time=episode.cisd.confirm_time,
        cisd_body_level=episode.cisd.body_level,
        cisd_invalidation_level=episode.cisd.invalidation_level,
        cisd_displacement_points=episode.cisd.displacement_points,
        fvg_time=fvg_time,
        fvg_kind=fvg_kind,
        entry_time=entry_time,
        entry_price=None if entry_price is None else round(entry_price, 6),
        stop_price=None if stop_price is None else round(stop_price, 6),
        target_price=None if target_price is None else round(target_price, 6),
        exit_price=None if exit_price is None else float(exit_price),
        r_multiple=None if r_multiple is None else float(r_multiple),
        event_id=event_id,
        thesis_id=thesis_id,
        order_id=order_id,
        intrabar_ambiguity=intrabar_ambiguity,
        trace=trace,
        event_known_time=trigger.known_time or trigger.time,
        cisd_known_time=episode.cisd.confirm_known_time or bar_known_time(episode.cisd.confirm_time),
        fvg_known_time=bar_known_time(fvg_time),
        entry_known_time=bar_known_time(entry_time),
        terminal_known_time=bar_known_time(terminal_time),
    )


def validate_day_state_invariants(day: ManualStateDayResult) -> None:
    order_ids: set[str] = set()
    terminal_by_order: dict[str, str] = {}
    for decision in day.decisions:
        if not decision.order_id:
            raise ValueError("Manual-state decision is missing order_id.")
        if decision.order_id in order_ids:
            raise ValueError(f"Duplicate order_id in one day: {decision.order_id}")
        order_ids.add(decision.order_id)
        if decision.order_state not in {state.value for state in OrderState}:
            raise ValueError(f"Unknown order state: {decision.order_state}")
        if decision.setup_state not in {state.value for state in SetupState}:
            raise ValueError(f"Unknown setup state: {decision.setup_state}")
        if decision.final_decision not in {state.value for state in FinalDecision}:
            raise ValueError(f"Unknown final decision: {decision.final_decision}")
        if decision.order_state == OrderState.FILLED.value:
            if decision.final_decision != FinalDecision.TAKE.value or not decision.entry_time:
                raise ValueError("FILLED order must be TAKE and must have entry_time.")
            if decision.entry_price is None or decision.stop_price is None or decision.target_price is None:
                raise ValueError("FILLED order must have entry, stop, and target prices.")
            if decision.exit_price is None or decision.r_multiple is None:
                raise ValueError("FILLED order must have exit price and R multiple.")
            if decision.direction == "long" and not (
                decision.stop_price < decision.entry_price < decision.target_price
            ):
                raise ValueError("Long price ordering must be stop < entry < target.")
            if decision.direction == "short" and not (
                decision.target_price < decision.entry_price < decision.stop_price
            ):
                raise ValueError("Short price ordering must be target < entry < stop.")
        elif decision.final_decision == FinalDecision.TAKE.value:
            raise ValueError("Only FILLED orders may have FINAL=TAKE.")
        if decision.terminal_reason:
            if decision.order_id in terminal_by_order:
                raise ValueError(f"Order has multiple terminal states: {decision.order_id}")
            terminal_by_order[decision.order_id] = decision.terminal_reason
        chronological = [
            value
            for value in [
                decision.cisd_time,
                decision.fvg_time,
                decision.entry_time,
                decision.terminal_time,
            ]
            if value
        ]
        parsed = [pd.Timestamp(value) for value in chronological]
        if parsed != sorted(parsed):
            raise ValueError(f"Decision timestamps are not monotonic: {decision.order_id}")


def run_manual_state_backtest(
    frame: pd.DataFrame,
    config: SymbolConfig,
    dates: Iterable[date],
    state_config: ManualStateConfig | None = None,
) -> ManualStateBacktestResult:
    frame = frame.sort_values("time").reset_index(drop=True)
    effective_state_config = state_config or ManualStateConfig()
    htf_frame = build_independent_htf_frame(
        frame,
        config.timeframe,
        effective_state_config.htf_timeframe_minutes,
    )
    return ManualStateBacktestResult(
        [
            run_manual_state_day(
                frame,
                config,
                trade_date,
                effective_state_config,
                htf_frame_override=htf_frame,
            )
            for trade_date in sorted(set(dates))
        ]
    )


def cisd_events_to_frame(days: Iterable[ManualStateDayResult]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for day in days:
        for event in day.cisd_events:
            rows.append({"date": day.trade_date, **asdict(event)})
    return pd.DataFrame(rows)


def cisd_qualifications_to_frame(days: Iterable[ManualStateDayResult]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for day in days:
        for qualification in day.cisd_qualifications:
            item = asdict(qualification)
            event = item.pop("event")
            rows.append(
                {
                    "date": day.trade_date,
                    **item,
                    **{f"cisd_{key}": value for key, value in event.items()},
                }
            )
    return pd.DataFrame(rows)


def htf_arrays_to_frame(days: Iterable[ManualStateDayResult]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for day in days:
        for array in day.htf_arrays:
            item = asdict(array)
            item["transitions"] = "; ".join(
                f"{transition['time']}:{transition['state']}" for transition in item["transitions"]
            )
            rows.append({"date": day.trade_date, **item})
    return pd.DataFrame(rows)


def context_triggers_to_frame(days: Iterable[ManualStateDayResult]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for day in days:
        for trigger in day.context_triggers:
            rows.append({"date": day.trade_date, **asdict(trigger)})
    return pd.DataFrame(rows)


def premarket_contexts_to_frame(days: Iterable[ManualStateDayResult]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": day.trade_date, **asdict(day.premarket_context)}
            for day in days
            if day.premarket_context is not None
        ]
    )


def liquidity_selections_to_frame(days: Iterable[ManualStateDayResult]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": day.trade_date, **asdict(day.liquidity_selection)}
            for day in days
            if day.liquidity_selection is not None
        ]
    )


def context_authority_to_frame(days: Iterable[ManualStateDayResult]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for day in days:
        for event in day.context_authority:
            rows.append({"date": day.trade_date, **asdict(event)})
    return pd.DataFrame(rows)


def thesis_episodes_to_frame(days: Iterable[ManualStateDayResult]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for day in days:
        for episode in day.thesis_episodes:
            item = asdict(episode)
            cisd = item.pop("cisd")
            rows.append({"date": day.trade_date, **item, **{f"cisd_{key}": value for key, value in cisd.items()}})
    return pd.DataFrame(rows)


def decisions_to_frame(days: Iterable[ManualStateDayResult]) -> pd.DataFrame:
    return pd.DataFrame([asdict(decision) for day in days for decision in day.decisions])
