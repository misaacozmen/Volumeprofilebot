from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class GapEvent:
    prev_time: pd.Timestamp
    time: pd.Timestamp
    gap_minutes: float
    gap_type: str
    affects_profile: bool
    affects_trade: bool
    skip_date: date


def find_gap_events(frame: pd.DataFrame, expected_minutes: float = 5.0) -> list[GapEvent]:
    data = frame.sort_values("time").copy()
    data["prev_time"] = data["time"].shift()
    data["gap_minutes"] = (data["time"] - data["prev_time"]).dt.total_seconds() / 60
    gaps = data[data["gap_minutes"] > expected_minutes * 1.5]

    events: list[GapEvent] = []
    for row in gaps.itertuples(index=False):
        prev_time = row.prev_time
        cur_time = row.time
        gap_type = classify_gap(prev_time, cur_time, float(row.gap_minutes))
        affects_profile = overlaps_profile_window(prev_time, cur_time)
        affects_trade = overlaps_trade_window(prev_time, cur_time)
        skip_date = infer_skip_date(prev_time, cur_time)
        events.append(
            GapEvent(
                prev_time=prev_time,
                time=cur_time,
                gap_minutes=float(row.gap_minutes),
                gap_type=gap_type,
                affects_profile=affects_profile,
                affects_trade=affects_trade,
                skip_date=skip_date,
            )
        )
    return events


def skip_dates_from_gaps(events: list[GapEvent]) -> set[date]:
    return {
        event.skip_date
        for event in events
        if event.gap_type in {"real_gap", "holiday_or_extended_close"} and (event.affects_profile or event.affects_trade)
    }


def classify_gap(prev_time: pd.Timestamp, cur_time: pd.Timestamp, gap_minutes: float) -> str:
    if prev_time.strftime("%H:%M") == "16:55" and cur_time.strftime("%H:%M") == "17:05" and prev_time.date() == cur_time.date():
        return "missing_17_00_boundary_bar"
    if prev_time.strftime("%H:%M") == "16:55" and cur_time.strftime("%H:%M") == "18:00" and prev_time.date() == cur_time.date():
        return "daily_maintenance"
    if prev_time.weekday() == 4 and cur_time.weekday() == 6 and cur_time.strftime("%H:%M") == "18:00":
        return "weekend_or_friday_early_close"
    if gap_minutes >= 1000:
        return "holiday_or_extended_close"
    return "real_gap"


def infer_skip_date(prev_time: pd.Timestamp, cur_time: pd.Timestamp) -> date:
    if overlaps_trade_window(prev_time, cur_time):
        return cur_time.date()
    if cur_time.hour >= 18:
        return (cur_time + pd.Timedelta(days=1)).date()
    return cur_time.date()


def overlaps_profile_window(prev_time: pd.Timestamp, cur_time: pd.Timestamp) -> bool:
    return overlaps_window(prev_time, cur_time, 18 * 60, 24 * 60) or overlaps_window(prev_time, cur_time, 0, 9 * 60 + 30)


def overlaps_trade_window(prev_time: pd.Timestamp, cur_time: pd.Timestamp) -> bool:
    return overlaps_window(prev_time, cur_time, 9 * 60 + 30, 12 * 60)


def overlaps_window(prev_time: pd.Timestamp, cur_time: pd.Timestamp, start_minute: int, end_minute: int) -> bool:
    for day in pd.date_range(prev_time.date(), cur_time.date(), freq="D"):
        start = pd.Timestamp(day.date()).tz_localize(prev_time.tz) + pd.Timedelta(minutes=start_minute)
        end = pd.Timestamp(day.date()).tz_localize(prev_time.tz) + pd.Timedelta(minutes=end_minute)
        if prev_time < end and cur_time > start:
            return True
    return False
