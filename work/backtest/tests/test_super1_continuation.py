from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import zipfile

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from plan_super1_campaign_continuation import (  # noqa: E402
    PARENT_BASELINE,
    RUNTIME,
    super1_harness_hash,
    super1_harness_paths,
)
from v08_helpers import checkpoint_if_enabled, record_if_enabled
from run_capital_forward import BarStore  # noqa: E402
from run_xm_mt5_forward import XmMt5DemoOrderClient  # noqa: E402
from super1_continuation import (  # noqa: E402
    FIXTURE_CASES,
    RSA_SIGNATURE_ALGORITHM,
    STATE_ARTIFACTS,
    compare_tree_inventory,
    build_transition_record,
    canonical_transition_payload,
    file_hash,
    inventory_snapshot_root,
    sqlite_schema_fingerprint,
    tree_inventory,
    validate_actual_transition_evidence,
    validate_fixture_cases,
    validate_real_snapshot_state,
    validate_transition_record,
    _sqlite_rows,
    _jsonl_file,
    write_exclusive_json,
)


def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_bytes(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _signature(path: Path, key: rsa.RSAPrivateKey) -> dict[str, str]:
    return {
        "algorithm": RSA_SIGNATURE_ALGORITHM,
        "padding": "PKCS1v15",
        "hash": "SHA256",
        "payload_sha256": file_hash(path),
        "signature": base64.b64encode(
            key.sign(path.read_bytes(), padding.PKCS1v15(), hashes.SHA256())
        ).decode("ascii"),
    }


def _make_signed_transition(root: Path) -> tuple[dict, bytes, rsa.RSAPrivateKey]:
    root.mkdir()
    key = _private_key()
    lock = root / "campaign_lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "created_at": "2026-08-31T08:00:00+00:00",
                "parent_baseline_sha256": "0" * 64,
                "engine_code_hash": "1" * 64,
                "live_config_hash": "2" * 64,
                "runtime_config_hash": "3" * 64,
                "harness_hash": "4" * 64,
                "feed": "XM_MT5",
                "epics": {"nq": "US100Cash", "spx": "US500Cash"},
                "symbols": {"nq": "NQ", "spx": "SPX"},
                "timeframes": {"nq": "3m", "spx": "5m", "htf": "15m"},
                "execution": "MT5_DEMO_ORDERS",
                "account_login": 1301910045,
                "server": "test-account-server",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    member = root / "orders.jsonl"
    member.write_text('{"order_id":"o1","state":"UNKNOWN"}\n', encoding="utf-8")
    member_spec = {
        "path": member.name,
        "sha256": file_hash(member),
        "bytes": member.stat().st_size,
    }
    manifest = root / "snapshot-manifest.json"
    manifest.write_text(
        json.dumps({"members": [member_spec]}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    release_member = root / "release-member.txt"
    release_member.write_text("release fixture\n", encoding="utf-8")
    archive = root / "release.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
        handle.write(release_member, release_member.name)
    release = root / "release-root.json"
    release_manifest = {
        "schema_version": 1,
        "release_id": "super1-test-release",
        "profile": "super1-windows-demo",
        "archive_file": archive.name,
        "archive_sha256": file_hash(archive),
        "created_at_utc": "2026-08-31T08:00:00+00:00",
        "built_at_utc": "2026-08-31T08:00:00+00:00",
        "git_commit": "test-only",
        "git_dirty": False,
        "python_version": "test-python",
        "python_executable_sha256": "e" * 64,
        "pytest_command": "python -m pytest",
        "pytest_passed": True,
        "pytest_passed_count": 1,
        "dependencies": {"pytest": "test"},
        "artifact_pytest_passed": True,
        "artifact_pytest_count": 1,
        "artifact_pytest_command": "python -m pytest artifact_tests",
        "artifact_test_files": ["test_xm_mt5_forward.py"],
        "locked_dependencies": ["pytest==test"],
        "wheelhouse": [],
        "linux_wheelhouse": [],
        "files": [{"path": release_member.name, "sha256": file_hash(release_member)}],
    }
    release.write_text(json.dumps(release_manifest, sort_keys=True) + "\n", encoding="utf-8")
    release_spec = {
        "status": "VERIFIED",
        "path": release.name,
        "sha256": file_hash(release),
        "bytes": release.stat().st_size,
        "manifest": release_manifest,
        "signature": _signature(release, key),
    }
    lock_payload = json.loads(lock.read_text(encoding="utf-8"))
    lock_binding = {"raw_lock_sha256": file_hash(lock), "fields": lock_payload}
    previous = root / "previous-transition.json"
    previous_payload = {
        "schema_version": 2,
        "campaign_origin": "CURRENT_SUPER1_CAMPAIGN_CONTINUATION",
        "plan_created_at": "2026-08-31T09:00:00+00:00",
        "source_campaign_created_at": "2026-08-31T08:00:00+00:00",
        "predecessor_hash": None,
        "original_campaign_lock_sha256": file_hash(lock),
        "campaign_root_binding": lock_binding,
        "source_root_lock": {
            "status": "VERIFIED",
            "path": lock.name,
            "sha256": file_hash(lock),
        },
        "hashes": {
            "old": {key: "1" * 64 for key in ("release", "runtime", "harness", "contract", "calendar")},
            "new": {
                "release": "1" * 64,
                "runtime": "2" * 64,
                "harness": "3" * 64,
                "contract": "4" * 64,
                "calendar": "5" * 64,
            },
            "unchanged_engine_and_risk": {
                "engine": "b" * 64,
                "frozen_candidate": "c" * 64,
                "strategy_risk": "d" * 64,
            },
        },
        "broker_identity_digest": hashlib.sha256(
            b"1301910045|test-account-server"
        ).hexdigest(),
        "state_schema_versions": {name: 1 for name in STATE_ARTIFACTS},
        "allowed_change_list": [
            "continuation transition record only",
            "new release/runtime/harness verification after source snapshot",
            "RTH calendar binding and local validation evidence",
        ],
        "source_state_snapshot": {"status": "UNRESOLVED", "inventory": list(STATE_ARTIFACTS)},
        "release_root_signature": {"status": "NOT_VERIFIED", "signature": None},
        "previous_transition": {"status": "GENESIS", "path": None, "sha256": None},
    }
    previous.write_text(json.dumps(previous_payload, sort_keys=True) + "\n", encoding="utf-8")
    previous_hash = file_hash(previous)
    record = build_transition_record(
        plan_created_at="2026-08-31T12:00:00+00:00",
        original_campaign_lock_sha256=file_hash(lock),
        previous_transition_hash=previous_hash,
        old_hashes={
            "release": "1" * 64,
            "runtime": "2" * 64,
            "harness": "3" * 64,
            "contract": "4" * 64,
            "calendar": "5" * 64,
        },
        new_hashes={
            "release": "6" * 64,
            "runtime": "7" * 64,
            "harness": "8" * 64,
            "contract": "9" * 64,
            "calendar": "a" * 64,
        },
        unchanged_engine_and_risk={
            "engine": "b" * 64,
            "frozen_candidate": "c" * 64,
            "strategy_risk": "d" * 64,
        },
        broker_identity="1301910045|test-account-server",
        source_snapshot_manifest_sha256=file_hash(manifest),
        state_schema_versions={name: 1 for name in STATE_ARTIFACTS},
        release_root_signature=release_spec,
    )
    record["source_root_lock"] = {
        "status": "VERIFIED",
        "path": lock.name,
        "sha256": file_hash(lock),
        "bytes": lock.stat().st_size,
    }
    record["source_campaign_created_at"] = "2026-08-31T08:00:00+00:00"
    record["campaign_root_binding"] = lock_binding
    record["previous_transition"] = {
        "status": "VERIFIED",
        "path": previous.name,
        "sha256": previous_hash,
        "bytes": previous.stat().st_size,
        "signature": _signature(previous, key),
    }
    record["source_state_snapshot"] = {
        "status": "VERIFIED",
        "path": manifest.name,
        "manifest_path": manifest.name,
        "manifest_sha256": file_hash(manifest),
        "sha256": file_hash(manifest),
        "bytes": manifest.stat().st_size,
        "inventory": list(record["source_state_snapshot"]["inventory"]),
        "members": [member_spec],
    }
    transition = root / "transition.json"
    transition.write_text(
        json.dumps(canonical_transition_payload(record), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    record["transition_evidence"] = {
        "path": transition.name,
        "sha256": file_hash(transition),
        "bytes": transition.stat().st_size,
        "signature": _signature(transition, key),
    }
    return record, _public_bytes(key), key


def test_verified_signature_status_alone_is_not_accepted(request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    record = build_transition_record(
        plan_created_at="2026-08-31T12:00:00+00:00",
        original_campaign_lock_sha256="a" * 64,
        previous_transition_hash="b" * 64,
        old_hashes={"runtime": "c" * 64},
        new_hashes={"runtime": "d" * 64},
        unchanged_engine_and_risk={"engine": "e" * 64},
        broker_identity="digest-only",
        source_snapshot_manifest_sha256="f" * 64,
        state_schema_versions={"order_idempotency_sqlite": 1},
        release_root_signature={"status": "VERIFIED"},
    )
    result = validate_transition_record(record)
    assert result["status"] == "EVIDENCE_NOT_VALIDATED"
    assert result["safe_to_apply"] is False
    record_if_enabled(request, evidence_token)


def test_rsa_transition_payload_binds_all_external_fields_and_chain(tmp_path: Path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    record, public_key, _ = _make_signed_transition(tmp_path / "evidence")
    result = validate_actual_transition_evidence(
        record, tmp_path / "evidence", trusted_public_key=public_key
    )
    assert result["status"] == "PASS"
    assert result["safe_to_apply"] is False

    mutations = []
    changed_account = copy.deepcopy(record)
    changed_account["broker_identity_digest"] = "0" * 64
    mutations.append(changed_account)
    changed_runtime = copy.deepcopy(record)
    changed_runtime["hashes"]["new"]["runtime"] = "0" * 64
    mutations.append(changed_runtime)
    changed_schema = copy.deepcopy(record)
    changed_schema["state_schema_versions"] = {}
    mutations.append(changed_schema)
    for mutated in mutations:
        assert validate_actual_transition_evidence(
            mutated, tmp_path / "evidence", trusted_public_key=public_key
        )["status"] == "FAIL"

    wrong_key = _public_bytes(_private_key())
    assert validate_actual_transition_evidence(
        record, tmp_path / "evidence", trusted_public_key=wrong_key
    )["status"] == "FAIL"

    transition = tmp_path / "evidence" / "transition.json"
    transition.write_bytes(transition.read_bytes() + b"x")
    assert validate_actual_transition_evidence(
        record, tmp_path / "evidence", trusted_public_key=public_key
    )["status"] == "FAIL"
    record_if_enabled(request, evidence_token)


def _resign_transition(record: dict, root: Path, key: rsa.RSAPrivateKey) -> None:
    transition = root / "transition.json"
    transition.write_text(
        json.dumps(canonical_transition_payload(record), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    record["transition_evidence"].update(
        {
            "sha256": file_hash(transition),
            "bytes": transition.stat().st_size,
            "signature": _signature(transition, key),
        }
    )


def test_transition_semantic_negatives_are_rejected_with_fresh_signatures(tmp_path: Path) -> None:
    cases = {
        "account": ("campaign_root_binding.semantic", lambda record, root: record.update({"broker_identity_digest": "0" * 64})),
        "server": (
            "campaign_root_binding.semantic",
            lambda record, root: record["campaign_root_binding"]["fields"].update({"server": "other-server"}),
        ),
        "old_new": (
            "previous_transition.old_new_link",
            lambda record, root: record["hashes"]["old"].update({"runtime": "f" * 64}),
        ),
        "release_manifest": (
            "release_root_signature.manifest_binding",
            lambda record, root: record["release_root_signature"]["manifest"].update({"profile": "other"}),
        ),
        "snapshot_member_loss": (
            "source_state_snapshot.members",
            lambda record, root: record["source_state_snapshot"].update({"members": []}),
        ),
        "frozen_risk": (
            "previous_transition.frozen_risk_link",
            lambda record, root: record["hashes"]["unchanged_engine_and_risk"].update({"engine": "f" * 64}),
        ),
        "missing_schema": (
            "record.state_schema_versions",
            lambda record, root: record.update({"state_schema_versions": {}}),
        ),
    }
    for name, (error, mutate) in cases.items():
        root = tmp_path / name
        record, public_key, key = _make_signed_transition(root)
        mutate(record, root)
        _resign_transition(record, root, key)
        result = validate_actual_transition_evidence(
            record, root, trusted_public_key=public_key
        )
        assert result["status"] == "FAIL"
        assert error in result["errors"]

    root = tmp_path / "archive_member"
    record, public_key, _ = _make_signed_transition(root)
    archive = root / "release.zip"
    archive.write_bytes(archive.read_bytes() + b"corruption")
    result = validate_actual_transition_evidence(record, root, trusted_public_key=public_key)
    assert result["status"] == "FAIL"
    assert "release_root_signature.archive_hash" in result["errors"]

    root = tmp_path / "wrong_trust_root"
    record, _, _ = _make_signed_transition(root)
    assert validate_actual_transition_evidence(
        record, root, trusted_public_key=_public_bytes(_private_key())
    )["status"] == "FAIL"


def test_transition_snapshot_manifest_and_predecessor_mutations_fail(tmp_path: Path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    record, public_key, _ = _make_signed_transition(tmp_path / "evidence")
    root = tmp_path / "evidence"
    manifest = root / "snapshot-manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert validate_actual_transition_evidence(
        record, root, trusted_public_key=public_key
    )["status"] == "FAIL"

    record, public_key, _ = _make_signed_transition(tmp_path / "second")
    predecessor = tmp_path / "second" / "previous-transition.json"
    predecessor.write_text(json.dumps({"predecessor_hash": record["previous_transition_hash"]}), encoding="utf-8")
    assert validate_actual_transition_evidence(
        record, tmp_path / "second", trusted_public_key=public_key
    )["status"] == "FAIL"
    record_if_enabled(request, evidence_token)


def test_exclusive_writer_rejects_existing_source_contained_and_reparse_outputs(tmp_path: Path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    source = tmp_path / "source"
    source.mkdir()
    existing = source / "existing.json"
    existing.write_text("old\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source-contained"):
        write_exclusive_json(source / "new.json", {"x": 1}, source_root=source)
    with pytest.raises(FileExistsError):
        write_exclusive_json(existing, {"x": 2})
    output = tmp_path / "output.json"
    write_exclusive_json(output, {"x": 1})
    with pytest.raises(FileExistsError):
        write_exclusive_json(output, {"x": 2})

    junction = tmp_path / "junction-to-source"
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    assert powershell is not None, "INCOMPLETE: a Windows reparse-point fixture could not be created."
    created = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "New-Item -ItemType Junction -Path "
            + "'" + str(junction).replace("'", "''") + "'"
            + " -Target "
            + "'" + str(source).replace("'", "''") + "' | Out-Null",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0 and junction.is_dir(), (
        "INCOMPLETE: Windows junction/reparse fixture could not be created. "
        + created.stderr.strip()
    )
    with pytest.raises(ValueError, match="source-contained|reparse"):
        write_exclusive_json(junction / "through-junction.json", {"x": 3}, source_root=source)
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize(
    "fixture_case",
    ["existing_empty_output", "existing_nonempty_output", "source_case_equivalent", "source_descendant", "source_exact", "windows_junction"],
    ids=lambda value: value,
)
def test_m01_pre_capture_inventory_and_retention_are_exact_and_case_safe(tmp_path: Path, fixture_case: str, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    del fixture_case
    source = tmp_path / "Source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "source.json").write_bytes(b"source-bytes")
    (source / "nested" / "state.db").write_bytes(b"state")
    target = tmp_path / "Output"
    target.mkdir()
    (target / "existing.json").write_bytes(b"existing")
    source_inventory = tree_inventory(source)
    output_inventory = tree_inventory(target)

    with pytest.raises(ValueError, match="source-contained|case-equivalent"):
        write_exclusive_json(tmp_path / "sOURCE" / "new.json", {"x": 1}, source_root=source)
    with pytest.raises(ValueError, match="source-contained|case-equivalent"):
        write_exclusive_json(source / "nested" / "new.json", {"x": 1}, source_root=source)

    assert compare_tree_inventory(source, source_inventory)["ok"] is True
    assert compare_tree_inventory(target, output_inventory)["ok"] is True
    (target / "existing.json").write_bytes(b"changed")
    assert compare_tree_inventory(target, output_inventory)["ok"] is False
    record_if_enabled(request, evidence_token)


def _create_real_state(root: Path) -> None:
    for relative in (
        "cache",
        "orders",
        "prefix/2026-08-31",
        "finalized",
        "manual",
        "daily_health",
        "sessions/2026-08-31",
        "preflight",
        "runtime",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "campaign_lock.json").write_text(
        json.dumps({
            "schema_version": 1,
            "created_at": "2026-08-31T08:00:00+00:00",
            "parent_baseline_sha256": "0" * 64,
            "engine_code_hash": "1" * 64,
            "live_config_hash": "2" * 64,
            "runtime_config_hash": "3" * 64,
            "harness_hash": "4" * 64,
            "feed": "XM_MT5",
            "epics": {"nq": "US100Cash", "spx": "US500Cash"},
            "symbols": {"nq": "NQ", "spx": "SPX"},
            "timeframes": {"nq": "3m", "spx": "5m", "htf": "15m"},
            "execution": "MT5_DEMO_ORDERS",
            "account_login": 1301910045,
            "server": "test-account-server",
            "magic_number": 260805101,
            "campaign_root": "TEST_SUPER1_CAMPAIGN_ROOT",
        }),
        encoding="utf-8",
    )
    (root / "health.json").write_text(json.dumps({"state": "RUNNING", "campaign": "CURRENT_SUPER1_CAMPAIGN"}), encoding="utf-8")
    store = BarStore(root)
    for index in range(20):
        timestamp = f"2026-08-31T13:{30 + index:02d}:00Z"
        store.ingest(
            "US100Cash",
            __import__("pandas").Timestamp("2026-08-31T14:00:00Z"),
            [{
                "snapshotTimeUTC": timestamp,
                "openPrice": {"bid": 100 + index},
                "highPrice": {"bid": 101 + index},
                "lowPrice": {"bid": 99 + index},
                "closePrice": {"bid": 100.5 + index},
                "lastTradedVolume": 1,
            }],
        )
    store.close()
    broker_history = {
        "schema_version": 1,
        "account_login": 1301910045,
        "server": "test-account-server",
        "campaign_root": "TEST_SUPER1_CAMPAIGN_ROOT",
        "observed_at": "2026-08-31T14:00:00+00:00",
        "terminal_history_days": 365,
        "records": [
            {
                "time_msc": index,
                "ticket": 2000 + index,
                "position_id": 3000 + index,
                "order": 1000 + index,
                "entry": 1,
                "reason": 5,
                "symbol": "US100Cash",
                "volume": 0.1,
                "magic": 260805101,
                "raw_r": 3.0,
            }
            for index in range(13)
        ],
    }
    (root / "orders" / "broker-history.json").write_text(
        json.dumps(broker_history, sort_keys=True), encoding="utf-8"
    )
    (root / "orders" / "positions.json").write_text(
        json.dumps({
            "schema_version": 1,
            "account_login": 1301910045,
            "server": "test-account-server",
            "campaign_root": "TEST_SUPER1_CAMPAIGN_ROOT",
            "observed_at": "2026-08-31T14:00:00+00:00",
            "terminal_history_days": 365,
            "records": [],
        }, sort_keys=True),
        encoding="utf-8",
    )
    client = object.__new__(XmMt5DemoOrderClient)
    client._initialize_order_db(root)
    connection = client._order_connection(root)
    try:
        for index in range(13):
            order_id = f"order-{index:02d}"
            status = (
                "UNKNOWN_NO_SEND"
                if index == 12
                else "CANCEL_UNKNOWN"
                if index == 11
                else "SUBMITTED"
                if index == 10
                else "CLOSED_TP"
            )
            connection.execute(
                "INSERT INTO order_intents VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, status, "FSP:test", "{}", 1000 + index, "created", f"updated-{index}"),
            )
            connection.execute(
                "INSERT INTO order_event_outbox (order_id, event_json, delivered_at) VALUES (?, ?, ?)",
                (order_id, json.dumps({"sequence": index, "order_id": order_id}), None if index % 2 else "acked"),
            )
            connection.execute(
                "INSERT INTO broker_execution_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, 1, order_id, 1000 + index, 2000 + index, 3000 + index, "PARTIAL_FILL" if index == 11 else status, 0.1, 0.1, 100.0, "PROTECTED", json.dumps({"time_msc": index, "ticket": 2000 + index, "broker_order_ticket": 1000 + index}), "checked", f"updated-{index}"),
            )
        connection.commit()
    finally:
        connection.close()
    events = []
    for index in range(13):
        if index == 11:
            events.extend([
                {"event": "CANCEL_ARMED", "order_id": "order-11", "broker_order_ticket": 1011, "ticket": 1011},
                {"event": "CANCEL_UNKNOWN", "order_id": "order-11", "broker_order_ticket": 1011, "ticket": 1011},
            ])
        elif index == 10:
            events.append({"event": "SUBMITTED", "order_id": "order-10", "ticket": 1010})
        events.append({"event": "SUPER1_TERMINAL_R", "state": "R", "time_msc": index, "ticket": 2000 + index, "raw_r": 1.0})
        events.append({"event": "DEAL", "broker_ticket": 2000 + index, "time_msc": index, "ticket": 2000 + index})
    (root / "orders" / "events.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in events), encoding="utf-8"
    )
    (root / "prefix/2026-08-31/1030_1100.json").write_text("{}\n", encoding="utf-8")
    (root / "finalized/2026-08-31.json").write_text("{}\n", encoding="utf-8")
    (root / "manual/2026-08-31.json").write_text("{}\n", encoding="utf-8")
    (root / "daily_health/2026-08-31.json").write_text("{}\n", encoding="utf-8")
    (root / "sessions/2026-08-31/session.json").write_text("{}\n", encoding="utf-8")
    (root / "preflight/2026-08-31.json").write_text("{}\n", encoding="utf-8")


def _sqlite_backup(source: Path, target: Path) -> None:
    source_connection = sqlite3.connect(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def _make_snapshot(source: Path, snapshot: Path) -> None:
    snapshot.mkdir()
    for path in source.rglob("*"):
        if not path.is_file() or path.name in {"capital_bars.sqlite3", "idempotency.sqlite3"}:
            continue
        target = snapshot / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    _sqlite_backup(source / "cache/capital_bars.sqlite3", snapshot / "cache/capital_bars.sqlite3")
    _sqlite_backup(source / "orders/idempotency.sqlite3", snapshot / "orders/idempotency.sqlite3")


def test_inventory_and_real_sqlite_backup_snapshot_reject_all_mutations(tmp_path: Path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    source = tmp_path / "source"
    snapshot = tmp_path / "snapshot"
    _create_real_state(source)
    _make_snapshot(source, snapshot)
    positive = validate_real_snapshot_state(source, snapshot)
    assert positive["status"] == "PASS"
    assert inventory_snapshot_root(source)["launcher_failure"]["path"].endswith("launcher_failure.json")
    assert sqlite_schema_fingerprint(source / "cache/capital_bars.sqlite3")["schema"]

    def mutate_deal(case: Path) -> None:
        path = case / "orders/events.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[1]["broker_ticket"] = 999999
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def mutate_outbox(case: Path) -> None:
        connection = sqlite3.connect(case / "orders/idempotency.sqlite3")
        connection.execute("UPDATE order_event_outbox SET delivered_at='corrupt' WHERE sequence=1")
        connection.commit()
        connection.close()

    def mutate_risk(case: Path) -> None:
        path = case / "orders/events.jsonl"
        rows = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(rows[:-2]) + "\n", encoding="utf-8")

    def mutate_required(case: Path) -> None:
        (case / "health.json").unlink()

    def mutate_campaign(case: Path) -> None:
        path = case / "campaign_lock.json"
        path.write_text(json.dumps({"campaign": "OTHER", "created_at": "other"}), encoding="utf-8")

    for mutation in (mutate_deal, mutate_outbox, mutate_risk, mutate_required, mutate_campaign):
        case = tmp_path / mutation.__name__
        shutil.copytree(snapshot, case)
        mutation(case)
        assert validate_real_snapshot_state(source, case)["status"] == "FAIL"

    assert validate_real_snapshot_state(source, tmp_path / "missing")["status"] == "FAIL"
    assert not all(validate_fixture_cases({}).values())
    record_if_enabled(request, evidence_token)


def test_uncheckpointed_wal_last_commit_is_in_backup_and_wal_loss_is_specific(tmp_path: Path) -> None:
    source = tmp_path / "wal-source"
    snapshot = tmp_path / "wal-snapshot"
    _create_real_state(source)
    writer = sqlite3.connect(source / "cache/capital_bars.sqlite3")
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO minute_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "US100Cash",
                "2026-08-31T14:30:00+00:00",
                123.0,
                124.0,
                122.0,
                123.5,
                1.0,
                "2026-08-31T14:00:00+00:00",
                "wal-last-row",
            ),
        )
        writer.commit()
        assert (source / "cache/capital_bars.sqlite3-wal").exists()
        _make_snapshot(source, snapshot)
        snapshot_rows = _sqlite_rows(snapshot / "cache/capital_bars.sqlite3")
        assert any(
            row[1] == "2026-08-31T14:30:00+00:00"
            for row in snapshot_rows["tables"]["minute_bars"]["rows"]
        )
        assert validate_real_snapshot_state(source, snapshot)["status"] == "PASS"

        bad = tmp_path / "wal-excluded"
        for path in source.rglob("*"):
            if not path.is_file() or path.name.startswith("capital_bars.sqlite3"):
                continue
            target = bad / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        bad_db = bad / "cache/capital_bars.sqlite3"
        bad_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "cache/capital_bars.sqlite3", bad_db)
        failed = validate_real_snapshot_state(source, bad)
        assert failed["status"] == "FAIL"
        assert "minute_bars_and_conflicts.wal_continuity" in failed["errors"]
    finally:
        writer.close()


def test_super1_harness_scope_is_ordered_and_not_capital_scope(request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    runtime = json.loads(RUNTIME.read_text(encoding="utf-8"))
    expected = [
        "run_super1_xm_mt5_forward.py",
        "run_xm_mt5_forward.py",
        "run_capital_forward.py",
        "run_forward_shadow.py",
        Path(runtime["candidate_path"]).name,
        Path(runtime["signal_contract_path"]).name,
        "super1_manifest.json",
    ]
    assert [path.name for path in super1_harness_paths(runtime)] == expected
    assert super1_harness_hash(runtime) != ""
    assert super1_harness_hash(runtime) != __import__("run_capital_forward").harness_hash()
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize(
    "case",
    ["changed_member", "credential_member", "duplicate_member", "extra_member", "forbidden_member", "missing_member", "non_normalized_member", "traversal_member"],
    ids=lambda value: value,
)
def test_m02_release_archive_mutation_is_rejected(case: str, tmp_path: Path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    root = tmp_path / f"m02-archive-{case}"
    record, public_key, key = _make_signed_transition(root)
    files = record["release_root_signature"]["manifest"]["files"]
    if case == "changed_member":
        (root / "release.zip").write_bytes((root / "release.zip").read_bytes() + b"changed")
    else:
        mutated = {"path": "release-member.txt", "sha256": "0" * 64}
        if case == "credential_member":
            mutated["path"] = "credentials/password.txt"
            files.append(mutated)
        elif case == "duplicate_member":
            files.append(copy.deepcopy(files[0]))
        elif case == "extra_member":
            mutated["path"] = "extra.txt"
            files.append(mutated)
        elif case == "forbidden_member":
            mutated["path"] = "deploy/signed.key"
            files.append(mutated)
        elif case == "missing_member":
            files.clear()
        elif case == "non_normalized_member":
            mutated["path"] = "./release-member.txt"
            files[0] = mutated
        elif case == "traversal_member":
            mutated["path"] = "../escape.txt"
            files[0] = mutated
        _resign_transition(record, root, key)
    result = validate_actual_transition_evidence(record, root, trusted_public_key=public_key)
    assert result["status"] == "FAIL", (case, result)
    assert result["errors"], (case, result)
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize(
    "case",
    [
        "account", "calendar_bytes", "campaign_lock", "contract_bytes", "frozen_state",
        "genesis_transition_signature", "harness_bytes", "missing_required_field", "old_new_release_link",
        "r0_archive_sha", "r0_manifest_signature", "r0_member_sha", "r1_archive_sha", "r1_member_sha",
        "release_binding", "replay", "risk_parameters", "runtime_bytes", "server", "snapshot_member",
        "structural_cycle", "synthetic_genesis_flag_only", "synthetic_genesis_root_only", "wrong_trust_root",
    ],
    ids=lambda value: value,
)
def test_m02_transition_mutation_is_rejected_with_fresh_signature(case: str, tmp_path: Path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    root = tmp_path / f"m02-transition-{case}"
    record, public_key, key = _make_signed_transition(root)
    old = record["hashes"]["old"]
    frozen = record["hashes"]["unchanged_engine_and_risk"]
    if case == "account":
        record["broker_identity_digest"] = "0" * 64
    elif case in {"calendar_bytes", "contract_bytes", "harness_bytes", "old_new_release_link", "runtime_bytes", "r0_archive_sha", "r1_archive_sha"}:
        old_key = {"calendar_bytes": "calendar", "contract_bytes": "contract", "harness_bytes": "harness", "old_new_release_link": "release", "runtime_bytes": "runtime"}.get(case)
        if old_key:
            old[old_key] = "f" * 64
        else:
            record["release_root_signature"]["manifest"]["archive_sha256"] = "0" * 64
    elif case in {"frozen_state", "risk_parameters"}:
        frozen["engine"] = "f" * 64
    elif case in {"campaign_lock", "server", "synthetic_genesis_root_only"}:
        record["campaign_root_binding"]["fields"]["server"] = "other-server"
    elif case == "missing_required_field":
        record["state_schema_versions"].pop(next(iter(record["state_schema_versions"])))
    elif case in {"r0_member_sha", "r1_member_sha", "snapshot_member"}:
        if case == "snapshot_member":
            record["source_state_snapshot"]["members"] = []
        else:
            record["source_state_snapshot"]["members"][0]["sha256"] = "0" * 64
    elif case == "release_binding":
        record["release_root_signature"]["manifest"]["profile"] = "other-profile"
    elif case in {"genesis_transition_signature", "r0_manifest_signature"}:
        target = "previous_transition" if case == "genesis_transition_signature" else "release_root_signature"
        record[target]["signature"] = "not-a-valid-signature"
    elif case in {"replay", "structural_cycle"}:
        record["previous_transition"].update(
            {
                "path": record["transition_evidence"]["path"],
                "sha256": record["transition_evidence"]["sha256"],
                "signature": record["transition_evidence"]["signature"],
            }
        )
    elif case in {"synthetic_genesis_flag_only", "wrong_trust_root"}:
        record["previous_transition"]["status"] = "GENESIS"
    else:
        raise AssertionError(f"unhandled M02 transition case: {case}")
    if case != "wrong_trust_root":
        _resign_transition(record, root, key)
        result = validate_actual_transition_evidence(record, root, trusted_public_key=public_key)
    else:
        result = validate_actual_transition_evidence(record, root, trusted_public_key=_public_bytes(_private_key()))
    assert result["status"] == "FAIL", (case, result)
    assert result["errors"], (case, result)
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize(
    "case",
    [
        "cancel_armed", "deal", "duplicate_terminal", "entry_constant", "external_identity", "lookback",
        "magic", "missing_required_field", "missing_required_file", "negative_scale", "nonnegative_scale",
        "out_constant", "outbox_ack", "outbox_content", "outbox_sequence", "position", "reward_map",
        "signed_config_path", "signed_config_sha", "sl_constant", "submitted", "symbol", "terminal_history_days",
        "tp_constant", "unknown",
    ],
    ids=lambda value: value,
)
def test_m03_snapshot_mutation_is_rejected_with_exact_code(case: str, tmp_path: Path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    source = tmp_path / f"m03-source-{case}"
    snapshot = tmp_path / f"m03-snapshot-{case}"
    _create_real_state(source)
    _make_snapshot(source, snapshot)
    if case in {"cancel_armed", "deal", "duplicate_terminal", "external_identity", "symbol"}:
        path = snapshot / "orders/events.jsonl" if case in {"cancel_armed", "deal"} else snapshot / "orders/broker-history.json"
        rows = _jsonl_file(path) if path.suffix == ".jsonl" else json.loads(path.read_text(encoding="utf-8"))
        if case == "cancel_armed":
            next(row for row in rows if row.get("event") == "CANCEL_ARMED").pop("broker_order_ticket", None)
        elif case == "deal":
            next(row for row in rows if row.get("event") == "DEAL")["broker_ticket"] = 999999
        elif case == "duplicate_terminal":
            rows["records"][1]["ticket"] = rows["records"][0]["ticket"]
        elif case == "external_identity":
            rows["account_login"] = 999999
        else:
            rows["records"][0]["symbol"] = "OTHER"
        path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n" if path.suffix == ".jsonl" else json.dumps(rows, sort_keys=True), encoding="utf-8")
    elif case in {"lookback", "terminal_history_days"}:
        path = snapshot / "orders/broker-history.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["terminal_history_days"] = 30
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    elif case == "magic":
        path = snapshot / "orders/broker-history.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload["records"]:
            record["magic"] = 999
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    elif case in {"missing_required_field", "entry_constant", "nonnegative_scale", "negative_scale", "out_constant", "reward_map", "sl_constant", "tp_constant", "signed_config_path", "signed_config_sha"}:
        path = snapshot / "campaign_lock.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if case == "missing_required_field":
            payload.pop("magic_number")
        else:
            payload["engine_code_hash"] = "f" * 64
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    elif case == "missing_required_file":
        (snapshot / "health.json").unlink()
    elif case in {"outbox_ack", "outbox_content", "order_store"}:
        connection = sqlite3.connect(snapshot / "orders/idempotency.sqlite3")
        connection.execute("UPDATE order_event_outbox SET delivered_at='corrupt' WHERE sequence=1")
        connection.commit()
        connection.close()
    elif case == "outbox_sequence":
        connection = sqlite3.connect(snapshot / "orders/idempotency.sqlite3")
        connection.execute("UPDATE order_event_outbox SET sequence=0 WHERE sequence=1")
        connection.commit()
        connection.close()
    elif case == "position":
        path = snapshot / "orders/positions.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["server"] = "other-server"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    elif case == "minute_bars_and_conflicts":
        connection = sqlite3.connect(snapshot / "cache/capital_bars.sqlite3")
        connection.execute("UPDATE minute_bars SET close=999 WHERE rowid=1")
        connection.commit()
        connection.close()
    elif case == "submitted" or case == "unknown":
        connection = sqlite3.connect(snapshot / "orders/idempotency.sqlite3")
        connection.execute("UPDATE order_intents SET status='CORRUPT' WHERE rowid=1")
        connection.commit()
        connection.close()
    else:
        raise AssertionError(f"unhandled M03 snapshot case: {case}")
    result = validate_real_snapshot_state(source, snapshot)
    assert result["status"] == "FAIL", (case, result)
    assert result["errors"], (case, result)
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize("case", ["minute_bars_and_conflicts", "order_store"], ids=lambda value: value)
def test_m03_uncheckpointed_wal_continuity(case: str, tmp_path: Path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    source = tmp_path / f"m03-wal-source-{case}"
    snapshot = tmp_path / f"m03-wal-snapshot-{case}"
    _create_real_state(source)
    database = source / ("cache/capital_bars.sqlite3" if case == "minute_bars_and_conflicts" else "orders/idempotency.sqlite3")
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        if case == "minute_bars_and_conflicts":
            connection.execute("UPDATE minute_bars SET close=456 WHERE rowid=1")
        else:
            connection.execute("UPDATE order_event_outbox SET delivered_at='wal-change' WHERE sequence=1")
        connection.commit()
        _make_snapshot(source, snapshot)
        assert validate_real_snapshot_state(source, snapshot)["status"] == "PASS"
        bad = tmp_path / f"m03-wal-bad-{case}"
        for path in source.rglob("*"):
            if path.is_file() and not path.name.startswith(database.name):
                target = bad / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        target_db = bad / database.relative_to(source)
        target_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(database, target_db)
    finally:
        connection.close()
    result = validate_real_snapshot_state(source, bad)
    assert result["status"] == "FAIL"
    assert f"{case}.wal_continuity" in result["errors"]
    record_if_enabled(request, evidence_token)
