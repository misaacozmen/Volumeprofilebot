"""Independent data, warmup, and inclusive New York session-date windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd


class EvaluationWindowError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EvaluationWindow:
    data_start: date | datetime | pd.Timestamp
    evaluation_start: date | datetime | pd.Timestamp
    evaluation_end: date | datetime | pd.Timestamp
    warmup_bars: int = 0
    timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        start = _as_date(self.evaluation_start)
        end = _as_date(self.evaluation_end)
        data = _as_date(self.data_start)
        if data > start or start > end:
            raise EvaluationWindowError("window dates must satisfy data_start <= evaluation_start <= evaluation_end")
        if isinstance(self.warmup_bars, bool) or not isinstance(self.warmup_bars, int) or self.warmup_bars < 0:
            raise EvaluationWindowError("warmup_bars must be non-negative")
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise EvaluationWindowError("timezone must be an explicit IANA timezone")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise EvaluationWindowError(f"unknown evaluation timezone: {self.timezone}") from exc

    @property
    def evaluation_dates(self) -> list[date]:
        start, end = _as_date(self.evaluation_start), _as_date(self.evaluation_end)
        return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]

    def includes_session(self, value: date | datetime | pd.Timestamp) -> bool:
        session = _as_date(value)
        return _as_date(self.evaluation_start) <= session <= _as_date(self.evaluation_end)

    def split_frame(self, frame: pd.DataFrame, *, time_column: str = "time") -> tuple[pd.DataFrame, pd.DataFrame]:
        if time_column not in frame.columns:
            raise EvaluationWindowError(f"missing time column: {time_column}")
        timestamps = pd.to_datetime(frame[time_column], errors="coerce", utc=True, format="mixed")
        if timestamps.isna().any():
            raise EvaluationWindowError("evaluation frame contains invalid timestamps")
        session_dates = timestamps.dt.tz_convert(self.timezone).dt.date
        evaluation_mask = session_dates.map(self.includes_session)
        eligible_positions = [index for index, value in enumerate(evaluation_mask) if bool(value)]
        first_position = min(eligible_positions) if eligible_positions else len(frame)
        if first_position < self.warmup_bars:
            raise EvaluationWindowError(
                f"insufficient warmup bars: required={self.warmup_bars} available={first_position}"
            )
        warmup_start = first_position - self.warmup_bars
        end_positions = [index for index, value in enumerate(session_dates) if _as_date(value) <= _as_date(self.evaluation_end)]
        end_position = (max(end_positions) + 1) if end_positions else warmup_start
        source = frame.iloc[warmup_start:end_position].copy()
        source_sessions = session_dates.iloc[warmup_start:end_position]
        return source, source.loc[source_sessions.map(self.includes_session).to_numpy()].copy()

    def assert_trades_in_window(self, trades: pd.DataFrame, *, date_column: str = "date") -> None:
        if trades.empty:
            return
        if date_column not in trades.columns:
            raise EvaluationWindowError(f"trade frame missing {date_column}")
        outside = [value for value in trades[date_column] if not self.includes_session(value)]
        if outside:
            raise EvaluationWindowError(f"trade outside evaluation window: {outside[0]}")

    def manifest_fields(self, *, first_eligible_decision_time: object = None, evaluated_sessions: int | None = None) -> dict[str, object]:
        return {
            "data_window": {"start": _as_date(self.data_start).isoformat()},
            "evaluation_window": {"start": _as_date(self.evaluation_start).isoformat(), "end": _as_date(self.evaluation_end).isoformat()},
            "warmup_bars": int(self.warmup_bars),
            "timezone": self.timezone,
            "first_eligible_decision_time": None if first_eligible_decision_time is None else str(first_eligible_decision_time),
            "evaluated_session_count": evaluated_sessions,
        }


def _as_date(value: date | datetime | pd.Timestamp | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError) as exc:
        raise EvaluationWindowError(f"invalid session date: {value!r}") from exc


SESSION_CLASSES = frozenset({"VALID", "PLANNED_CLOSED", "INVALID_DATA", "MISSING_SOURCE"})


def classify_sessions(
    requested: Iterable[date],
    *,
    source_dates: Iterable[date],
    planned_closed: Iterable[date] = (),
    invalid_data: Iterable[date] = (),
) -> dict[date, str]:
    requested_set = set(requested)
    source_set = set(source_dates)
    closed_set, invalid_set = set(planned_closed), set(invalid_data)
    if closed_set & invalid_set:
        raise EvaluationWindowError("a session cannot be both planned-closed and invalid")
    result: dict[date, str] = {}
    for session in sorted(requested_set):
        if session in closed_set:
            state = "PLANNED_CLOSED"
        elif session in invalid_set:
            state = "INVALID_DATA"
        elif session not in source_set:
            state = "MISSING_SOURCE"
        else:
            state = "VALID"
        result[session] = state
    return result


def coverage_report(
    classification: Mapping[date, str],
    *,
    evaluated: Iterable[date] = (),
    non_promotable: bool | None = None,
) -> dict[str, object]:
    states = list(classification.values())
    if any(state not in SESSION_CLASSES for state in states):
        raise EvaluationWindowError("invalid session coverage class")
    evaluated_set = set(evaluated)
    counts = {state.lower(): sum(item == state for item in states) for state in SESSION_CLASSES}
    valid = counts["valid"]
    valid_dates = {key for key, value in classification.items() if value == "VALID"}
    evaluated_set &= valid_dates
    denominator = len(states) - counts["planned_closed"]
    result = {
        "requested_sessions": len(states),
        "planned_closed_sessions": counts["planned_closed"],
        "valid_sessions": valid,
        "evaluated_sessions": len(evaluated_set),
        "invalid_sessions": counts["invalid_data"],
        "missing_sessions": counts["missing_source"],
        "coverage_percent": (100.0 * valid / denominator) if denominator else 0.0,
        "classification": {str(key): value for key, value in classification.items()},
        "evaluated_dates": sorted(str(value) for value in evaluated_set),
    }
    if non_promotable is not None:
        result["non_promotable"] = bool(non_promotable)
    return result
