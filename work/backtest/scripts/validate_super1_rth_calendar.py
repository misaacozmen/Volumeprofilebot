"""Independent local validation for the sealed 2026 US equity RTH calendar."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

import run_super1_xm_mt5_forward as super1


EXPECTED_CLOSED = frozenset(
    {
        "2026-01-01",
        "2026-01-19",
        "2026-02-16",
        "2026-04-03",
        "2026-05-25",
        "2026-06-19",
        "2026-07-03",
        "2026-09-07",
        "2026-11-26",
        "2026-12-25",
    }
)
EXPECTED_EARLY_CLOSE = frozenset({"2026-11-27", "2026-12-24"})
EXPECTED_SOURCE_IDS = frozenset(
    {"NASDAQ_TRADING_CALENDAR_2026", "NYSE_TRADING_CALENDAR_2026"}
)
EXPECTED_EXAMPLES = {
    "2026-08-31": "2026-08-28T15:59:00-04:00",
    "2026-09-08": "2026-09-04T15:59:00-04:00",
    "2026-11-30": "2026-11-27T12:59:00-05:00",
    "2026-12-28": "2026-12-24T12:59:00-05:00",
    "2026-07-02": "2026-07-01T15:59:00-04:00",
}
EXTRACTION_PATHS = {
    "NASDAQ_TRADING_CALENDAR_2026": "live_forward/calendars/provenance/nasdaq-2026-extracted.json",
    "NYSE_TRADING_CALENDAR_2026": "live_forward/calendars/provenance/nyse-2026-extracted.json",
}
EXPECTED_SOURCE_URLS = {
    "NASDAQ_TRADING_CALENDAR_2026": "https://www.nasdaqtrader.com/Trader.aspx?id=calendar",
    "NYSE_TRADING_CALENDAR_2026": "https://www.nyse.com/publicdocs/nyse/ICE_NYSE_2026_Yearly_Trading_Calendar.pdf",
}


def _check(name: str, passed: bool, **details: Any) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", **details}


def _event_records_from_runtime(sessions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "date": date,
            "status": row["state"],
            "session_end": row.get("end"),
        }
        for date, row in sorted(sessions.items())
        if row["state"] in {"CLOSED", "EARLY_CLOSE"} and row.get("reason") != "WEEKEND"
    ]


def load_extraction_record(
    path: Path,
    *,
    expected_source_id: str,
    expected_url: str,
    raw_source_path: Path,
    raw_source_sha256: str,
) -> dict[str, Any]:
    """Load one independently produced source extraction and verify its raw binding."""
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        extraction = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("duplicate JSON key: "):
            raise
        raise ValueError(f"Extraction record is invalid: {path.name}") from exc
    if not isinstance(extraction, dict):
        raise ValueError(f"Extraction root is not an object: {path.name}")
    if (
        extraction.get("schema_version") != 1
        or extraction.get("source_id") != expected_source_id
        or extraction.get("source_url") != expected_url
        or extraction.get("raw_source_path")
        != raw_source_path.relative_to(super1.ROOT).as_posix()
        or extraction.get("raw_source_sha256") != raw_source_sha256
        or not isinstance(extraction.get("extraction_method"), str)
        or not extraction.get("extraction_method")
        or not isinstance(extraction.get("visual_manual_review"), bool)
    ):
        raise ValueError(f"Extraction metadata is invalid: {path.name}")
    if not raw_source_path.is_file() or super1.core.file_hash(raw_source_path) != raw_source_sha256:
        raise ValueError(f"Raw source hash is invalid: {raw_source_path.name}")
    records = extraction.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError(f"Extraction records are missing: {path.name}")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"Extraction record is invalid: {path.name}")
        date = record.get("date")
        status = record.get("status")
        location = record.get("source_location")
        normalized_date = None
        if isinstance(date, str):
            try:
                parsed_date = pd.Timestamp(date)
                normalized_date = parsed_date.strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                normalized_date = None
        if (
            not isinstance(date, str)
            or normalized_date != date
            or normalized_date is None
            or not normalized_date.startswith("2026-")
            or date in seen
            or status not in {"CLOSED", "EARLY_CLOSE"}
            or not isinstance(location, dict)
            or not location
        ):
            raise ValueError(f"Extraction record is invalid: {path.name}")
        session_end = record.get("session_end")
        if status == "CLOSED" and session_end is not None:
            raise ValueError(f"Closed extraction has a session end: {path.name}")
        if status == "EARLY_CLOSE" and session_end != "13:00":
            raise ValueError(f"Early-close extraction has an invalid end: {path.name}")
        if "timezone" in record and record.get("timezone") != super1.core.TZ:
            raise ValueError(f"Extraction record has an invalid timezone: {path.name}")
        seen.add(date)
        normalized.append({"date": date, "status": status, "session_end": session_end})
    extraction["normalized_records"] = sorted(normalized, key=lambda item: item["date"])
    return extraction


def compare_extraction_records(
    nasdaq: dict[str, Any], nyse: dict[str, Any], sessions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Compare source records independently; callers can mutate one fixture for negative tests."""
    nasdaq_records = nasdaq.get("normalized_records") or nasdaq.get("records")
    nyse_records = nyse.get("normalized_records") or nyse.get("records")
    runtime_records = _event_records_from_runtime(sessions)
    return {
        "source_records_agree": nasdaq_records == nyse_records,
        "nasdaq_matches_runtime": nasdaq_records == runtime_records,
        "nyse_matches_runtime": nyse_records == runtime_records,
        "nasdaq_records": nasdaq_records,
        "nyse_records": nyse_records,
        "runtime_records": runtime_records,
    }


