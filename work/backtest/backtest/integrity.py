from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .data_inspector import parse_timeframe_minutes
from .gaps import find_gap_events


OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class DataIntegrityIssue:
    code: str
    message: str
    time: str = ""
    source_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class DayDataIntegrity:
    trade_date: str
    valid: bool
    issues: tuple[DataIntegrityIssue, ...]


def conflicting_duplicate_issues(frame: pd.DataFrame) -> list[DataIntegrityIssue]:
    if frame.empty or "time" not in frame.columns:
        return []
    duplicate = frame[frame.duplicated("time", keep=False)]
    issues: list[DataIntegrityIssue] = []
    for timestamp, group in duplicate.groupby("time", sort=True):
        distinct = group[list(OHLCV_COLUMNS)].drop_duplicates()
        if len(distinct) <= 1:
            continue
        sources = (
            tuple(sorted(group["source_file"].astype(str).unique()))
            if "source_file" in group.columns
            else ()
        )
        issues.append(
            DataIntegrityIssue(
                code="CONFLICTING_DUPLICATE_BAR",
                message=f"{len(distinct)} different OHLCV rows share one timestamp.",
                time=pd.Timestamp(timestamp).isoformat(),
                source_files=sources,
            )
        )
    return issues


def structural_ohlcv_issues(frame: pd.DataFrame) -> list[DataIntegrityIssue]:
    issues: list[DataIntegrityIssue] = []
    required = {"time", *OHLCV_COLUMNS}
    missing = sorted(required - set(frame.columns))
    if missing:
        return [DataIntegrityIssue("MISSING_COLUMNS", f"Missing columns: {', '.join(missing)}")]
    parsed_time = pd.to_datetime(frame["time"], errors="coerce", utc=True, format="mixed")
    numeric = frame[list(OHLCV_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    if parsed_time.isna().any():
        issues.append(DataIntegrityIssue("INVALID_TIME", "One or more timestamps are missing."))
    if numeric.isna().any().any():
        issues.append(DataIntegrityIssue("INVALID_OHLCV", "One or more OHLCV values are missing."))
    finite = pd.DataFrame(
        np.isfinite(numeric.to_numpy(dtype=float)),
        index=numeric.index,
        columns=numeric.columns,
    )
    if not bool(finite.all().all()):
        issues.append(DataIntegrityIssue("NON_FINITE_OHLCV", "One or more OHLCV values are not finite."))
    if bool((numeric["volume"] < 0).any()):
        issues.append(DataIntegrityIssue("NEGATIVE_VOLUME", "Volume cannot be negative."))
    if parsed_time.notna().all() and not parsed_time.is_monotonic_increasing:
        issues.append(DataIntegrityIssue("NON_MONOTONIC_TIME", "Timestamps must be monotonic increasing."))
    duplicate_times = parsed_time[parsed_time.duplicated(keep=False) & parsed_time.notna()]
    if not duplicate_times.empty:
        issues.append(
            DataIntegrityIssue(
                "DUPLICATE_BAR",
                "Timestamps must be unique.",
                pd.Timestamp(duplicate_times.iloc[0]).isoformat(),
            )
        )
    invalid_range = (
        (numeric["high"] < numeric["low"])
        | (numeric["open"] > numeric["high"])
        | (numeric["open"] < numeric["low"])
        | (numeric["close"] > numeric["high"])
        | (numeric["close"] < numeric["low"])
    )
    for row in frame.loc[invalid_range, ["time"]].head(10).itertuples(index=False):
        issues.append(
            DataIntegrityIssue(
                "INVALID_OHLC_RANGE",
                "OHLC values violate low <= open/close <= high.",
                pd.Timestamp(row.time).isoformat(),
            )
        )
    issues.extend(conflicting_duplicate_issues(frame))
    return issues


def assess_manual_state_day(
    frame: pd.DataFrame,
    trade_date: date,
    timeframe: str,
    profile_start: pd.Timestamp,
    trade_end: pd.Timestamp,
) -> DayDataIntegrity:
    expected_minutes = parse_timeframe_minutes(timeframe) or 5.0
    relevant = frame[(frame["time"] >= profile_start) & (frame["time"] <= trade_end)].copy()
    issues = structural_ohlcv_issues(relevant)
    if relevant.empty:
        issues.append(DataIntegrityIssue("EMPTY_DAY_WINDOW", "Profile and trade window contain no bars."))
    else:
        for event in find_gap_events(relevant, expected_minutes=expected_minutes):
            if event.affects_profile or event.affects_trade:
                issues.append(
                    DataIntegrityIssue(
                        "MISSING_BARS",
                        f"{event.gap_minutes:g}-minute gap ({event.gap_type}) intersects a required window.",
                        event.time.isoformat(),
                    )
                )
    return DayDataIntegrity(str(trade_date), not issues, tuple(issues))
