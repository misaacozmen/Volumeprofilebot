from __future__ import annotations

from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import sqlite3
import time

import pytest

from backtest.dukascopy_acquisition import (
    AcquisitionDeferred,
    AcquisitionCoordinator,
    AcquisitionRunLedger,
    HttpCas,
    ProviderProcessLock,
    RateLimitController,
    parse_retry_after,
    rate_limit_delay,
    sha256_bytes,
)
from scripts.reacquire_invalid_sessions_v5 import load_authoritative_targets


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def test_retry_after_seconds_and_http_date_are_parsed_without_sleep() -> None:
    assert parse_retry_after("12", now=NOW) == 12.0
    assert parse_retry_after("Wed, 14 Sep 2026 12:01:00 GMT", now=NOW) == 60.0
    assert parse_retry_after("not-a-date", now=NOW) is None


def test_429_delay_is_header_max_base_plus_deterministic_positive_jitter() -> None:
    delay, parsed = rate_limit_delay(1, "10", now=NOW, rng=lambda: 1)
    assert parsed == 10.0
    assert delay > 60.0


def test_persisted_circuit_defers_and_half_open_success_resets(tmp_path) -> None:
    controller = RateLimitController(tmp_path / "state.json", rng=lambda: 1)
    item = controller.record_429("datafeed.dukascopy.com", None, now=NOW, transport_fixture=True)
    with pytest.raises(AcquisitionDeferred):
        controller.before_request("datafeed.dukascopy.com", now=NOW, transport_fixture=True)
    later = datetime.fromisoformat(item["next_retry_at_utc"].replace("Z", "+00:00"))
    controller.before_request("datafeed.dukascopy.com", now=later, transport_fixture=True)
    controller.record_success("datafeed.dukascopy.com", now=later, transport_fixture=True)
    assert controller.store.read()["hosts"]["datafeed.dukascopy.com"]["circuit"] == "CLOSED"


def test_stale_lock_requires_pid_and_start_token_match(tmp_path) -> None:
    path = tmp_path / "provider.lock"
    path.write_text(json.dumps({"pid": 44, "process_start_token": "old"}), encoding="utf-8")
    lock = ProviderProcessLock(path, pid=55, token="new")
    lock.acquire(process_alive=lambda pid: True, token_for_pid=lambda pid: "current")
    assert lock.held
    lock.release()


def test_cas_is_content_addressed_and_reuses_verified_body(tmp_path) -> None:
    cas = HttpCas(tmp_path / "cas")
    digest, path = cas.put(b"locked-body")
    assert path.name == f"{digest}.bi5"
    assert cas.put(b"locked-body") == (digest, path)


def test_authoritative_inventory_has_113_targets_and_missing_nq_record() -> None:
    targets = load_authoritative_targets(
        __import__("pathlib").Path("data/provenance/dukascopy_v4/frozen_invalid_leg_days_v4.csv")
    )
    assert len(targets) == 113
    assert any(str(row["date"]) == "2025-04-16" and row["leg"] == "nq" for row in targets)


def _coordinator_process_worker(fixture_root: str, cas_root: str, ledger_path: str, result_queue) -> None:
    coordinator = AcquisitionCoordinator(fixture_root=fixture_root, cas_root=cas_root, ledger_path=ledger_path)
    original = coordinator._fixture_bytes

    def slow_fixture(*args, **kwargs):
        time.sleep(0.5)
        return original(*args, **kwargs)

    coordinator._fixture_bytes = slow_fixture
    try:
        result = coordinator.run(
            run_id="a" * 40,
            source_commit="b" * 40,
            source_tree_sha256="c" * 64,
            artifacts=[{"artifact_id": "one", "fixture_path": "one.bin", "sha256": sha256_bytes(b"fixture-body"), "bytes": 12}],
        )
    except Exception as exc:
        result_queue.put(("ERROR", str(exc)))
    else:
        result_queue.put(("OK", result[0]["state"]))
    finally:
        coordinator.close()


