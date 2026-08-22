from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import NamedTuple

import pandas as pd

from .config import SymbolConfig
from .data_inspector import parse_timeframe_minutes
from .gaps import GapEvent, find_gap_events, skip_dates_from_gaps
from .volume_profile import compute_volume_profile


@dataclass(frozen=True)
class Trade:
    symbol: str
    timeframe: str
    date: str
    direction: str
    vah: float
    val: float
    trigger_level: float
    liquidity_context: str
    sweep_time: str
    cisd_time: str
    fvg_time: str
    entry_time: str
    entry_price: float
    stop_price: float
    target_price: float
    exit_time: str
    exit_price: float
    result: str
    r_multiple: float
    notes: str
    execution_ambiguity: str = ""


@dataclass(frozen=True)
class SetupLifecycle:
    symbol: str
    timeframe: str
    date: str
    direction: str
    setup_state: str
    order_state: str
    final_decision: str
    outcome: str
    terminal_reason: str
    terminal_time: str
    liquidity_context: str
    sweep_time: str
    cisd_time: str
    fvg_time: str
    entry_time: str
    setup_kind: str
    execution_ambiguity: str = ""


@dataclass(frozen=True)
class BacktestResult:
    trades: list[Trade]
    skipped_dates: set[date]
    gap_events: list[GapEvent]
    lifecycles: list[SetupLifecycle] = field(default_factory=list)


class LiquidityLevel(NamedTuple):
    name: str
    side: str
    price: float


class SweepEvent(NamedTuple):
    index: int
    level: LiquidityLevel
    is_fresh: bool = True
    source: str = "regular"
    direction: str | None = None


class HtfArray(NamedTuple):
    array_type: str
    direction: str
    lower: float
    upper: float
    known_time: pd.Timestamp


class SetupCandidate(NamedTuple):
    direction: str
    cisd_index: int
    fvg_index: int
    entry_price: float
    setup_kind: str
    cisd_body_stop: float
    cisd_invalidation_level: float | None = None
    zone_lower: float | None = None
    zone_upper: float | None = None
    body_edge_entry: float | None = None


class PendingOrderResolution(NamedTuple):
    entry_index: int | None
    terminal_reason: str
    terminal_time: pd.Timestamp | None
    intrabar_ambiguity: str = ""


CLOSED_RESULTS = {"win", "loss", "loss_same_bar", "breakeven", "reduced_loss"}


def run_backtest(frame: pd.DataFrame, config: SymbolConfig) -> BacktestResult:
    frame = frame.sort_values("time").reset_index(drop=True)
    expected_minutes = parse_timeframe_minutes(config.timeframe) or 5.0
    gap_events = find_gap_events(frame, expected_minutes=expected_minutes)
    skipped_dates = skip_dates_from_gaps(gap_events)
    trades: list[Trade] = []
    lifecycles: list[SetupLifecycle] = []
    htf_frame = build_htf_frame(frame, config)

    dates = sorted(frame["date"].unique())
    for trade_date in dates:
        if trade_date in skipped_dates:
            continue
        if not allowed_trade_date(trade_date, config.allowed_weekdays):
            continue

        profile_start = pd.Timestamp(trade_date).tz_localize("America/New_York") - pd.Timedelta(hours=6)
        profile_end = pd.Timestamp(trade_date).tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=30)
        trade_start = timestamp_on_day(trade_date, config.trade_window_start)
        trade_end = timestamp_on_day(trade_date, config.trade_window_end)

        profile_frame = time_slice(frame, profile_start, profile_end)
        trade_frame = time_slice(frame, trade_start, trade_end)
        execution_frame = time_slice_from(frame, trade_start)
        if profile_frame.empty or trade_frame.empty:
            continue

        profile = compute_volume_profile(
            profile_frame,
            rows=config.volume_profile_rows,
            value_area_pct=config.value_area_pct,
        )
        if profile is None:
            continue

        liquidity_levels, opening_sweeps = build_liquidity_context(frame, trade_date, trade_start, config)
        htf_requalification_sweeps = build_htf_requalification_sweeps(
            htf_frame,
            trade_frame,
            trade_date,
            profile.vah,
            profile.val,
            config,
        )
        first30_range = compute_first30_range(frame, trade_date)
        first30_directionality = compute_first30_directionality(frame, trade_date)
        day_trades = find_day_trades(
            trade_frame,
            execution_frame,
            profile.vah,
            profile.val,
            liquidity_levels,
            opening_sweeps,
            htf_requalification_sweeps,
            config,
            first30_range,
            first30_directionality,
            lifecycles,
        )
        trades.extend(day_trades[: config.max_trades_per_day])

    return BacktestResult(
        trades=trades,
        skipped_dates=skipped_dates,
        gap_events=gap_events,
        lifecycles=lifecycles,
    )


