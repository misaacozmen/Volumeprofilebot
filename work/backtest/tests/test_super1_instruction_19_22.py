from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_public_identity_scan_without_private_denylist_is_explicitly_blocked(tmp_path: Path) -> None:
    report = tmp_path / "public-scan.json"
    result = subprocess.run(
        [sys.executable, "scripts/scan_public_broker_identity.py", "--root", str(ROOT), "--report", str(report)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "UNASSESSED_MISSING_DENYLIST"
    assert payload["working_tree_match_count"] is None
    assert payload["reachable_history_match_count"] is None


def test_history_rehearsal_without_private_denylist_is_explicitly_blocked(tmp_path: Path) -> None:
    report = tmp_path / "history-rehearsal.json"
    result = subprocess.run(
        [sys.executable, "scripts/audit_and_rehearse_history_sanitization.py", "--source", str(ROOT), "--report", str(report)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "UNASSESSED_MISSING_DENYLIST"
    assert payload["sanitized_mirror_match_count"] is None


def test_public_scanner_rejects_a_denylist_inside_repository(tmp_path: Path) -> None:
    denylist = ROOT / "tests" / ".private-denylist-fixture.json"
    report = tmp_path / "public-scan.json"
    denylist.write_text('{"denylist":["fixture-secret"]}\n', encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, "scripts/scan_public_broker_identity.py", "--root", str(ROOT), "--denylist", str(denylist), "--report", str(report)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        denylist.unlink(missing_ok=True)
    assert result.returncode != 0
    assert "outside the source repository" in result.stderr