def test_fixture_coordinator_rejects_provider_and_verifies_cas_on_resume(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    body = b"fixture-body"
    (fixture_root / "one.bin").write_bytes(body)
    with pytest.raises(ValueError, match="provider calls are forbidden"):
        AcquisitionCoordinator(
            fixture_root=fixture_root,
            cas_root=tmp_path / "cas-provider",
            ledger_path=tmp_path / "provider.sqlite3",
            provider=lambda: None,
        )
    coordinator = AcquisitionCoordinator(fixture_root=fixture_root, cas_root=tmp_path / "cas", ledger_path=tmp_path / "ledger.sqlite3")
    spec = {"artifact_id": "one", "fixture_path": "one.bin", "sha256": sha256_bytes(body), "bytes": len(body)}
    first = coordinator.run(run_id="a" * 40, source_commit="b" * 40, source_tree_sha256="c" * 64, artifacts=[spec])
    assert first[0]["state"] == "COMMITTED"
    Path(first[0]["cas_path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="CAS evidence"):
        coordinator.run(run_id="a" * 40, source_commit="b" * 40, source_tree_sha256="c" * 64, artifacts=[spec])
    coordinator.close()


def test_fixture_coordinator_rejects_declared_hash_and_byte_count_mismatch(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "one.bin").write_bytes(b"fixture-body")
    coordinator = AcquisitionCoordinator(fixture_root=fixture_root, cas_root=tmp_path / "cas", ledger_path=tmp_path / "ledger.sqlite3")
    with pytest.raises(ValueError, match="declared CAS identity"):
        coordinator.run(run_id="a" * 40, source_commit="b" * 40, source_tree_sha256="c" * 64, artifacts=[{"artifact_id": "one", "fixture_path": "one.bin", "sha256": "d" * 64, "bytes": 12}])
    with pytest.raises(ValueError, match="declared CAS identity"):
        coordinator.run(run_id="b" * 40, source_commit="b" * 40, source_tree_sha256="c" * 64, artifacts=[{"artifact_id": "one", "fixture_path": "one.bin", "sha256": sha256_bytes(b"fixture-body"), "bytes": 11}])
    coordinator.close()


def test_forged_committed_row_never_resumes_as_verified(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "one.bin").write_bytes(b"fixture-body")
    db_path = tmp_path / "ledger.sqlite3"
    ledger = AcquisitionRunLedger(db_path)
    ledger.start_run("a" * 40, source_commit="b" * 40, source_tree_sha256="c" * 64)
    assert ledger.reserve_artifact("a" * 40, "one", "one.bin") == "IN_PROGRESS"
    ledger.connection.execute(
        "UPDATE acquisition_artifacts SET state='COMMITTED',body_sha256=?,body_bytes=? WHERE run_id=? AND artifact_id=?",
        ("d" * 64, 12, "a" * 40, "one"),
    )
    ledger.connection.commit()
    ledger.close()
    coordinator = AcquisitionCoordinator(fixture_root=fixture_root, cas_root=tmp_path / "cas", ledger_path=db_path)
    with pytest.raises(ValueError, match="CAS evidence"):
        coordinator.run(
            run_id="a" * 40,
            source_commit="b" * 40,
            source_tree_sha256="c" * 64,
            artifacts=[{"artifact_id": "one", "fixture_path": "one.bin", "sha256": sha256_bytes(b"fixture-body"), "bytes": 12}],
        )
    coordinator.close()


def test_same_run_artifact_race_has_one_lease_owner_and_one_terminal_commit(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "one.bin").write_bytes(b"fixture-body")
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_coordinator_process_worker, args=(str(fixture_root), str(tmp_path / "cas"), str(tmp_path / "ledger.sqlite3"), queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    results = [queue.get(timeout=5) for _ in processes]
    assert sorted(results) == [("ERROR", "acquisition artifact lease is held by another live process"), ("OK", "COMMITTED")]
    connection = sqlite3.connect(tmp_path / "ledger.sqlite3")
    assert connection.execute("SELECT state FROM acquisition_artifacts").fetchone() == ("COMMITTED",)
    connection.close()
