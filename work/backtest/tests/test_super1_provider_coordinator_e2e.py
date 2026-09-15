from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


HARNESS = r'''
import json, sqlite3, sys, time
from hashlib import sha256
from pathlib import Path
from backtest.dukascopy_acquisition import AcquisitionCoordinator, RateLimitController

root = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
run_id = sys.argv[3]
url = "https://datafeed.dukascopy.com/test.bin"
body = b"provider-e2e-body"
def provider(item):
    if mode == "timeout":
        raise TimeoutError("deadline")
    if mode == "crash":
        raise RuntimeError("child crashed")
    if mode == "concurrent":
        time.sleep(0.5)
    status = {"ok": 200, "429": 429, "500": 500}.get(mode, 200)
    if status == 200:
        evidence_body = body
        evidence_sha = sha256(body).hexdigest()
        evidence_size = len(body)
    else:
        evidence_body = b""
        evidence_sha = None
        evidence_size = 0
    return {
        "status": status, "url": url, "headers": {"retry-after": "1"} if status == 429 else {},
        "body": evidence_body, "body_sha256": evidence_sha, "body_byte_count": evidence_size,
        "provenance": {"provider": "Dukascopy", "status": status, "url_sha256": sha256(url.encode()).hexdigest(), "body_sha256": evidence_sha, "body_byte_count": evidence_size},
    }

controller = RateLimitController(root / "state.json", rng=lambda: 0)
coordinator = AcquisitionCoordinator(fixture_root=None, cas_root=root / "cas", ledger_path=root / "ledger.sqlite3", provider=provider, mode="provider", rate_controller=controller, host="datafeed.dukascopy.com")
try:
    if mode == "resume":
        state = controller._load()
        state["hosts"]["datafeed.dukascopy.com"]["next_retry_at_utc"] = "1970-01-01T00:00:00Z"
        state["hosts"]["datafeed.dukascopy.com"]["circuit"] = "OPEN"
        state["hosts"]["datafeed.dukascopy.com"]["last_provider_start_at_utc"] = "1970-01-01T00:00:00Z"
        controller._write(state)
    try:
        result = coordinator.run(run_id=run_id, source_commit="b" * 40, source_tree_sha256="c" * 40, artifacts=[{"artifact_id": "artifact", "url": url}], repository_root=None)
        second = coordinator.run(run_id=run_id, source_commit="b" * 40, source_tree_sha256="c" * 40, artifacts=[{"artifact_id": "artifact", "url": url}], repository_root=None) if mode == "ok" else None
        outcome = {"ok": True, "state": result[0]["state"]}
        if second is not None:
            outcome["second_state"] = second[0]["state"]
    except Exception as exc:
        outcome = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
finally:
    coordinator.close()
with sqlite3.connect(root / "ledger.sqlite3") as db:
    rows = db.execute("SELECT state FROM acquisition_artifacts").fetchall()
state = json.loads((root / "state.json").read_text())
events = state["request_events"]
print(json.dumps({"outcome": outcome, "artifact_states": rows, "events": events, "provider_call_count": state["provider_call_count"], "circuit": state["hosts"].get("datafeed.dukascopy.com", {}).get("circuit"), "cas_count": len(list((root / "cas").rglob("*.bi5")))}, sort_keys=True))
sys.exit(0 if outcome["ok"] else 1)
'''


SPACING_HARNESS = r'''
import json, sys
from datetime import datetime, timedelta, timezone
from backtest.dukascopy_acquisition import AcquisitionDeferred, RateLimitController

root = sys.argv[1]
host = "datafeed.dukascopy.com"
now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
controller = RateLimitController(root + "/state.json", rng=lambda: 0)
first = controller.before_request(host, now=now, run_id="run", artifact_id="one", transport_fixture=True)
controller.finish_request(first, status=200, body_sha256="a" * 64, body_byte_count=1, endpoint="https://datafeed.dukascopy.com/test.bin", request_url_sha256="b" * 64, finished_at=now, transport_fixture=True)
controller.record_success(host, now=now, transport_fixture=True)
spacing_error = None
try:
    controller.before_request(host, now=now + timedelta(seconds=1), run_id="run", artifact_id="two", transport_fixture=True)
except AcquisitionDeferred as exc:
    spacing_error = exc.reason
second = controller.before_request(host, now=now + timedelta(seconds=2), run_id="run", artifact_id="two", transport_fixture=True)
controller.finish_request(second, status=200, body_sha256="c" * 64, body_byte_count=1, endpoint="https://datafeed.dukascopy.com/test.bin", request_url_sha256="b" * 64, finished_at=now + timedelta(seconds=2), transport_fixture=True)
state = controller.store.read()
print(json.dumps({"spacing_error": spacing_error, "last_start": state["hosts"][host]["last_provider_start_at_utc"], "event_count": len(state["request_events"]), "all_terminal": all(event["terminal"] for event in state["request_events"])}, sort_keys=True))
'''


