from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_RUNTIME = ROOT / "tests" / "fixtures" / "super1_xm_mt5_demo_config.json"
sys.path.insert(0, str(ROOT / "scripts"))
import run_super1_xm_mt5_forward as super1
import validate_super1_rth_calendar as validator
from v08_helpers import checkpoint_if_enabled, record_if_enabled
from validate_super1_rth_calendar import (
    EXTRACTION_PATHS,
    EXPECTED_SOURCE_URLS,
    compare_extraction_records,
    load_extraction_record,
)


_ORIGINAL_CALENDAR = ROOT / "live_forward/calendars/us_equity_rth_2026.json"
_SYNTHETIC_ROOT = Path(tempfile.mkdtemp(prefix="super1-calendar-fixture-"))
_SYNTHETIC_CALENDAR = _SYNTHETIC_ROOT / "us_equity_rth_2026.json"
_SYNTHETIC_EXTRACTIONS: dict[str, Path] = {}
_calendar_payload = json.loads(_ORIGINAL_CALENDAR.read_text(encoding="utf-8"))
for _source in _calendar_payload["source_records"]:
    _raw_path = ROOT / _source["provenance_path"]
    _source["sha256"] = super1.core.file_hash(_raw_path)
    _source["bytes"] = _raw_path.stat().st_size
_SYNTHETIC_CALENDAR.write_text(json.dumps(_calendar_payload, indent=2) + "\n", encoding="utf-8")
for _source_id, _relative in EXTRACTION_PATHS.items():
    _extraction = json.loads((ROOT / _relative).read_text(encoding="utf-8"))
    _source = next(item for item in _calendar_payload["source_records"] if item["id"] == _source_id)
    _extraction["raw_source_sha256"] = _source["sha256"]
    _target = _SYNTHETIC_ROOT / Path(_relative).name
    _target.write_text(json.dumps(_extraction, indent=2) + "\n", encoding="utf-8")
    _SYNTHETIC_EXTRACTIONS[_source_id] = _target

_original_safe_repo_file = super1._safe_repo_file


def _synthetic_safe_repo_file(relative, label):
    if relative == "live_forward/calendars/us_equity_rth_2026.json":
        return _SYNTHETIC_CALENDAR
    for source_id, extraction_relative in EXTRACTION_PATHS.items():
        if relative == extraction_relative:
            return _SYNTHETIC_EXTRACTIONS[source_id]
    return _original_safe_repo_file(relative, label)


super1._safe_repo_file = _synthetic_safe_repo_file


def _load_runtime() -> dict:
    runtime = json.loads(FIXTURE_RUNTIME.read_text(encoding="utf-8"))
    runtime["rth_session_calendar"]["sha256"] = super1.core.file_hash(_SYNTHETIC_CALENDAR)
    return runtime


def _load_pair() -> tuple[dict, dict, dict]:
    runtime = _load_runtime()
    loaded = super1.load_verified_rth_calendar(runtime)
    source_by_id = {item["id"]: item for item in loaded["source_records"]}
    extracted = {}
    for source_id, relative in EXTRACTION_PATHS.items():
        source = source_by_id[source_id]
        extracted[source_id] = load_extraction_record(
            _SYNTHETIC_EXTRACTIONS[source_id],
            expected_source_id=source_id,
            expected_url=EXPECTED_SOURCE_URLS[source_id],
            raw_source_path=ROOT / source["provenance_path"],
            raw_source_sha256=source["sha256"],
        )
    return extracted["NASDAQ_TRADING_CALENDAR_2026"], extracted["NYSE_TRADING_CALENDAR_2026"], loaded["sessions"]


