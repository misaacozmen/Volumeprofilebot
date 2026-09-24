from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_super1_xm_mt5_forward as super1  # noqa: E402
from validate_super1_rth_calendar import EXTRACTION_PATHS  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--symlink-fixture-root",
        action="store",
        default=None,
        help="pre-prepared item-11 symlink fixture root; verification never creates or mutates it",
    )


@pytest.fixture
def synthetic_calendar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source_calendar = ROOT / "live_forward/calendars/us_equity_rth_2026.json"
    root = tmp_path / "calendar-fixture"
    root.mkdir()
    calendar = root / "us_equity_rth_2026.json"
    extractions: dict[str, Path] = {}
    calendar_payload = json.loads(source_calendar.read_text(encoding="utf-8"))
    for source in calendar_payload["source_records"]:
        raw_path = ROOT / source["provenance_path"]
        source["sha256"] = super1.core.file_hash(raw_path)
        source["bytes"] = raw_path.stat().st_size
    calendar.write_text(json.dumps(calendar_payload, indent=2) + "\n", encoding="utf-8")
    for source_id, relative in EXTRACTION_PATHS.items():
        extraction = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        source = next(item for item in calendar_payload["source_records"] if item["id"] == source_id)
        extraction["raw_source_sha256"] = source["sha256"]
        target = root / Path(relative).name
        target.write_text(json.dumps(extraction, indent=2) + "\n", encoding="utf-8")
        extractions[source_id] = target

    original_safe_repo_file = super1._safe_repo_file

    def synthetic_safe_repo_file(relative, label):
        if relative == "live_forward/calendars/us_equity_rth_2026.json":
            return calendar
        for source_id, extraction_relative in EXTRACTION_PATHS.items():
            if relative == extraction_relative:
                return extractions[source_id]
        return original_safe_repo_file(relative, label)

    monkeypatch.setattr(super1, "_safe_repo_file", synthetic_safe_repo_file)
    return SimpleNamespace(calendar=calendar, extractions=extractions)