def _run(tmp_path: Path, mode: str, run_id: str = "a" * 64) -> dict[str, object]:
    completed = subprocess.run([sys.executable, "-c", HARNESS, str(tmp_path), mode, run_id], cwd=ROOT, capture_output=True, text=True, check=False)
    assert completed.stdout.strip(), completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    payload["returncode"] = completed.returncode
    return payload


@pytest.mark.parametrize("mode,status,circuit", [("ok", 200, "CLOSED"), ("429", 429, "OPEN"), ("500", 500, "OPEN")])
def test_provider_cli_statuses_commit_only_valid_200(tmp_path: Path, mode: str, status: int, circuit: str) -> None:
    result = _run(tmp_path, mode)
    assert (result["returncode"] == 0) is (status == 200)
    expected_states = [["COMMITTED"]] if status == 200 else [["IN_PROGRESS"]]
    assert result["artifact_states"] == expected_states
    assert result["provider_call_count"] == 1
    assert result["circuit"] == circuit
    assert result["cas_count"] == (1 if status == 200 else 0)
    event = result["events"][0]
    assert event["terminal"] is True and event["status"] == status
    if status == 200:
        assert result["outcome"]["second_state"] == "COMMITTED"
        assert len(result["events"]) == 1


@pytest.mark.parametrize("mode,error_code", [("timeout", "TRANSPORT_TIMEOUT"), ("crash", "PROVIDER_PROCESS_CRASH")])
def test_provider_cli_timeout_and_crash_are_terminal_and_not_committed(tmp_path: Path, mode: str, error_code: str) -> None:
    result = _run(tmp_path, mode)
    assert result["returncode"] != 0
    assert result["artifact_states"] == [["IN_PROGRESS"]]
    assert result["provider_call_count"] == 1 and result["cas_count"] == 0
    assert result["events"][0]["terminal"] is True and result["events"][0]["error_code"] == error_code


def test_provider_cli_crash_resume_commits_after_persisted_recovery(tmp_path: Path) -> None:
    first = _run(tmp_path, "crash")
    assert first["returncode"] != 0 and first["artifact_states"] == [["IN_PROGRESS"]]
    resumed = _run(tmp_path, "resume")
    assert resumed["returncode"] == 0 and resumed["artifact_states"] == [["COMMITTED"]]
    assert resumed["provider_call_count"] == 2 and resumed["cas_count"] == 1
    assert all(event["terminal"] is True for event in resumed["events"])


def test_provider_rate_controller_spacing_is_persistent_in_subprocess(tmp_path: Path) -> None:
    completed = subprocess.run([sys.executable, "-c", SPACING_HARNESS, str(tmp_path)], cwd=ROOT, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    assert result == {"all_terminal": True, "event_count": 2, "last_start": "2026-09-16T12:00:02Z", "spacing_error": "DEFERRED_PROVIDER_SPACING"}


def test_provider_cli_concurrent_lease_has_one_call_and_one_commit(tmp_path: Path) -> None:
    first = subprocess.Popen([sys.executable, "-c", HARNESS, str(tmp_path), "concurrent", "d" * 64], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.1)
    second = subprocess.run([sys.executable, "-c", HARNESS, str(tmp_path), "concurrent", "d" * 64], cwd=ROOT, capture_output=True, text=True, check=False)
    first_stdout, first_stderr = first.communicate(timeout=10)
    assert first.returncode == 0, first_stderr
    assert second.returncode != 0
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        assert db.execute("SELECT state FROM acquisition_artifacts").fetchone() == ("COMMITTED",)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["provider_call_count"] == 1