def time_slice(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if frame.empty or start >= end:
        return frame.iloc[0:0]
    times = frame["time"]
    left = int(times.searchsorted(start, side="left"))
    right = int(times.searchsorted(end, side="left"))
    return frame.iloc[left:right]


def time_slice_from(frame: pd.DataFrame, start: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame
    left = int(frame["time"].searchsorted(start, side="left"))
    return frame.iloc[left:]


def time_slice_before(frame: pd.DataFrame, end: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame
    right = int(frame["time"].searchsorted(end, side="left"))
    return frame.iloc[:right]


def find_day_trades(
    trade_frame: pd.DataFrame,
    execution_frame: pd.DataFrame,
    vah: float,
    val: float,
    liquidity_levels: list[LiquidityLevel],
    opening_sweeps: list[SweepEvent],
    htf_requalification_sweeps: list[SweepEvent],
    config: SymbolConfig,
    first30_range: float | None = None,
    first30_directionality: float | None = None,
    lifecycles: list[SetupLifecycle] | None = None,
) -> list[Trade]:
    trades: list[Trade] = []
    active_until: pd.Timestamp | None = None
    swept_levels: set[str] = set()
    last_trade_direction: str | None = None
    last_sweep: SweepEvent | None = opening_sweeps[-1] if opening_sweeps else None
    rows = list(trade_frame.itertuples(index=False))
    htf_sweeps_by_index = {sweep.index: sweep for sweep in htf_requalification_sweeps}

    for index, candle in enumerate(rows):
        if active_until is not None and candle.time <= active_until:
            continue

        sweep = htf_sweeps_by_index.get(index)
        if sweep is None and not near_value_area(candle, vah, val, config.vah_val_tolerance):
            continue

        direction_override: str | None = None if sweep is None else sweep.direction
        if sweep is None:
            sweep = detect_sweep(index, candle, liquidity_levels, swept_levels, vah, val, config)
        if sweep is None:
            if (
                not config.allow_same_direction_followup_without_fresh_sweep
                or last_sweep is None
                or (last_trade_direction is None and last_sweep.source != "opening_premarket")
            ):
                continue
            sweep = SweepEvent(index=index, level=last_sweep.level, is_fresh=False, source=last_sweep.source)
            direction_override = last_trade_direction
            if (
                direction_override is None
                and last_sweep.source == "opening_premarket"
                and config.opening_premarket_direction_mode == "reversal_only"
            ):
                direction_override = "short" if last_sweep.level.side == "high" else "long"

        trade = build_setup(
            rows,
            execution_frame,
            sweep,
            vah,
            val,
            config,
            direction_override,
            first30_range,
            first30_directionality,
            lifecycles,
        )
        if sweep.is_fresh:
            swept_levels.add(sweep.level.name)

        if trade is not None:
            trades.append(trade)
            last_trade_direction = trade.direction
            last_sweep = sweep if sweep.is_fresh else last_sweep
            active_until = next_scan_block_time(trade, config.active_trade_block_mode)
            if len(trades) >= config.max_trades_per_day:
                break

    return trades


def next_scan_block_time(trade: Trade, mode: str) -> pd.Timestamp | None:
    if mode == "none":
        return None
    if mode == "entry":
        return pd.Timestamp(trade.entry_time)
    if mode == "fvg":
        return pd.Timestamp(trade.fvg_time)
    return pd.Timestamp(trade.exit_time)


def build_setup(
    rows: list,
    execution_frame: pd.DataFrame,
    sweep: SweepEvent,
    vah: float,
    val: float,
    config: SymbolConfig,
    direction_override: str | None = None,
    first30_range: float | None = None,
    first30_directionality: float | None = None,
    lifecycles: list[SetupLifecycle] | None = None,
) -> Trade | None:
    setup = find_directional_setup(
        rows,
        sweep.index,
        config.fvg_entry_mode,
        direction_override or config.direction_filter,
        config.setup_type_filter,
        config.min_fvg_points,
        config.require_post_sweep_cisd_reference,
        config.invalidate_cisd_before_fvg,
        config.cisd_lookahead_candles,
        config.fvg_window_candles,
    )
    if setup is None:
        return None
    stop_price = round_price_to_tick(
        setup_stop(rows, sweep.index, setup, config.stop_model),
        config.tick_size_points,
    )
    direction = setup.direction
    cisd_index = setup.cisd_index
    fvg_index = setup.fvg_index
    entry_price = setup.entry_price
    followup_note = "; continuation_no_fresh_sweep" if not sweep.is_fresh else ""
    requalification_note = "; htf_body_close_requalification" if sweep.source == "htf_requalification" else ""
    setup_kind = f"{setup.setup_kind}; stop_model={config.stop_model}{followup_note}{requalification_note}"

    adjusted_entry = apply_entry_cost(entry_price, direction, config)
    risk = abs(adjusted_entry - stop_price)
    if risk <= 0 or not valid_stop(direction, adjusted_entry, stop_price):
        return None

    target_price = adjusted_entry - config.reward_r * risk if direction == "short" else adjusted_entry + config.reward_r * risk
    target_price = round_price_to_tick(target_price, config.tick_size_points)

    resolution = find_entry_resolution(
        rows,
        fvg_index,
        direction,
        entry_price,
        target_price,
        config.allow_entry_on_setup_candle,
        config.min_entry_delay_candles_after_fvg,
        config.require_close_away_before_entry,
        setup.zone_lower,
        setup.zone_upper,
        timestamp_on_day(rows[sweep.index].time.date(), config.latest_entry_time) if config.latest_entry_time else None,
        config.cancel_pending_on_opposite_cisd,
        setup.cisd_invalidation_level,
    )
    sweep_candle = rows[sweep.index]
    cisd = rows[cisd_index]
    fvg = rows[fvg_index]
    liquidity_context = (
        f"{sweep.level.name} requalification near VA"
        if sweep.source == "htf_requalification"
        else f"{sweep.level.name} {sweep.level.side} sweep near VA"
    )
    if resolution.entry_index is None:
        if lifecycles is not None:
            lifecycles.append(
                SetupLifecycle(
                    symbol=config.symbol,
                    timeframe=config.timeframe,
                    date=str(sweep_candle.time.date()),
                    direction=direction,
                    setup_state="VALID",
                    order_state="CANCELLED",
                    final_decision="SKIP",
                    outcome="NO_FILL",
                    terminal_reason=resolution.terminal_reason,
                    terminal_time="" if resolution.terminal_time is None else resolution.terminal_time.isoformat(),
                    liquidity_context=liquidity_context,
                    sweep_time=sweep_candle.time.isoformat(),
                    cisd_time=cisd.time.isoformat(),
                    fvg_time=fvg.time.isoformat(),
                    entry_time="",
                    setup_kind=setup_kind,
                    execution_ambiguity=resolution.intrabar_ambiguity,
                )
            )
        return None

    entry_index = resolution.entry_index
    entry = rows[entry_index]
    if blocked_by_first30_range_filter(entry.time, first30_range, config):
        return None
    if blocked_by_swing_first30_directionality(
        sweep.level.name,
        first30_directionality,
        config,
        as_of=entry.time,
    ):
        return None
    exit_candle, exit_price, result, r_multiple = simulate_exit(
        time_slice_from(execution_frame, entry.time).itertuples(index=False),
        entry.time,
        direction,
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
    trade = Trade(
        symbol=config.symbol,
        timeframe=config.timeframe,
        date=str(entry.time.date()),
        direction=direction,
        vah=round(vah, 2),
        val=round(val, 2),
        trigger_level=round(sweep.level.price, 2),
        liquidity_context=liquidity_context,
        sweep_time=sweep_candle.time.isoformat(),
        cisd_time=cisd.time.isoformat(),
        fvg_time=fvg.time.isoformat(),
        entry_time=entry.time.isoformat(),
        entry_price=round(adjusted_entry, 2),
        stop_price=round(stop_price, 2),
        target_price=round(target_price, 2),
        exit_time=exit_candle.time.isoformat(),
        exit_price=round(exit_price, 2),
        result=result,
        r_multiple=round(r_multiple, 2),
        notes=(
            setup_kind
            if not execution_ambiguity
            else f"{setup_kind}; execution_ambiguity={execution_ambiguity}"
        ),
        execution_ambiguity=execution_ambiguity,
    )
    if lifecycles is not None:
        lifecycles.append(
            SetupLifecycle(
                symbol=config.symbol,
                timeframe=config.timeframe,
                date=str(entry.time.date()),
                direction=direction,
                setup_state="VALID",
                order_state="FILLED",
                final_decision="TAKE",
                outcome=result_to_manual_outcome(result),
                terminal_reason=result_to_terminal_reason(result),
                terminal_time=exit_candle.time.isoformat(),
                liquidity_context=liquidity_context,
                sweep_time=sweep_candle.time.isoformat(),
                cisd_time=cisd.time.isoformat(),
                fvg_time=fvg.time.isoformat(),
                entry_time=entry.time.isoformat(),
                setup_kind=setup_kind,
                execution_ambiguity=execution_ambiguity,
            )
        )
    return trade


def build_liquidity_context(
    frame: pd.DataFrame,
    trade_date: date,
    trade_start: pd.Timestamp,
    config: SymbolConfig,
) -> tuple[list[LiquidityLevel], list[SweepEvent]]:
    session_levels = build_session_liquidity_levels(frame, trade_date)
    opening_sweeps = build_opening_premarket_sweeps(frame, trade_date, trade_start, session_levels, config)
    levels = filter_taken_session_levels(frame, trade_start, session_levels, opening_sweeps, config)
    if not config.session_liquidity_only:
        levels.extend(build_swing_liquidity_levels(frame, trade_start, config))
    return levels, opening_sweeps


def build_htf_frame(frame: pd.DataFrame, config: SymbolConfig) -> pd.DataFrame:
    if config.htf_body_close_requalification == "off" or frame.empty:
        return frame.iloc[0:0].copy()
    source_minutes = parse_timeframe_minutes(config.timeframe) or 5.0
    if 15 % int(source_minutes) != 0:
        return frame.iloc[0:0].copy()
    indexed = frame.set_index("time")
    result = indexed.resample("15min", label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        candle_count=("close", "count"),
    )
    expected = int(15 / source_minutes)
    result = result.dropna(subset=["open", "high", "low", "close"])
    return result[result["candle_count"] == expected].reset_index()


def build_htf_requalification_sweeps(
    htf_frame: pd.DataFrame,
    trade_frame: pd.DataFrame,
    trade_date: date,
    vah: float,
    val: float,
    config: SymbolConfig,
) -> list[SweepEvent]:
    if htf_frame.empty or trade_frame.empty or config.htf_body_close_requalification == "off":
        return []

    trade_start = timestamp_on_day(trade_date, config.trade_window_start)
    trade_end = timestamp_on_day(trade_date, config.trade_window_end)
    history_start = pd.Timestamp(trade_date).tz_localize("America/New_York") - pd.Timedelta(days=3)
    selected = time_slice(htf_frame, history_start, trade_end).reset_index(drop=True)
    arrays = detect_htf_arrays(selected, config.htf_body_close_requalification)
    trade_times = trade_frame["time"].reset_index(drop=True)
    candidates: list[SweepEvent] = []

    for array in arrays:
        if not htf_array_near_value_area(array, vah, val, config.vah_val_tolerance):
            continue
        stale = False
        event_time: pd.Timestamp | None = None
        for candle in selected.itertuples(index=False):
            candle_close_time = candle.time + pd.Timedelta(minutes=15)
            if candle_close_time <= array.known_time:
                continue
            if not htf_array_closed_through(array, float(candle.close)):
                continue
            if candle_close_time <= trade_start:
                stale = True
                break
            if candle_close_time >= trade_end:
                break
            event_time = candle_close_time
            break
        if stale or event_time is None:
            continue
        event_index = int(trade_times.searchsorted(event_time, side="left"))
        if event_index >= len(trade_times):
            continue
        direction = "short" if array.direction == "bullish" else "long"
        side = "high" if direction == "short" else "low"
        trigger = array.lower if direction == "short" else array.upper
        known_label = array.known_time.strftime("%H%M")
        level = LiquidityLevel(
            f"htf_15m_{array.array_type}_{array.direction}_{known_label}",
            side,
            trigger,
        )
        candidates.append(
            SweepEvent(
                index=event_index,
                level=level,
                is_fresh=True,
                source="htf_requalification",
                direction=direction,
            )
        )

    output: list[SweepEvent] = []
    for index in sorted({candidate.index for candidate in candidates}):
        same_index = [candidate for candidate in candidates if candidate.index == index]
        directions = {candidate.direction for candidate in same_index}
        if len(directions) != 1:
            continue
        output.append(sorted(same_index, key=lambda candidate: candidate.level.name)[0])
    return output


def detect_htf_arrays(frame: pd.DataFrame, mode: str) -> list[HtfArray]:
    arrays: list[HtfArray] = []
    rows = list(frame.itertuples(index=False))
    if mode in {"fvg", "fvg_ob"}:
        for index in range(len(rows) - 2):
            first = rows[index]
            third = rows[index + 2]
            known_time = third.time + pd.Timedelta(minutes=15)
            if float(first.high) < float(third.low):
                arrays.append(HtfArray("fvg", "bullish", float(first.high), float(third.low), known_time))
            if float(first.low) > float(third.high):
                arrays.append(HtfArray("fvg", "bearish", float(third.high), float(first.low), known_time))
    if mode == "fvg_ob":
        for index in range(len(rows) - 1):
            origin = rows[index]
            impulse = rows[index + 1]
            origin_bearish = float(origin.close) < float(origin.open)
            origin_bullish = float(origin.close) > float(origin.open)
            bullish_displacement = float(impulse.close) > float(impulse.open) and float(impulse.close) > float(origin.high)
            bearish_displacement = float(impulse.close) < float(impulse.open) and float(impulse.close) < float(origin.low)
            direction = "bullish" if origin_bearish and bullish_displacement else "bearish" if origin_bullish and bearish_displacement else ""
            if not direction:
                continue
            arrays.append(
                HtfArray(
                    "order_block",
                    direction,
                    min(float(origin.open), float(origin.close)),
                    max(float(origin.open), float(origin.close)),
                    impulse.time + pd.Timedelta(minutes=15),
                )
            )
    return arrays


def htf_array_near_value_area(array: HtfArray, vah: float, val: float, tolerance: float) -> bool:
    return any(array.lower - tolerance <= level <= array.upper + tolerance for level in [vah, val])


def htf_array_closed_through(array: HtfArray, close: float) -> bool:
    if array.direction == "bullish":
        return close < array.lower
    return close > array.upper


def build_session_liquidity_levels(frame: pd.DataFrame, trade_date: date) -> list[LiquidityLevel]:
    day = pd.Timestamp(trade_date).tz_localize("America/New_York")
    previous_session_date = previous_completed_ny_session_date(frame, trade_date)
    sessions = {
        "asia": (day - pd.Timedelta(hours=4), day),
        "london": (day + pd.Timedelta(hours=2), day + pd.Timedelta(hours=5)),
    }
    if previous_session_date is not None:
        previous_session = pd.Timestamp(previous_session_date).tz_localize("America/New_York")
        sessions.update(
            {
                "ny_am": (
                    previous_session + pd.Timedelta(hours=9, minutes=30),
                    previous_session + pd.Timedelta(hours=12),
                ),
                "ny_pm": (
                    previous_session + pd.Timedelta(hours=13, minutes=30),
                    previous_session + pd.Timedelta(hours=16),
                ),
                "previous_day": (previous_session, previous_session + pd.Timedelta(days=1)),
            }
        )
    levels: list[LiquidityLevel] = []
    for name, (start, end) in sessions.items():
        data = time_slice(frame, start, end)
        if data.empty:
            continue
        high = float(data["high"].max())
        low = float(data["low"].min())
        levels.append(LiquidityLevel(f"{name}_high", "high", high))
        levels.append(LiquidityLevel(f"{name}_low", "low", low))
    return levels


def filter_taken_session_levels(
    frame: pd.DataFrame,
    trade_start: pd.Timestamp,
    session_levels: list[LiquidityLevel],
    opening_sweeps: list[SweepEvent],
    config: SymbolConfig,
) -> list[LiquidityLevel]:
    opening_taken = {sweep.level.name for sweep in opening_sweeps}
    previous_session_date = previous_completed_ny_session_date(frame, trade_start.date())
    filtered: list[LiquidityLevel] = []
    for level in session_levels:
        if level.name in opening_taken:
            continue
        session_end = session_end_for_level(trade_start.date(), level.name, previous_session_date)
        if session_end is None:
            filtered.append(level)
            continue
        taken_before = was_level_taken(frame, session_end, trade_start, level.side, level.price)
        if config.opening_premarket_sweep_mode == "recent_as_setup":
            recent_start = timestamp_on_day(trade_start.date(), config.opening_premarket_sweep_start)
            taken_before = was_level_taken(frame, max(session_end, recent_start), trade_start, level.side, level.price)
        if not taken_before:
            filtered.append(level)
    return filtered


def build_opening_premarket_sweeps(
    frame: pd.DataFrame,
    trade_date: date,
    trade_start: pd.Timestamp,
    session_levels: list[LiquidityLevel],
    config: SymbolConfig,
) -> list[SweepEvent]:
    if config.opening_premarket_sweep_mode != "recent_as_setup":
        return []
    recent_start = timestamp_on_day(trade_date, config.opening_premarket_sweep_start)
    if recent_start >= trade_start:
        return []
    data = time_slice(frame, recent_start, trade_start)
    if data.empty:
        return []
    candidates = [
        level
        for level in session_levels
        if (level.side == "high" and bool((data["high"] >= level.price).any()))
        or (level.side == "low" and bool((data["low"] <= level.price).any()))
    ]
    if not candidates:
        return []
    level = min(candidates, key=lambda item: (liquidity_priority(item.name), item.price))
    return [SweepEvent(index=0, level=level, is_fresh=False, source="opening_premarket")]


def previous_completed_ny_session_date(frame: pd.DataFrame, trade_date: date) -> date | None:
    """Find the latest prior date that actually contains a completed NY cash session."""
    if frame.empty or "time" not in frame.columns:
        return None
    timestamps = pd.to_datetime(frame["time"], errors="coerce", utc=True, format="mixed").dt.tz_convert(
        "America/New_York"
    )
    day = pd.Timestamp(trade_date).tz_localize("America/New_York")
    minute_of_day = timestamps.dt.hour * 60 + timestamps.dt.minute
    regular_session = (minute_of_day >= 9 * 60 + 30) & (minute_of_day < 16 * 60)
    candidates = timestamps[(timestamps < day) & regular_session].dropna()
    for candidate_date in sorted(candidates.dt.date.unique(), reverse=True):
        session = candidates[candidates.dt.date == candidate_date].drop_duplicates().sort_values()
        if len(session) < 12:
            continue
        minutes = session.dt.hour * 60 + session.dt.minute
        first_minute = int(minutes.iloc[0])
        last_minute = int(minutes.iloc[-1])
        completed_close = 12 * 60 + 55 <= last_minute <= 13 * 60 + 5 or last_minute >= 15 * 60 + 45
        if first_minute > 9 * 60 + 35 or not completed_close:
            continue
        gaps = session.diff().dt.total_seconds().div(60).dropna()
        cadence_sample = gaps[(gaps > 0) & (gaps <= 15)]
        if cadence_sample.empty:
            continue
        cadence = float(cadence_sample.mode().iloc[0])
        expected = int((last_minute - first_minute) / cadence) + 1
        if len(session) < max(12, int(expected * 0.8)) or bool((gaps > cadence * 3).any()):
            continue
        return candidate_date
    return None


def session_end_for_level(
    trade_date: date,
    level_name: str,
    previous_session_date: date | None = None,
) -> pd.Timestamp | None:
    day = pd.Timestamp(trade_date).tz_localize("America/New_York")
    if level_name.startswith("asia_"):
        return day
    if level_name.startswith("london_"):
        return day + pd.Timedelta(hours=5)
    previous_session = (
        None
        if previous_session_date is None
        else pd.Timestamp(previous_session_date).tz_localize("America/New_York")
    )
    if level_name.startswith("ny_am_"):
        return None if previous_session is None else previous_session + pd.Timedelta(hours=12)
    if level_name.startswith("ny_pm_"):
        return None if previous_session is None else previous_session + pd.Timedelta(hours=16)
    if level_name.startswith("previous_day_"):
        return None if previous_session is None else previous_session + pd.Timedelta(days=1)
    return None


def was_level_taken(
    frame: pd.DataFrame,
    after: pd.Timestamp,
    before: pd.Timestamp,
    side: str,
    price: float,
) -> bool:
    if after >= before:
        return False
    data = time_slice(frame, after, before)
    if data.empty:
        return False
    if side == "high":
        return bool((data["high"] >= price).any())
    return bool((data["low"] <= price).any())


def build_swing_liquidity_levels(
    frame: pd.DataFrame,
    trade_start: pd.Timestamp,
    config: SymbolConfig,
) -> list[LiquidityLevel]:
    history = time_slice_before(frame, trade_start).tail(config.swing_lookback_candles).reset_index(drop=True)
    if len(history) < 5:
        return []

    swing_highs: list[tuple[pd.Timestamp, float]] = []
    swing_lows: list[tuple[pd.Timestamp, float]] = []
    for idx in range(2, len(history) - 2):
        candle = history.iloc[idx]
        left = history.iloc[idx - 2 : idx]
        right = history.iloc[idx + 1 : idx + 3]
        if candle.high > left["high"].max() and candle.high >= right["high"].max():
            swing_highs.append((candle.time, float(candle.high)))
        if candle.low < left["low"].min() and candle.low <= right["low"].min():
            swing_lows.append((candle.time, float(candle.low)))

    levels: list[LiquidityLevel] = []
    for side, swings in [("high", swing_highs), ("low", swing_lows)]:
        clustered = cluster_swings(swings, config.equal_swing_tolerance)
        if config.swing_liquidity_mode == "strong_only":
            clustered = [cluster for cluster in clustered if cluster[2] >= config.strong_swing_min_touches]
        for number, (_, price, _) in enumerate(clustered[-5:], start=1):
            levels.append(LiquidityLevel(f"swing_{side}_{number}", side, price))
    return levels


def cluster_swings(swings: list[tuple[pd.Timestamp, float]], tolerance: float) -> list[tuple[pd.Timestamp, float, int]]:
    clusters: list[list[tuple[pd.Timestamp, float]]] = []
    for swing in swings:
        for cluster in clusters:
            avg = sum(price for _, price in cluster) / len(cluster)
            if abs(swing[1] - avg) <= tolerance:
                cluster.append(swing)
                break
        else:
            clusters.append([swing])

    result: list[tuple[pd.Timestamp, float, int]] = []
    for cluster in clusters:
        timestamp = max(item[0] for item in cluster)
        price = sum(item[1] for item in cluster) / len(cluster)
        result.append((timestamp, price, len(cluster)))
    return sorted(result, key=lambda item: item[0])


def near_value_area(candle, vah: float, val: float, tolerance: float) -> bool:
    touches_vah = candle.high >= vah - tolerance and candle.low <= vah + tolerance
    touches_val = candle.low <= val + tolerance and candle.high >= val - tolerance
    return touches_vah or touches_val


def detect_sweep(
    index: int,
    candle,
    liquidity_levels: list[LiquidityLevel],
    swept_levels: set[str],
    vah: float,
    val: float,
    config: SymbolConfig,
) -> SweepEvent | None:
    candidates: list[LiquidityLevel] = []
    for level in liquidity_levels:
        if level.name in swept_levels:
            continue
        if not valid_liquidity_for_context(level, candle, vah, val, config):
            continue
        if level.side == "high" and candle.high >= level.price and valid_sweep_rejection(level, candle, config):
            candidates.append(level)
        elif level.side == "low" and candle.low <= level.price and valid_sweep_rejection(level, candle, config):
            candidates.append(level)

    if not candidates:
        return None

    level = min(candidates, key=lambda item: liquidity_sort_key(item, candle))
    return SweepEvent(index=index, level=level)


def valid_liquidity_for_context(
    level: LiquidityLevel,
    candle,
    vah: float,
    val: float,
    config: SymbolConfig,
) -> bool:
    if not config.require_swing_near_value_area or not level.name.startswith("swing_"):
        return True
    reference = vah if level.side == "high" else val
    return abs(level.price - reference) <= config.vah_val_tolerance * 2


def valid_sweep_rejection(level: LiquidityLevel, candle, config: SymbolConfig) -> bool:
    if not config.require_sweep_rejection_close:
        return True
    if level.side == "high":
        return float(candle.close) < level.price
    return float(candle.close) > level.price


def liquidity_sort_key(level: LiquidityLevel, candle) -> tuple[int, float]:
    distance = abs((candle.high if level.side == "high" else candle.low) - level.price)
    return liquidity_priority(level.name), distance


def liquidity_priority(name: str) -> int:
    if name.startswith("ny_am_"):
        return 0
    if name.startswith("london_"):
        return 1
    if name.startswith("asia_"):
        return 2
    if name.startswith("ny_pm_"):
        return 3
    if name.startswith("previous_day_"):
        return 4
    if name.startswith("swing_"):
        return 5
    return 9


def find_directional_setup(
    rows: list,
    sweep_index: int,
    entry_mode: str,
    direction_filter: str = "all",
    setup_type_filter: str = "all",
    min_fvg_points: float = 0.0,
    require_post_sweep_cisd_reference: bool = True,
    invalidate_cisd_before_fvg: bool = True,
    cisd_lookahead_candles: int = 6,
    fvg_window_candles: int = 5,
) -> SetupCandidate | None:
    candidates: list[SetupCandidate] = []
    directions = ["long", "short"] if direction_filter == "all" else [direction_filter]
    for direction in directions:
        cisd_index, cisd_body_stop, cisd_invalidation_level = find_cisd(
            rows,
            sweep_index,
            direction,
            require_post_sweep_cisd_reference,
            cisd_lookahead_candles,
        )
        if cisd_index is None:
            continue
        if cisd_body_stop is None:
            continue
        if entry_mode == "cisd_close":
            candidates.append(
                SetupCandidate(
                    direction,
                    cisd_index,
                    cisd_index,
                    float(rows[cisd_index].close),
                    f"{direction}_cisd_close",
                    cisd_body_stop,
                    cisd_invalidation_level,
                )
            )
            continue
        fvg_index, entry_price, setup_kind, zone_lower, zone_upper, body_edge_entry = find_fvg_or_ifvg(
            rows,
            cisd_index,
            direction,
            entry_mode,
            setup_type_filter,
            min_fvg_points,
            cisd_invalidation_level if invalidate_cisd_before_fvg else None,
            fvg_window_candles,
        )
        if fvg_index is None or entry_price is None or setup_kind is None:
            continue
        candidates.append(
            SetupCandidate(
                direction,
                cisd_index,
                fvg_index,
                entry_price,
                setup_kind,
                cisd_body_stop,
                cisd_invalidation_level,
                zone_lower,
                zone_upper,
                body_edge_entry,
            )
        )

    if not candidates:
        return None
    return min(candidates, key=lambda item: (item.cisd_index, item.fvg_index))


def sweep_stop(candle, direction: str) -> float:
    return float(candle.low if direction == "long" else candle.high)


def setup_stop(rows: list, sweep_index: int, setup: SetupCandidate, stop_model: str) -> float:
    if stop_model == "cisd_body":
        return float(setup.cisd_body_stop)
    if stop_model == "fvg_opposite_edge" and setup.zone_lower is not None and setup.zone_upper is not None:
        return float(setup.zone_lower if setup.direction == "long" else setup.zone_upper)
    if stop_model == "swing_based":
        return swing_based_stop(rows, sweep_index, setup.direction)
    return sweep_stop(rows[sweep_index], setup.direction)


def swing_based_stop(rows: list, sweep_index: int, direction: str) -> float:
    lookback_start = max(0, sweep_index - 100)
    history = rows[lookback_start : sweep_index + 1]
    if len(history) < 5:
        return sweep_stop(rows[sweep_index], direction)

    swings: list[float] = []
    for idx in range(2, len(history) - 2):
        candle = history[idx]
        left = history[idx - 2 : idx]
        right = history[idx + 1 : idx + 3]
        if direction == "long":
            if candle.low < min(item.low for item in left) and candle.low <= min(item.low for item in right):
                swings.append(float(candle.low))
        else:
            if candle.high > max(item.high for item in left) and candle.high >= max(item.high for item in right):
                swings.append(float(candle.high))
    return swings[-1] if swings else sweep_stop(rows[sweep_index], direction)


def valid_stop(direction: str, entry_price: float, stop_price: float) -> bool:
    if direction == "long":
        return stop_price < entry_price
    return stop_price > entry_price


def find_cisd(
    rows: list,
    sweep_index: int,
    direction: str,
    require_post_sweep_reference: bool = True,
    lookahead: int = 6,
) -> tuple[int | None, float | None, float | None]:
    prior_start = max(0, sweep_index - 5)
    prior = rows[prior_start : sweep_index + 1]
    end = min(len(rows), sweep_index + lookahead + 1)

    if direction == "short":
        bullish = [] if require_post_sweep_reference else [c for c in prior if c.close > c.open]
        if not bullish:
            cisd_level = None
            stop_price = None
            invalidation_level = float(rows[sweep_index].high)
        else:
            cisd_level = min(bullish[-1].open, bullish[-1].close)
            stop_price = max(max(c.open, c.close) for c in bullish)
            invalidation_level = max(float(rows[sweep_index].high), max(float(c.high) for c in bullish))
        for idx in range(sweep_index + 1, end):
            candle = rows[idx]
            if candle.close > candle.open:
                bullish.append(candle)
                cisd_level = min(candle.open, candle.close)
                stop_price = max(max(c.open, c.close) for c in bullish)
                invalidation_level = max(invalidation_level, float(candle.high))
                continue
            if cisd_level is not None and candle.close < cisd_level:
                return idx, float(stop_price), float(invalidation_level)
    else:
        bearish = [] if require_post_sweep_reference else [c for c in prior if c.close < c.open]
        if not bearish:
            cisd_level = None
            stop_price = None
            invalidation_level = float(rows[sweep_index].low)
        else:
            cisd_level = max(bearish[-1].open, bearish[-1].close)
            stop_price = min(min(c.open, c.close) for c in bearish)
            invalidation_level = min(float(rows[sweep_index].low), min(float(c.low) for c in bearish))
        for idx in range(sweep_index + 1, end):
            candle = rows[idx]
            if candle.close < candle.open:
                bearish.append(candle)
                cisd_level = max(candle.open, candle.close)
                stop_price = min(min(c.open, c.close) for c in bearish)
                invalidation_level = min(invalidation_level, float(candle.low))
                continue
            if cisd_level is not None and candle.close > cisd_level:
                return idx, float(stop_price), float(invalidation_level)

    return None, None, None


def find_fvg_or_ifvg(
    rows: list,
    cisd_index: int,
    direction: str,
    entry_mode: str = "midpoint",
    setup_type_filter: str = "all",
    min_fvg_points: float = 0.0,
    cisd_invalidation_level: float | None = None,
    window: int = 5,
    search_start_index: int | None = None,
    minimum_completion_index: int | None = None,
) -> tuple[int | None, float | None, str | None, float | None, float | None, float | None]:
    candidates: list[tuple[int, float, str, float, float, float | None]] = []
    search_start = cisd_index if search_start_index is None else max(0, min(search_start_index, cisd_index))
    if setup_type_filter in ["all", "fvg"]:
        for idx in range(search_start, min(len(rows) - 2, cisd_index + window)):
            first = rows[idx]
            third = rows[idx + 2]
            if idx + 2 < cisd_index or (
                minimum_completion_index is not None and idx + 2 < minimum_completion_index
            ):
                continue
            if direction == "long" and first.high < third.low:
                lower, upper = float(first.high), float(third.low)
                if not valid_fvg_size(lower, upper, min_fvg_points):
                    continue
                if cisd_invalidated_before_setup(rows, cisd_index, idx + 2, direction, cisd_invalidation_level):
                    continue
                body_edge = body_edge_near_gap(third, direction)
                candidates.append((
                    idx + 2,
                    entry_from_zone(lower, upper, entry_mode, direction, body_edge),
                    f"bullish_fvg_{entry_mode}",
                    lower,
                    upper,
                    body_edge,
                ))
            if direction == "short" and first.low > third.high:
                lower, upper = float(third.high), float(first.low)
                if not valid_fvg_size(lower, upper, min_fvg_points):
                    continue
                if cisd_invalidated_before_setup(rows, cisd_index, idx + 2, direction, cisd_invalidation_level):
                    continue
                body_edge = body_edge_near_gap(third, direction)
                candidates.append((
                    idx + 2,
                    entry_from_zone(lower, upper, entry_mode, direction, body_edge),
                    f"bearish_fvg_{entry_mode}",
                    lower,
                    upper,
                    body_edge,
                ))

    if setup_type_filter in ["body_fvg", "pd_array"]:
        for idx in range(search_start, min(len(rows) - 2, cisd_index + window)):
            first = rows[idx]
            third = rows[idx + 2]
            if idx + 2 < cisd_index or (
                minimum_completion_index is not None and idx + 2 < minimum_completion_index
            ):
                continue
            first_body_low = min(float(first.open), float(first.close))
            first_body_high = max(float(first.open), float(first.close))
            third_body_low = min(float(third.open), float(third.close))
            third_body_high = max(float(third.open), float(third.close))
            if direction == "long" and first_body_high < third_body_low:
                lower, upper = first_body_high, third_body_low
                if not valid_fvg_size(lower, upper, min_fvg_points):
                    continue
                if cisd_invalidated_before_setup(rows, cisd_index, idx + 2, direction, cisd_invalidation_level):
                    continue
                body_edge = body_edge_near_gap(third, direction)
                candidates.append((
                    idx + 2,
                    entry_from_zone(lower, upper, entry_mode, direction, body_edge),
                    f"bullish_body_fvg_{entry_mode}",
                    lower,
                    upper,
                    body_edge,
                ))
            if direction == "short" and first_body_low > third_body_high:
                lower, upper = third_body_high, first_body_low
                if not valid_fvg_size(lower, upper, min_fvg_points):
                    continue
                if cisd_invalidated_before_setup(rows, cisd_index, idx + 2, direction, cisd_invalidation_level):
                    continue
                body_edge = body_edge_near_gap(third, direction)
                candidates.append((
                    idx + 2,
                    entry_from_zone(lower, upper, entry_mode, direction, body_edge),
                    f"bearish_body_fvg_{entry_mode}",
                    lower,
                    upper,
                    body_edge,
                ))

    # Minimal IFVG approximation: opposite FVG broken by close within the same 5-candle window.
    if setup_type_filter in ["all", "ifvg", "pd_array"]:
        for idx in range(search_start, min(len(rows) - 3, cisd_index + window)):
            first = rows[idx]
            third = rows[idx + 2]
            breaker = rows[idx + 3]
            if idx + 3 < cisd_index or (
                minimum_completion_index is not None and idx + 3 < minimum_completion_index
            ):
                continue
            if direction == "long" and first.low > third.high and breaker.close > first.low:
                lower, upper = float(third.high), float(first.low)
                if not valid_fvg_size(lower, upper, min_fvg_points):
                    continue
                if cisd_invalidated_before_setup(rows, cisd_index, idx + 3, direction, cisd_invalidation_level):
                    continue
                body_edge = body_edge_near_gap(breaker, direction)
                candidates.append((
                    idx + 3,
                    entry_from_zone(lower, upper, entry_mode, direction, body_edge),
                    f"bullish_ifvg_{entry_mode}",
                    lower,
                    upper,
                    body_edge,
                ))
            if direction == "short" and first.high < third.low and breaker.close < first.high:
                lower, upper = float(first.high), float(third.low)
                if not valid_fvg_size(lower, upper, min_fvg_points):
                    continue
                if cisd_invalidated_before_setup(rows, cisd_index, idx + 3, direction, cisd_invalidation_level):
                    continue
                body_edge = body_edge_near_gap(breaker, direction)
                candidates.append((
                    idx + 3,
                    entry_from_zone(lower, upper, entry_mode, direction, body_edge),
                    f"bearish_ifvg_{entry_mode}",
                    lower,
                    upper,
                    body_edge,
                ))

    if not candidates:
        return None, None, None, None, None, None
    return min(candidates, key=lambda item: item[0])


def cisd_invalidated_before_setup(
    rows: list,
    cisd_index: int,
    setup_index: int,
    direction: str,
    invalidation_level: float | None,
) -> bool:
    if invalidation_level is None or setup_index <= cisd_index:
        return False
    candles = rows[cisd_index + 1 : setup_index + 1]
    if direction == "short":
        return any(float(candle.high) > invalidation_level for candle in candles)
    return any(float(candle.low) < invalidation_level for candle in candles)


def valid_fvg_size(lower: float, upper: float, min_fvg_points: float) -> bool:
    return (upper - lower) >= min_fvg_points


def allowed_trade_date(trade_date: date, allowed_weekdays: str) -> bool:
    if allowed_weekdays == "all":
        return True
    allowed = {part.strip() for part in allowed_weekdays.split(",") if part.strip()}
    weekday = pd.Timestamp(trade_date).day_name()
    return weekday in allowed


def timestamp_on_day(trade_date: date, clock: str) -> pd.Timestamp:
    hour_text, minute_text = clock.split(":", maxsplit=1)
    return pd.Timestamp(trade_date).tz_localize("America/New_York") + pd.Timedelta(
        hours=int(hour_text),
        minutes=int(minute_text),
    )


def compute_first30_range(frame: pd.DataFrame, trade_date: date) -> float | None:
    day = pd.Timestamp(trade_date).tz_localize("America/New_York")
    start = day + pd.Timedelta(hours=9, minutes=30)
    end = day + pd.Timedelta(hours=10)
    first30 = time_slice(frame, start, end)
    if first30.empty:
        return None
    return float(first30["high"].max() - first30["low"].min())


def compute_first30_directionality(
    frame: pd.DataFrame,
    trade_date: date,
    *,
    as_of: pd.Timestamp | None = None,
) -> float | None:
    day = pd.Timestamp(trade_date).tz_localize("America/New_York")
    start = day + pd.Timedelta(hours=9, minutes=30)
    end = day + pd.Timedelta(hours=10)
    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize("America/New_York")
        else:
            cutoff = cutoff.tz_convert("America/New_York")
        end = min(end, cutoff)
    first30 = time_slice(frame, start, end)
    if first30.empty:
        return None
    first_range = float(first30["high"].max() - first30["low"].min())
    if first_range <= 0:
        return 0.0
    first_body = abs(float(first30.iloc[-1]["close"] - first30.iloc[0]["open"]))
    return first_body / first_range


def blocked_by_first30_range_filter(
    timestamp: pd.Timestamp,
    first30_range: float | None,
    config: SymbolConfig,
) -> bool:
    if config.first30_range_filter != "live_safe_max":
        return False
    if config.first30_range_max is None or first30_range is None:
        return False
    ten_am = timestamp.normalize() + pd.Timedelta(hours=10)
    if timestamp < ten_am:
        return False
    return first30_range > config.first30_range_max


def blocked_by_swing_first30_directionality(
    liquidity_name: str,
    first30_directionality: float | None,
    config: SymbolConfig,
    *,
    as_of: pd.Timestamp | None = None,
) -> bool:
    if config.swing_first30_directionality_min is None:
        return False
    if not liquidity_name.startswith("swing_"):
        return False
    if as_of is not None:
        timestamp = pd.Timestamp(as_of)
        ten_am = timestamp.normalize() + pd.Timedelta(hours=10)
        if timestamp < ten_am:
            return False
    if first30_directionality is None:
        return False
    return first30_directionality < config.swing_first30_directionality_min


def entry_from_zone(
    lower: float,
    upper: float,
    entry_mode: str,
    direction: str,
    body_edge_entry: float | None = None,
) -> float:
    zone_size = upper - lower
    if entry_mode == "start":
        return upper if direction == "long" else lower
    if entry_mode == "body_end" and body_edge_entry is not None:
        return body_edge_entry
    if entry_mode == "quarter_25":
        return upper - zone_size * 0.25 if direction == "long" else lower + zone_size * 0.25
    if entry_mode == "ote_62":
        return upper - zone_size * 0.62 if direction == "long" else lower + zone_size * 0.62
    if entry_mode == "ote_705":
        return upper - zone_size * 0.705 if direction == "long" else lower + zone_size * 0.705
    if entry_mode == "ote_79":
        return upper - zone_size * 0.79 if direction == "long" else lower + zone_size * 0.79
    if entry_mode == "lower":
        return lower
    if entry_mode == "upper":
        return upper
    return (lower + upper) / 2


def body_edge_near_gap(candle, direction: str) -> float:
    body_low = min(float(candle.open), float(candle.close))
    body_high = max(float(candle.open), float(candle.close))
    return body_low if direction == "long" else body_high


def find_entry_fill(
    rows: list,
    fvg_index: int,
    direction: str,
    entry_price: float,
    target_price: float,
    allow_setup_candle: bool = False,
    min_delay_candles: int = 0,
    require_close_away: bool = False,
    zone_lower: float | None = None,
    zone_upper: float | None = None,
    latest_entry_time: pd.Timestamp | None = None,
    cancel_on_opposite_cisd: bool = False,
    cisd_invalidation_level: float | None = None,
    cancel_target_before_fill: bool = True,
    reject_entry_target_same_bar: bool = True,
) -> int | None:
    return find_entry_resolution(
        rows,
        fvg_index,
        direction,
        entry_price,
        target_price,
        allow_setup_candle,
        min_delay_candles,
        require_close_away,
        zone_lower,
        zone_upper,
        latest_entry_time,
        cancel_on_opposite_cisd,
        cisd_invalidation_level,
        cancel_target_before_fill,
        reject_entry_target_same_bar,
    ).entry_index


def find_entry_resolution(
    rows: list,
    fvg_index: int,
    direction: str,
    entry_price: float,
    target_price: float,
    allow_setup_candle: bool = False,
    min_delay_candles: int = 0,
    require_close_away: bool = False,
    zone_lower: float | None = None,
    zone_upper: float | None = None,
    latest_entry_time: pd.Timestamp | None = None,
    cancel_on_opposite_cisd: bool = False,
    cisd_invalidation_level: float | None = None,
    cancel_target_before_fill: bool = True,
    reject_entry_target_same_bar: bool = True,
) -> PendingOrderResolution:
    start_index = fvg_index if allow_setup_candle else fvg_index + 1
    start_index += max(0, min_delay_candles)
    close_away_seen = not require_close_away
    for idx in range(start_index, len(rows)):
        candle = rows[idx]
        if latest_entry_time is not None and candle.time > latest_entry_time:
            return PendingOrderResolution(None, "CANCELLED_LATEST_ENTRY_CUTOFF", candle.time)
        if cancel_on_opposite_cisd and pending_invalidated(candle, direction, cisd_invalidation_level):
            return PendingOrderResolution(None, "CANCELLED_OPPOSITE_CISD", candle.time)
        target_hit = candle.high >= target_price if direction == "long" else candle.low <= target_price
        entry_hit = candle.low <= entry_price <= candle.high
        if cancel_target_before_fill and target_hit and not entry_hit:
            return PendingOrderResolution(None, "CANCELLED_TARGET_BEFORE_FILL", candle.time)
        if close_away_seen and entry_hit:
            ambiguity = "ENTRY_AND_TARGET_TOUCHED_SAME_BAR" if cancel_target_before_fill and target_hit else ""
            if ambiguity and reject_entry_target_same_bar:
                return PendingOrderResolution(None, "CANCELLED_AMBIGUOUS_INTRABAR", candle.time, ambiguity)
            return PendingOrderResolution(idx, "FILLED", candle.time, ambiguity)
        if require_close_away and zone_lower is not None and zone_upper is not None:
            close_away_seen = candle.close > zone_upper if direction == "long" else candle.close < zone_lower
    terminal_time = rows[-1].time if rows else None
    return PendingOrderResolution(None, "CANCELLED_TRADE_WINDOW_END", terminal_time)


def pending_invalidated(candle, direction: str, cisd_invalidation_level: float | None) -> bool:
    if cisd_invalidation_level is None:
        return False
    if direction == "long":
        return float(candle.close) < cisd_invalidation_level
    return float(candle.close) > cisd_invalidation_level


def result_to_manual_outcome(result: str) -> str:
    if result == "win":
        return "TP"
    if result in {"loss", "loss_same_bar", "reduced_loss"}:
        return "SL"
    if result == "breakeven":
        return "BE"
    return "OPEN"


def result_to_terminal_reason(result: str) -> str:
    return {
        "win": "FILLED_TP",
        "loss": "FILLED_SL",
        "loss_same_bar": "FILLED_SL_SAME_BAR",
        "reduced_loss": "FILLED_REDUCED_LOSS",
        "breakeven": "FILLED_BE",
        "open_data_end": "FILLED_OPEN_DATA_END",
    }.get(result, f"FILLED_{result.upper()}")


def apply_entry_cost(entry_price: float, direction: str, config: SymbolConfig) -> float:
    cost = config.spread_points / 2 + config.slippage_points
    adjusted = entry_price - cost if direction == "short" else entry_price + cost
    return round_price_to_tick(adjusted, config.tick_size_points)


def round_price_to_tick(price: float, tick_size: float | None) -> float:
    if tick_size is None:
        return float(price)
    if tick_size <= 0:
        raise ValueError("tick_size_points must be positive.")
    return round(round(float(price) / tick_size) * tick_size, 10)


def is_tick_aligned(price: float, tick_size: float | None, tolerance: float = 1e-8) -> bool:
    if tick_size is None:
        return True
    if tick_size <= 0:
        return False
    return abs(price / tick_size - round(price / tick_size)) <= tolerance


def simulate_exit(
    rows,
    entry_time: pd.Timestamp,
    direction: str,
    entry_price: float,
    stop_price: float,
    target_price: float,
    reward_r: float,
    stop_management: str = "none",
) -> tuple[int, float, str, float]:
    risk = abs(entry_price - stop_price)
    active_stop = stop_price
    half_target_price = (entry_price + target_price) / 2
    management_active = stop_management == "none"
    final = None
    for idx, candle in enumerate(rows):
        final = candle
        if candle.time < entry_time:
            continue
        if direction == "long":
            stop_hit = candle.low <= active_stop
            target_hit = candle.high >= target_price
            half_target_hit = candle.high >= half_target_price
        else:
            stop_hit = candle.high >= active_stop
            target_hit = candle.low <= target_price
            half_target_hit = candle.low <= half_target_price

        if stop_hit and target_hit:
            r_multiple = (active_stop - entry_price) / risk if direction == "long" else (entry_price - active_stop) / risk
            return candle, active_stop, "loss_same_bar", r_multiple
        if stop_hit:
            r_multiple = (active_stop - entry_price) / risk if direction == "long" else (entry_price - active_stop) / risk
            if r_multiple == 0:
                result = "breakeven"
            elif active_stop != stop_price:
                result = "reduced_loss"
            else:
                result = "loss"
            return candle, active_stop, result, r_multiple
        if target_hit:
            return candle, target_price, "win", reward_r
        if not management_active and half_target_hit:
            if stop_management == "be_at_half_target":
                active_stop = entry_price
            elif stop_management == "half_stop_at_half_target":
                active_stop = (entry_price + stop_price) / 2
            management_active = True

    if final is None:
        raise ValueError("simulate_exit received no execution rows")
    exit_price = float(final.close)
    r_multiple = (exit_price - entry_price) / risk if direction == "long" else (entry_price - exit_price) / risk
    return final, exit_price, "open_data_end", r_multiple


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([asdict(trade) for trade in trades])


def lifecycles_to_frame(lifecycles: list[SetupLifecycle]) -> pd.DataFrame:
    return pd.DataFrame([asdict(lifecycle) for lifecycle in lifecycles])


def monthly_stats(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            columns=[
                "month",
                "total_trades",
                "tp",
                "sl",
                "open_trades",
                "win_rate",
                "net_r",
                "mtm_net_r",
            ]
        )

    frame = trades_to_frame(trades)
    frame["entry_dt"] = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    frame["month"] = frame["entry_dt"].dt.strftime("%Y-%m")
    rows: list[dict[str, object]] = []
    for month, group in frame.groupby("month"):
        closed = group[group["result"].isin(CLOSED_RESULTS)]
        tp = int((group["result"] == "win").sum())
        sl = int((closed["r_multiple"] < 0).sum())
        open_trades = int((~group["result"].isin(CLOSED_RESULTS)).sum())
        rows.append(
            {
                "month": month,
                "total_trades": int(len(group)),
                "tp": tp,
                "sl": sl,
                "open_trades": open_trades,
                "win_rate": round(tp / len(closed) * 100, 2) if len(closed) else 0.0,
                "net_r": round(float(closed["r_multiple"].sum()), 2),
                "mtm_net_r": round(float(group["r_multiple"].sum()), 2),
            }
        )
    return pd.DataFrame(rows)


def weekday_stats(trades: list[Trade]) -> pd.DataFrame:
    columns = [
        "weekday",
        "total_trades",
        "tp",
        "sl",
        "open_trades",
        "win_rate",
        "net_r",
        "mtm_net_r",
    ]
    if not trades:
        return pd.DataFrame(columns=columns)

    frame = trades_to_frame(trades)
    frame["entry_dt"] = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    frame["weekday"] = pd.Categorical(
        frame["entry_dt"].dt.day_name(),
        categories=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        ordered=True,
    )
    rows: list[dict[str, object]] = []
    for weekday, group in frame.groupby("weekday", observed=True):
        closed = group[group["result"].isin(CLOSED_RESULTS)]
        tp = int((group["result"] == "win").sum())
        sl = int((closed["r_multiple"] < 0).sum())
        open_trades = int((~group["result"].isin(CLOSED_RESULTS)).sum())
        rows.append(
            {
                "weekday": str(weekday),
                "total_trades": int(len(group)),
                "tp": tp,
                "sl": sl,
                "open_trades": open_trades,
                "win_rate": round(tp / len(closed) * 100, 2) if len(closed) else 0.0,
                "net_r": round(float(closed["r_multiple"].sum()), 2),
                "mtm_net_r": round(float(group["r_multiple"].sum()), 2),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def summarize_trades(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            [
                {
                    "symbol": "",
                    "timeframe": "",
                    "total_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "open_trades": 0,
                    "win_rate": 0.0,
                    "net_r": 0.0,
                    "mtm_net_r": 0.0,
                    "avg_r": 0.0,
                    "max_drawdown_r": 0.0,
                    "profit_factor": 0.0,
                    "best_day": "",
                    "worst_day": "",
                    "long_stats": "",
                    "short_stats": "",
                    "month_stats": "",
                    "hour_stats": "",
                }
            ]
        )

    frame = trades_to_frame(trades)
    frame["date_dt"] = pd.to_datetime(frame["date"])
    frame["entry_dt"] = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    closed = frame[frame["result"].isin(CLOSED_RESULTS)].copy()
    wins = int((frame["result"] == "win").sum())
    losses = int((closed["r_multiple"] < 0).sum())
    open_trades = int((~frame["result"].isin(CLOSED_RESULTS)).sum())
    equity = closed["r_multiple"].cumsum()
    drawdown = equity - equity.cummax()
    gross_win = closed.loc[closed["r_multiple"] > 0, "r_multiple"].sum()
    gross_loss = abs(closed.loc[closed["r_multiple"] < 0, "r_multiple"].sum())
    daily = closed.groupby("date")["r_multiple"].sum()

    summary = {
        "symbol": frame["symbol"].iloc[0],
        "timeframe": frame["timeframe"].iloc[0],
        "total_trades": len(frame),
        "wins": wins,
        "losses": losses,
        "open_trades": open_trades,
        "win_rate": round(wins / len(closed) * 100, 2) if len(closed) else 0.0,
        "net_r": round(closed["r_multiple"].sum(), 2),
        "mtm_net_r": round(frame["r_multiple"].sum(), 2),
        "avg_r": round(closed["r_multiple"].mean(), 2) if len(closed) else 0.0,
        "max_drawdown_r": round(float(drawdown.min()), 2) if len(closed) else 0.0,
        "profit_factor": round(float(gross_win / gross_loss), 2) if gross_loss else 0.0,
        "best_day": f"{daily.idxmax()}:{daily.max():.2f}R" if len(daily) else "",
        "worst_day": f"{daily.idxmin()}:{daily.min():.2f}R" if len(daily) else "",
        "long_stats": compact_group_stats(closed, "direction", "long"),
        "short_stats": compact_group_stats(closed, "direction", "short"),
        "month_stats": compact_month_stats(closed),
        "hour_stats": compact_hour_stats(closed),
    }
    return pd.DataFrame([summary])


def compact_group_stats(frame: pd.DataFrame, column: str, value: str) -> str:
    group = frame[frame[column] == value]
    if group.empty:
        return "0 trades"
    wins = int((group["result"] == "win").sum())
    return f"{len(group)} trades, {wins / len(group) * 100:.1f}% win, {group['r_multiple'].sum():.2f}R"


def compact_month_stats(frame: pd.DataFrame) -> str:
    stats = frame.groupby(frame["entry_dt"].dt.strftime("%Y-%m"))["r_multiple"].agg(["count", "sum"])
    return "; ".join(f"{idx}:{row['count']:.0f}/{row['sum']:.2f}R" for idx, row in stats.iterrows())


def compact_hour_stats(frame: pd.DataFrame) -> str:
    stats = frame.groupby(frame["entry_dt"].dt.hour)["r_multiple"].agg(["count", "sum"])
    return "; ".join(f"{idx:02.0f}:00:{row['count']:.0f}/{row['sum']:.2f}R" for idx, row in stats.iterrows())