def validate(runtime: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        loaded = super1.load_verified_rth_calendar(runtime)
    except Exception as exc:  # noqa: BLE001 - report all validation failures as data
        return {
            "status": "FAIL",
            "calendar_id": "US_EQUITY_RTH_2026",
            "checks": [_check("runtime_loader", False, error=str(exc))],
        }

    sessions = loaded["sessions"]
    observed_closed = frozenset(
        date for date, row in sessions.items() if row["state"] == "CLOSED"
    )
    observed_early = frozenset(
        date for date, row in sessions.items() if row["state"] == "EARLY_CLOSE"
    )
    checks.append(
        _check(
            "schema_identity_coverage",
            loaded["calendar_id"] == "US_EQUITY_RTH_2026"
            and loaded["coverage"] == {"start": "2026-01-01", "end": "2026-12-31"}
            and len(sessions) == 365,
            calendar_id=loaded["calendar_id"],
            coverage=loaded["coverage"],
            session_count=len(sessions),
        )
    )
    checks.append(
        _check(
            "closed_dates_match_nasdaq_and_nyse",
            observed_closed == EXPECTED_CLOSED
            | frozenset(
                date
                for date in pd.date_range("2026-01-01", "2026-12-31")
                if date.weekday() >= 5
                for date in [date.strftime("%Y-%m-%d")]
            ),
            observed=sorted(observed_closed),
            expected_holidays=sorted(EXPECTED_CLOSED),
        )
    )
    checks.append(
        _check(
            "early_close_dates_match_nasdaq_and_nyse",
            observed_early == EXPECTED_EARLY_CLOSE
            and all(sessions[date].get("end") == "13:00" for date in EXPECTED_EARLY_CLOSE),
            observed=sorted(observed_early),
            expected=sorted(EXPECTED_EARLY_CLOSE),
        )
    )
    checks.append(
        _check(
            "normal_session_and_holiday_exception",
            sessions["2026-07-02"].get("end") == "16:00"
            and sessions["2026-07-03"]["state"] == "CLOSED"
            and sessions["2026-11-26"]["state"] == "CLOSED",
            observed={
                date: sessions[date]
                for date in ("2026-07-02", "2026-07-03", "2026-11-26")
            },
        )
    )
    source_ids = frozenset(str(item.get("id")) for item in loaded["source_records"])
    checks.append(
        _check(
            "independent_source_records",
            source_ids == EXPECTED_SOURCE_IDS
            and all(str(item["url"]).startswith("https://") for item in loaded["source_records"]),
            source_ids=sorted(source_ids),
            source_urls=sorted(str(item["url"]) for item in loaded["source_records"]),
        )
    )
    extractions: dict[str, dict[str, Any]] = {}
    extraction_errors: list[str] = []
    for source in loaded["source_records"]:
        source_id = str(source.get("id"))
        try:
            extraction_path = super1._safe_repo_file(EXTRACTION_PATHS[source_id], "Calendar extraction")
            raw_path = super1._safe_repo_file(source["provenance_path"], "RTH source provenance")
            extractions[source_id] = load_extraction_record(
                extraction_path,
                expected_source_id=source_id,
                expected_url=EXPECTED_SOURCE_URLS[source_id],
                raw_source_path=raw_path,
                raw_source_sha256=str(source["sha256"]),
            )
        except (KeyError, TypeError, ValueError, super1.Super1FeatureError) as exc:
            extraction_errors.append(str(exc))
    checks.append(
        _check(
            "source_bytes_hashes",
            not extraction_errors
            and set(extractions) == {"NASDAQ_TRADING_CALENDAR_2026", "NYSE_TRADING_CALENDAR_2026"},
            extraction_paths={key: str((super1.ROOT / value).resolve()) for key, value in EXTRACTION_PATHS.items()},
            errors=extraction_errors,
        )
    )
    if len(extractions) == 2:
        comparison = compare_extraction_records(
            extractions["NASDAQ_TRADING_CALENDAR_2026"],
            extractions["NYSE_TRADING_CALENDAR_2026"],
            sessions,
        )
    else:
        comparison = {
            "source_records_agree": False,
            "nasdaq_matches_runtime": False,
            "nyse_matches_runtime": False,
            "nasdaq_records": [],
            "nyse_records": [],
            "runtime_records": _event_records_from_runtime(sessions),
        }
    checks.append(_check("source_record_agreement", comparison["source_records_agree"], **comparison))
    checks.append(
        _check(
            "runtime_matches_both_sources",
            comparison["nasdaq_matches_runtime"] and comparison["nyse_matches_runtime"],
            nasdaq_matches_runtime=comparison["nasdaq_matches_runtime"],
            nyse_matches_runtime=comparison["nyse_matches_runtime"],
        )
    )

    example_results: dict[str, Any] = {}
    for current_date, expected_previous_close in EXPECTED_EXAMPLES.items():
        current = pd.Timestamp(current_date)
        previous = current - pd.Timedelta(days=1)
        while sessions[previous.strftime("%Y-%m-%d")]["state"] == "CLOSED":
            previous -= pd.Timedelta(days=1)
        row = sessions[previous.strftime("%Y-%m-%d")]
        close = pd.Timestamp(
            f"{previous.date()} {row['end']}", tz=super1.core.TZ
        ) - pd.Timedelta(minutes=1)
        example_results[current_date] = {
            "previous_rth_date": previous.strftime("%Y-%m-%d"),
            "previous_rth_close": close.isoformat(),
            "expected": expected_previous_close,
            "pass": close.isoformat() == expected_previous_close,
        }
    checks.append(
        _check(
            "required_previous_session_examples",
            all(item["pass"] for item in example_results.values()),
            examples=example_results,
        )
    )
    feature_path_error = None
    feature_direction = None
    try:
        probe = object.__new__(super1.Super1XmMt5DemoOrderClient)
        probe.config = runtime
        probe.prices = lambda symbol, start, end: (
            end,
            [
                {
                    "snapshotTimeUTC": "2026-08-04T19:59:00Z",
                    "openPrice": {"bid": 100.0},
                    "closePrice": {"bid": 100.0},
                },
                {
                    "snapshotTimeUTC": "2026-08-05T13:30:00Z",
                    "openPrice": {"bid": 101.0},
                    "closePrice": {"bid": 101.0},
                },
            ],
        )
        feature_direction = probe._overnight_direction("US100Cash", "2026-08-05")
    except Exception as exc:  # noqa: BLE001 - surface feature-path evidence in the report
        feature_path_error = str(exc)
    checks.append(
        _check(
            "real_super1_calendar_feature_path",
            feature_direction == "up",
            direction=feature_direction,
            error=feature_path_error,
        )
    )
    return {
        "status": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
        "calendar_id": loaded["calendar_id"],
        "calendar_path": loaded["path"],
        "calendar_sha256": loaded["sha256"],
        "source_records": loaded["source_records"],
        "extraction_records": {
            key: {
                "path": str((super1.ROOT / EXTRACTION_PATHS[key]).resolve()),
                "raw_source_sha256": value["raw_source_sha256"],
                "record_count": len(value["normalized_records"]),
            }
            for key, value in extractions.items()
        },
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=super1.RUNTIME_CONFIG)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(json.loads(args.runtime.read_text(encoding="utf-8")))
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
