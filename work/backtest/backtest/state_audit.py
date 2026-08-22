from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date

import pandas as pd

from .config import SymbolConfig
from .manual_state import (
    ManualStateConfig,
    ManualStateDayResult,
    build_independent_htf_frame,
    run_manual_state_day,
)


@dataclass(frozen=True)
class PrefixViolation:
    trade_date: str
    cutoff: str
    component: str
    full_snapshot: tuple[tuple[object, ...], ...]
    prefix_snapshot: tuple[tuple[object, ...], ...]


def state_snapshot(day: ManualStateDayResult, cutoff: pd.Timestamp) -> dict[str, tuple[tuple[object, ...], ...]]:
    context = tuple(
        sorted(
            (
                item.time,
                item.gate,
                item.preferred_direction,
                item.source,
                item.event_id,
            )
            for item in day.context_triggers
            if pd.Timestamp(item.known_time or item.time) <= cutoff
        )
    )
    cisd = tuple(
        sorted(
            (
                item.confirm_time,
                item.direction,
                item.anchor_time,
                round(item.body_level, 8),
                round(item.invalidation_level, 8),
            )
            for item in day.cisd_events
            if pd.Timestamp(item.confirm_known_time or item.confirm_time) <= cutoff
        )
    )
    decisions = []
    for item in day.decisions:
        known_terminal = bool(item.terminal_known_time) and pd.Timestamp(item.terminal_known_time) <= cutoff
        known_entry = bool(item.entry_known_time) and pd.Timestamp(item.entry_known_time) <= cutoff
        if not (known_terminal or known_entry):
            continue
        if (
            item.terminal_reason == "CANCELLED_TRADE_WINDOW_END"
            and pd.Timestamp(item.terminal_known_time) == cutoff
        ):
            continue
        decisions.append(
            (
                item.order_id,
                item.setup_state,
                item.order_state,
                item.final_decision,
                item.direction,
                item.cisd_time,
                item.fvg_time,
                item.entry_time,
                item.entry_price,
                item.stop_price,
                item.target_price,
            )
        )
    return {
        "context": context,
        "cisd": cisd,
        "decisions": tuple(sorted(decisions)),
    }


def prefix_invariance_violations(
    frame: pd.DataFrame,
    config: SymbolConfig,
    trade_date: date,
    cutoffs: list[str],
    state_config: ManualStateConfig | None = None,
    *,
    htf_frame: pd.DataFrame | None = None,
) -> list[PrefixViolation]:
    base = state_config or ManualStateConfig()
    effective_htf = (
        htf_frame
        if htf_frame is not None
        else build_independent_htf_frame(frame, config.timeframe, base.htf_timeframe_minutes)
    )
    full_day = run_manual_state_day(
        frame,
        config,
        trade_date,
        base,
        htf_frame_override=effective_htf,
    )
    if full_day.data_state == "INVALID":
        return []
    violations: list[PrefixViolation] = []
    for clock in cutoffs:
        cutoff = pd.Timestamp(f"{trade_date} {clock}", tz="America/New_York")
        configured_end = pd.Timestamp(
            f"{trade_date} {config.trade_window_end}",
            tz="America/New_York",
        )
        if cutoff > configured_end:
            continue
        prefix_frame = frame[frame["time"] <= cutoff].copy()
        prefix_state_config = replace(
            base,
            reject_invalid_data=False,
        )
        prefix_symbol_config = replace(config, trade_window_end=clock)
        prefix_day = run_manual_state_day(
            prefix_frame,
            prefix_symbol_config,
            trade_date,
            prefix_state_config,
            htf_frame_override=effective_htf,
        )
        full_snapshot = state_snapshot(full_day, cutoff)
        prefix_snapshot = state_snapshot(prefix_day, cutoff)
        for component in full_snapshot:
            if full_snapshot[component] != prefix_snapshot[component]:
                violations.append(
                    PrefixViolation(
                        str(trade_date),
                        cutoff.isoformat(),
                        component,
                        full_snapshot[component],
                        prefix_snapshot[component],
                    )
                )
    return violations


def effective_config_record(day: ManualStateDayResult) -> dict[str, object]:
    return {
        "date": day.trade_date,
        "data_state": day.data_state,
        "data_reasons": "|".join(day.data_reasons),
        **day.effective_config,
    }


def pipeline_records(day: ManualStateDayResult) -> list[dict[str, object]]:
    return [{"date": day.trade_date, **asdict(item)} for item in day.pipeline]
