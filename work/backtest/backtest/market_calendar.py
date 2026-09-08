"""Versioned market-session calendar loader used by research and production runs."""

from __future__ import annotations

import json
from hashlib import sha256
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd


class MarketCalendarError(ValueError):
    pass


DEFAULT_CALENDAR_ID = "US_EQUITY_RTH_2022_2026_V2"
DEFAULT_CALENDAR_SHA256 = "00720155a8ff465cbd679b8fc2f21f50730d0d478e7db0802aa3a86a0e0e2ea0"
DEFAULT_CALENDAR_PATH = Path(__file__).resolve().parents[1] / "live_forward" / "calendars" / "us_equity_rth_2022_2026_v2.json"


def load_signed_calendar(
    *,
    artifact: str | Path | None = None,
    expected_calendar_id: str = DEFAULT_CALENDAR_ID,
    expected_sha256: str | None = None,
) -> tuple[dict[str, object], str]:
    path = Path(artifact) if artifact is not None else DEFAULT_CALENDAR_PATH
    required_sha256 = expected_sha256
    if required_sha256 is None:
        if artifact is not None:
            raise MarketCalendarError("custom market-calendar artifacts require an expected raw SHA-256")
        required_sha256 = DEFAULT_CALENDAR_SHA256
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketCalendarError("signed market-calendar artifact is unreadable") from exc
    actual_sha256 = sha256(raw).hexdigest()
    if actual_sha256 != str(required_sha256).lower():
        raise MarketCalendarError("signed market-calendar raw SHA-256 mismatch")
    if not isinstance(payload, dict) or payload.get("schema_version") != 2 or payload.get("calendar_id") != expected_calendar_id:
        raise MarketCalendarError("signed market-calendar artifact identity is invalid")
    if payload.get("timezone") != "America/New_York":
        raise MarketCalendarError("signed market-calendar timezone is invalid")
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict) or not isinstance(coverage.get("start"), str) or not isinstance(coverage.get("end"), str):
        raise MarketCalendarError("signed market-calendar coverage is missing")
    regular = payload.get("regular_session")
    if not isinstance(regular, dict) or regular.get("open") != "09:30" or regular.get("close") != "16:00":
        raise MarketCalendarError("signed market-calendar regular session contract is invalid")
    if payload.get("early_close_time") != "13:00":
        raise MarketCalendarError("signed market-calendar early-close contract is invalid")
    closed = payload.get("closed_dates")
    if not isinstance(closed, list) or any(not isinstance(item, str) for item in closed) or len(closed) != len(set(closed)):
        raise MarketCalendarError("signed market-calendar closed date list is invalid")
    early = payload.get("early_close_dates", [])
    if not isinstance(early, list) or any(not isinstance(item, str) for item in early) or len(early) != len(set(early)):
        raise MarketCalendarError("signed market-calendar early-close list is invalid")
    try:
        coverage_start = pd.Timestamp(coverage["start"]).date()
        coverage_end = pd.Timestamp(coverage["end"]).date()
    except (TypeError, ValueError) as exc:
        raise MarketCalendarError("signed market-calendar date is invalid") from exc
    if set(closed) & set(early):
        raise MarketCalendarError("a market date cannot be both closed and early-close")
    try:
        parsed_closed = {pd.Timestamp(item).date() for item in closed}
        parsed_early = {pd.Timestamp(item).date() for item in early}
    except (TypeError, ValueError) as exc:
        raise MarketCalendarError("signed market-calendar holiday date is invalid") from exc
    if any(item < coverage_start or item > coverage_end or item.weekday() >= 5 for item in parsed_closed | parsed_early):
        raise MarketCalendarError("signed market-calendar holiday date is outside weekday coverage")
    source_records = payload.get("source_records")
    if not isinstance(source_records, list) or not source_records:
        raise MarketCalendarError("signed market-calendar provenance is missing")
    source_ids = [str(item.get("id") or "") for item in source_records if isinstance(item, dict)]
    if len(source_ids) != len(source_records) or len(source_ids) != len(set(source_ids)) or any(not item for item in source_ids) or any(
        not str(item.get("url") or "").startswith("https://") or not str(item.get("provenance") or "").strip()
        for item in source_records
    ):
        raise MarketCalendarError("signed market-calendar provenance is invalid")
    source_years = set()
    for item in source_records:
        coverage_year = str(item.get("coverage_year") or "")
        source_years.update(int(value) for value in coverage_year.replace("-", " ").split() if value.isdigit() and len(value) == 4)
    required_years = set(range(coverage_start.year, coverage_end.year + 1))
    if not required_years.issubset(source_years):
        raise MarketCalendarError("signed market-calendar provenance does not cover the full range")
    return payload, actual_sha256


def signed_calendar_contract(*, artifact: str | Path | None = None) -> dict[str, object]:
    payload, digest = load_signed_calendar(artifact=artifact)
    return {
        "calendar_id": str(payload["calendar_id"]),
        "calendar_sha256": digest,
        "timezone": str(payload["timezone"]),
        "coverage": dict(payload["coverage"]),
        "provenance": [
            {"id": str(item["id"]), "url": str(item["url"]), "coverage_year": item.get("coverage_year")}
            for item in payload["source_records"]
        ],
    }


def signed_market_dates(start: date | str | pd.Timestamp, end: date | str | pd.Timestamp, *, artifact: str | Path | None = None) -> list[date]:
    payload, _ = load_signed_calendar(artifact=artifact)
    coverage = payload["coverage"]
    closed = payload["closed_dates"]
    coverage_start = pd.Timestamp(coverage["start"]).date()
    coverage_end = pd.Timestamp(coverage["end"]).date()
    start_date, end_date = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    if start_date > end_date:
        raise MarketCalendarError("requested market-calendar range is reversed")
    if start_date < coverage_start or end_date > coverage_end:
        raise MarketCalendarError("requested market-calendar range is outside signed coverage")
    closed_set = set(closed)
    return [
        value.date()
        for value in pd.date_range(start_date, end_date, freq="1D")
        if value.weekday() < 5 and value.strftime("%Y-%m-%d") not in closed_set
    ]
