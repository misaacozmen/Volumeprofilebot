from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from .data_inspector import parse_timeframe_minutes
from .numeric_contracts import finite_float


def apply_pair_risk_rule(
    trades: pd.DataFrame,
    rule: object,
    *,
    group_col: str = "group",
    date_col: str = "date",
    label_col: str = "label",
    tie_break_col: str = "symbol",
    entry_time_col: str = "entry_time",
    terminal_time_col: str | None = None,
    r_col: str = "r_multiple",
    timezone: str = "America/New_York",
) -> pd.DataFrame:
    if rule is None or trades.empty:
        return trades.copy()
    frame = normalize_pair_risk_frame(
        trades,
        group_col=group_col,
        date_col=date_col,
        label_col=label_col,
        tie_break_col=tie_break_col,
        entry_time_col=entry_time_col,
        terminal_time_col=terminal_time_col,
        timezone=timezone,
    )
    if rule == "skip_second_after_first_loss":
        return skip_second_after_first_symbol_loss(frame, group_col, date_col, label_col, r_col)
    return apply_daily_loss_cap(frame, finite_float(rule, "pair_cap_r"), group_col, date_col, r_col)


def normalize_pair_risk_frame(
    trades: pd.DataFrame,
    *,
    group_col: str = "group",
    date_col: str = "date",
    label_col: str = "label",
    tie_break_col: str = "symbol",
    entry_time_col: str = "entry_time",
    terminal_time_col: str | None = None,
    timezone: str = "America/New_York",
) -> pd.DataFrame:
    frame = trades.copy()
    ensure_columns(frame, [group_col, date_col, label_col, entry_time_col])
    if "entry_time_dt" not in frame.columns:
        frame["entry_time_dt"] = pd.to_datetime(frame[entry_time_col], utc=True, format="mixed").dt.tz_convert(timezone)
    else:
        frame["entry_time_dt"] = pd.to_datetime(frame["entry_time_dt"], utc=True, format="mixed").dt.tz_convert(timezone)
    frame["_risk_source_order"] = range(len(frame))
    risk_label_col = tie_break_col if tie_break_col in frame.columns else label_col
    frame["_risk_label"] = frame[risk_label_col].astype(str)
    resolved_terminal_col = terminal_time_col
    if resolved_terminal_col is None:
        resolved_terminal_col = next(
            (
                candidate
                for candidate in ("exit_known_time", "terminal_known_time", "exit_time", "terminal_time")
                if candidate in frame.columns
            ),
            None,
        )
    if resolved_terminal_col is None:
        raise ValueError(
            "Causal pair-risk evaluation requires an exit_time or terminal_time column. "
            "A final R value cannot be used before its terminal event is known."
        )
    ensure_columns(frame, [resolved_terminal_col])
    frame["_risk_terminal_time"] = pd.to_datetime(
        frame[resolved_terminal_col],
        utc=True,
        format="mixed",
        errors="coerce",
    ).dt.tz_convert(timezone)
    if (
        terminal_time_col is None
        and resolved_terminal_col in {"exit_time", "terminal_time"}
        and "timeframe" in frame.columns
    ):
        frame["_risk_terminal_time"] = frame["_risk_terminal_time"] + frame["timeframe"].map(
            lambda value: pd.Timedelta(minutes=float(parse_timeframe_minutes(str(value)) or 0))
        )
    if frame["_risk_terminal_time"].isna().any():
        bad = frame.loc[frame["_risk_terminal_time"].isna(), resolved_terminal_col].head(3).tolist()
        raise ValueError(f"Invalid or missing pair-risk terminal times: {bad}")
    if (frame["_risk_terminal_time"] < frame["entry_time_dt"]).any():
        raise ValueError("Pair-risk terminal time cannot be earlier than entry time.")
    return frame


def apply_daily_loss_cap(
    trades: pd.DataFrame,
    cap_r: float = -1.0,
    group_col: str = "group",
    date_col: str = "date",
    r_col: str = "r_multiple",
) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    for value in trades[r_col]:
        finite_float(value, r_col)
    ensure_columns(
        trades,
        [
            group_col,
            date_col,
            "entry_time_dt",
            "_risk_terminal_time",
            "_risk_label",
            "_risk_source_order",
            r_col,
        ],
    )
    kept = []
    for _, day in sorted_risk_frame(trades, group_col, date_col).groupby([group_col, date_col], sort=False):
        selected_rows = []
        realized_rows: set[object] = set()
        cumulative = 0.0
        for index, row in day.iterrows():
            entry_time = row["entry_time_dt"]
            for selected_index in selected_rows:
                if selected_index in realized_rows:
                    continue
                selected = day.loc[selected_index]
                if selected["_risk_terminal_time"] <= entry_time:
                    cumulative += float(selected[r_col])
                    realized_rows.add(selected_index)
            if cumulative <= cap_r:
                break
            selected_rows.append(index)
        kept.append(day.loc[selected_rows])
    return cleanup_risk_columns(pd.concat(kept, ignore_index=True) if kept else trades.iloc[0:0].copy())


def skip_second_after_first_symbol_loss(
    trades: pd.DataFrame,
    group_col: str = "group",
    date_col: str = "date",
    label_col: str = "label",
    r_col: str = "r_multiple",
) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    for value in trades[r_col]:
        finite_float(value, r_col)
    ensure_columns(
        trades,
        [
            group_col,
            date_col,
            label_col,
            "entry_time_dt",
            "_risk_terminal_time",
            "_risk_label",
            "_risk_source_order",
            r_col,
        ],
    )
    kept = []
    for _, day in sorted_risk_frame(trades, group_col, date_col).groupby([group_col, date_col], sort=False):
        labels_by_first_entry = day.groupby(label_col)["entry_time_dt"].min().sort_values(kind="mergesort")
        if len(labels_by_first_entry) < 2:
            kept.append(day)
            continue
        first_label = labels_by_first_entry.index[0]
        first_label_trades = day[day[label_col] == first_label]
        second_entry = day.loc[day[label_col] != first_label, "entry_time_dt"].min()
        first_label_realized = first_label_trades[first_label_trades["_risk_terminal_time"] <= second_entry]
        first_label_lost_before_second = (
            not first_label_realized.empty and float(first_label_realized[r_col].sum()) < 0
        )
        kept.append(first_label_trades if first_label_lost_before_second else day)
    return cleanup_risk_columns(pd.concat(kept, ignore_index=True) if kept else trades.iloc[0:0].copy())


def sorted_risk_frame(trades: pd.DataFrame, group_col: str, date_col: str) -> pd.DataFrame:
    return trades.sort_values(
        [group_col, date_col, "entry_time_dt", "_risk_label", "_risk_source_order"],
        kind="mergesort",
    )


def cleanup_risk_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.drop(
        columns=[
            column
            for column in ["_risk_label", "_risk_source_order", "_risk_terminal_time"]
            if column in frame.columns
        ]
    )


def ensure_columns(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"Missing required risk columns: {', '.join(missing)}")