def test_independent_calendar_extractions_match_each_other_and_runtime(request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    nasdaq, nyse, sessions = _load_pair()
    comparison = compare_extraction_records(nasdaq, nyse, sessions)
    assert comparison["source_records_agree"] is True
    assert comparison["nasdaq_matches_runtime"] is True
    assert comparison["nyse_matches_runtime"] is True
    record_if_enabled(request, evidence_token)


def test_runtime_calendar_tamper_is_blocked_before_super1_feature_use() -> None:
    runtime = _load_runtime()
    tampered = copy.deepcopy(runtime)
    tampered["rth_session_calendar"]["sha256"] = "0" * 64
    with pytest.raises(super1.Super1FeatureError, match="raw hash mismatch"):
        super1.load_verified_rth_calendar(tampered)


def test_calendar_extraction_mutation_fails_cross_source_and_runtime_checks(request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    nasdaq, nyse, sessions = _load_pair()
    tampered = copy.deepcopy(nasdaq)
    tampered["normalized_records"][0]["status"] = "EARLY_CLOSE"
    tampered["normalized_records"][0]["session_end"] = "13:00"
    comparison = compare_extraction_records(tampered, nyse, sessions)
    assert comparison["source_records_agree"] is False
    assert comparison["nasdaq_matches_runtime"] is False
    assert comparison["nyse_matches_runtime"] is True
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize("source_id", ["NASDAQ_TRADING_CALENDAR_2026", "NYSE_TRADING_CALENDAR_2026"])
def test_calendar_extraction_requires_explicit_source_location(source_id: str, tmp_path: Path) -> None:
    nasdaq, nyse, _ = _load_pair()
    extraction = copy.deepcopy(nasdaq if source_id.startswith("NASDAQ") else nyse)
    extraction["records"][0].pop("source_location")
    path = tmp_path / "tampered-extraction.json"
    source = next(
        item
        for item in json.loads(_SYNTHETIC_CALENDAR.read_text(encoding="utf-8"))["source_records"]
        if item["id"] == source_id
    )
    path.write_text(json.dumps(extraction), encoding="utf-8")
    with pytest.raises(ValueError, match="Extraction record is invalid"):
        load_extraction_record(
            path,
            expected_source_id=source_id,
            expected_url=EXPECTED_SOURCE_URLS[source_id],
            raw_source_path=ROOT / source["provenance_path"],
            raw_source_sha256=source["sha256"],
        )


@pytest.mark.parametrize(
    ("mutation", "expected_check", "expected_error"),
    [
        ("duplicate-key", "source_bytes_hashes", "duplicate JSON key: date"),
        ("repeated-date", "source_bytes_hashes", "Extraction record is invalid"),
        ("outside-date", "source_bytes_hashes", "Extraction record is invalid"),
        ("wrong-provenance", "source_bytes_hashes", "Extraction metadata is invalid"),
        ("coverage-in-gap", "runtime_loader", "does not cover every date exactly once"),
        ("coverage-outside", "runtime_loader", "outside coverage"),
        ("wrong-timezone", "runtime_loader", "identity or timezone is invalid"),
        ("wrong-normal-start", "runtime_loader", "outside the US equity core session"),
        ("wrong-normal-end", "runtime_loader", "outside the US equity core session"),
        ("wrong-early-start", "runtime_loader", "outside the US equity core session"),
        ("wrong-early-end", "runtime_loader", "outside the US equity core session"),
        ("calendar-provenance", "runtime_loader", "source provenance hash mismatch"),
    ],
    ids=[
        "duplicate-key", "repeated-date", "outside-date", "wrong-provenance",
        "coverage-in-gap", "coverage-outside", "wrong-timezone", "wrong-normal-start",
        "wrong-normal-end", "wrong-early-start", "wrong-early-end", "calendar-provenance",
    ],
)
def test_calendar_semantic_mutations_fail_through_full_validator(
    mutation: str, expected_check: str, expected_error: str, monkeypatch, tmp_path: Path, request
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    runtime = _load_runtime()
    source = next(
        item
        for item in json.loads(_SYNTHETIC_CALENDAR.read_text(encoding="utf-8"))["source_records"]
        if item["id"] == "NASDAQ_TRADING_CALENDAR_2026"
    )
    extraction_original = _SYNTHETIC_EXTRACTIONS["NASDAQ_TRADING_CALENDAR_2026"]
    calendar_original = ROOT / "live_forward/calendars/us_equity_rth_2026.json"
    target = tmp_path / (
        "calendar-mutated.json" if expected_check == "runtime_loader" else "nasdaq-mutated.json"
    )
    if expected_check == "source_bytes_hashes":
        if mutation == "duplicate-key":
            extracted = json.loads(extraction_original.read_text(encoding="utf-8"))
            encoded = json.dumps(extracted, indent=2)
            marker = '"records": ['
            start = encoded.index(marker)
            date = extracted["records"][0]["date"]
            needle = f'"date": "{date}"'
            index = encoded.index(needle, start)
            encoded = encoded[:index] + f'{needle}, "date": "{date}"' + encoded[index + len(needle):]
            target.write_text(encoded + "\n", encoding="utf-8")
        else:
            extracted = json.loads(extraction_original.read_text(encoding="utf-8"))
            if mutation == "repeated-date":
                extracted["records"][1]["date"] = extracted["records"][0]["date"]
            elif mutation == "outside-date":
                extracted["records"][0]["date"] = "2025-12-31"
            elif mutation == "wrong-provenance":
                extracted["raw_source_sha256"] = "0" * 64
            target.write_text(json.dumps(extracted), encoding="utf-8")
    else:
        calendar = json.loads(calendar_original.read_text(encoding="utf-8"))
        if mutation == "coverage-in-gap":
            calendar["sessions"] = calendar["sessions"][:-1]
        elif mutation == "coverage-outside":
            calendar["sessions"][0]["date"] = "2027-01-01"
        elif mutation == "wrong-timezone":
            calendar["timezone"] = "UTC"
        elif mutation == "wrong-normal-start":
            next(row for row in calendar["sessions"] if row["date"] == "2026-01-02")["start"] = "10:00"
        elif mutation == "wrong-normal-end":
            next(row for row in calendar["sessions"] if row["date"] == "2026-01-02")["end"] = "15:00"
        elif mutation == "wrong-early-start":
            next(row for row in calendar["sessions"] if row["date"] == "2026-11-27")["start"] = "10:00"
        elif mutation == "wrong-early-end":
            next(row for row in calendar["sessions"] if row["date"] == "2026-11-27")["end"] = "14:00"
        elif mutation == "calendar-provenance":
            calendar["source_records"][0]["sha256"] = "0" * 64
        target.write_text(json.dumps(calendar), encoding="utf-8")
        runtime["rth_session_calendar"]["sha256"] = super1.core.file_hash(target)

    original_safe = super1._safe_repo_file

    def safe_file(relative, label):
        if (
            expected_check == "source_bytes_hashes"
            and label == "Calendar extraction"
            and relative == validator.EXTRACTION_PATHS["NASDAQ_TRADING_CALENDAR_2026"]
        ):
            return target
        if expected_check == "runtime_loader" and label == "RTH calendar":
            return target
        return original_safe(relative, label)

    monkeypatch.setattr(super1, "_safe_repo_file", safe_file)
    result = validator.validate(runtime)
    assert result["status"] == "FAIL"
    failed = next(check for check in result["checks"] if check["status"] == "FAIL")
    assert failed["name"] == expected_check
    assert expected_error in json.dumps(failed, sort_keys=True)
    record_if_enabled(request, evidence_token)
