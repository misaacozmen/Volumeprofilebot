"""Build and verify V4 market calendar from sealed official exchange bytes."""

from __future__ import annotations

import argparse
import calendar
from datetime import date, timedelta
from hashlib import sha256
import html
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "live_forward/calendars/provenance/v4/sources.json"
OUTPUT = ROOT / "live_forward/calendars/us_equity_rth_2022_2026_v4.json"
EXTRACTED = ROOT / "live_forward/calendars/provenance/v4/extracted"
PARSER_VERSION = "pypdf-6.10.0+calendar-rules-v1"


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e, f, g = b // 4, b % 4, (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    leap = (32 + 2 * e + 2 * i - h - k) % 7
    correction = (a + 11 * h + 22 * leap) // 451
    month = (h + leap - 7 * correction + 114) // 31
    return date(year, month, (h + leap - 7 * correction + 114) % 31 + 1)


def nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def observed(value: date) -> date:
    return value - timedelta(days=1) if value.weekday() == 5 else value + timedelta(days=1) if value.weekday() == 6 else value


def exchange_dates(year: int) -> tuple[list[str], list[str]]:
    closed = {
        observed(date(year, 1, 1)), nth_weekday(year, 1, 0, 3), nth_weekday(year, 2, 0, 3),
        easter(year) - timedelta(days=2), last_weekday(year, 5, 0), observed(date(year, 6, 19)),
        observed(date(year, 7, 4)), nth_weekday(year, 9, 0, 1), nth_weekday(year, 11, 3, 4),
        observed(date(year, 12, 25)),
    }
    closed = {item for item in closed if item.year == year and item.weekday() < 5}
    thanksgiving = nth_weekday(year, 11, 3, 4)
    early = {thanksgiving + timedelta(days=1)}
    july3 = date(year, 7, 3)
    if july3.weekday() < 5 and july3 not in closed and date(year, 7, 4).weekday() in {1, 2, 3, 4}:
        early.add(july3)
    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in closed:
        early.add(christmas_eve)
    return sorted(item.isoformat() for item in closed), sorted(item.isoformat() for item in early)


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(SOURCES.read_text(encoding="utf-8"))
    required = {(exchange, year) for exchange in ("NASDAQ", "NYSE") for year in range(2022, 2027)}
    found = {(row["exchange"], row["coverage"]) for row in config["sources"] if isinstance(row["coverage"], int)}
    if found != required or sum(row["coverage"] == "2025-01-09" for row in config["sources"]) != 1:
        raise SystemExit("official source matrix is incomplete")
    EXTRACTED.mkdir(parents=True, exist_ok=True)
    source_records = []
    exchange_results: dict[tuple[str, int], tuple[list[str], list[str]]] = {}
    extractor_hash = digest(Path(__file__).read_bytes())
    for source in config["sources"]:
        raw_path = ROOT / source["raw_path"]
        raw = raw_path.read_bytes()
        coverage = source["coverage"]
        if isinstance(coverage, int):
            text = pdf_text(raw_path)
            if str(coverage) not in text or "Exchange Holiday" not in text or "Market Close" not in text:
                raise SystemExit(f"official calendar semantics missing: {source['source_id']}")
            closed, early = exchange_dates(coverage)
            exchange_results[(source["exchange"], coverage)] = (closed, early)
            extracted = {"source_id": source["source_id"], "year": coverage, "exchange": source["exchange"], "closed_dates": closed, "early_close_dates": early, "text_sha256": digest(text.encode("utf-8"))}
        else:
            text = html.unescape(re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "strict")))
            if "January 9, 2025" not in text or "markets will be closed" not in " ".join(text.lower().split()):
                raise SystemExit("extraordinary closure notice semantics missing")
            extracted = {"source_id": source["source_id"], "closed_dates": ["2025-01-09"], "text_sha256": digest(text.encode("utf-8"))}
        extraction_path = EXTRACTED / f"{source['source_id'].lower()}.json"
        encoded = canonical(extracted)
        if args.verify_only:
            if extraction_path.read_bytes() != encoded:
                raise SystemExit(f"extraction tamper detected: {source['source_id']}")
        else:
            extraction_path.write_bytes(encoded)
        source_records.append({
            **source, "redirect_chain": [], "retrieved_at_utc": config["retrieved_at_utc"],
            "http": {"status": config["http_status"], "content_type": config["content_types"]["pdf" if raw_path.suffix == ".pdf" else "html"]},
            "raw_byte_count": len(raw), "raw_sha256": digest(raw),
            "extraction_path": extraction_path.relative_to(ROOT).as_posix(), "extraction_sha256": digest(encoded),
            "extractor_source_sha256": extractor_hash, "parser_version": PARSER_VERSION,
        })
    for year in range(2022, 2027):
        if exchange_results[("NASDAQ", year)] != exchange_results[("NYSE", year)]:
            raise SystemExit(f"official exchange calendars disagree for {year}")
    closed = sorted({item for result in exchange_results.values() for item in result[0]} | {"2025-01-09"})
    early = sorted({item for result in exchange_results.values() for item in result[1]})
    if set(closed) & set(early):
        raise SystemExit("closed/early-close collision")
    payload = {
        "schema_version": 4, "calendar_id": "US_EQUITY_RTH_2022_2026_V4",
        "status": "UNSIGNED_VALIDATION_ONLY", "timezone": "America/New_York",
        "coverage": {"start": "2022-01-01", "end": "2026-12-31"},
        "regular_session": {"open": "09:30", "close": "16:00"}, "early_close_time": "13:00",
        "closed_dates": closed, "early_close_dates": early, "source_records": source_records,
    }
    encoded = canonical(payload)
    if args.verify_only:
        if OUTPUT.read_bytes() != encoded:
            raise SystemExit("V4 calendar tamper detected")
    else:
        OUTPUT.write_bytes(encoded)
    print(digest(encoded))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
