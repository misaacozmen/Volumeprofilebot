from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from scripts.git_provenance import current_commit_tree, validate_commit_tree
from scripts.owner_replay_ledger import consume, initialize, reserve, validate
from scripts import run_super1_owner_acceptance as owner_acceptance


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "reacquire_invalid_sessions_v5.py"


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    fixture_root = tmp_path / "fixture-root"
    fixture_root.mkdir()
    body = b"real-cli-fixture-body"
    (fixture_root / "one.bin").write_bytes(body)
    descriptor = {"schema_version": 1, "artifacts": [{"artifact_id": "one", "fixture_path": "one.bin", "url": "fixture://one", "sha256": sha256(body).hexdigest(), "bytes": len(body)}]}
    (fixture_root / "artifacts.json").write_text(json.dumps(descriptor), encoding="utf-8")
    ledger = tmp_path / "acquisition.sqlite3"
    return fixture_root, ledger, "a" * 64


def _run_fixture(fixture_root: Path, ledger: Path, cas: Path, run_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), "--mode", "fixture", "--fixture-root", str(fixture_root), "--ledger-path", str(ledger), "--cas-root", str(cas), "--run-id", run_id],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_real_cli_fixture_mode_has_zero_provider_side_effect_and_one_run_id(tmp_path: Path) -> None:
    fixture_root, ledger, run_id = _fixture(tmp_path)
    completed = _run_fixture(fixture_root, ledger, tmp_path / "cas", run_id)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {"artifact_count": 1, "mode": "fixture", "provider_call_count": 0, "run_id": run_id, "status": "COMPLETE"}
    connection = sqlite3.connect(ledger)
    try:
        assert connection.execute("SELECT COUNT(*), COUNT(DISTINCT run_id) FROM acquisition_runs").fetchone() == (1, 1)
        assert connection.execute("SELECT run_id, state, request_url, request_url_sha256 FROM acquisition_artifacts").fetchone()[:3] == (run_id, "COMMITTED", "fixture://one")
    finally:
        connection.close()


def test_real_cli_fixture_resume_rejects_corrupt_cas_and_provider_mode_rejects_fixture_root(tmp_path: Path) -> None:
    fixture_root, ledger, run_id = _fixture(tmp_path)
    cas = tmp_path / "cas"
    first = _run_fixture(fixture_root, ledger, cas, run_id)
    assert first.returncode == 0, first.stderr
    cas_file = next(cas.rglob("*.bi5"))
    cas_file.write_bytes(b"forged")
    resumed = _run_fixture(fixture_root, ledger, cas, run_id)
    assert resumed.returncode != 0
    assert "CAS evidence" in resumed.stderr or "body" in resumed.stderr
    rejected = subprocess.run(
        [sys.executable, str(CLI), "--mode", "provider", "--fixture-root", str(fixture_root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "rejects" in rejected.stderr


def test_real_cli_fixture_descriptor_is_closed_and_missing_descriptor_fails_closed(tmp_path: Path) -> None:
    fixture_root, ledger, run_id = _fixture(tmp_path)
    descriptor = json.loads((fixture_root / "artifacts.json").read_text(encoding="utf-8"))
    descriptor["artifacts"][0]["unexpected"] = True
    (fixture_root / "artifacts.json").write_text(json.dumps(descriptor), encoding="utf-8")
    result = _run_fixture(fixture_root, ledger, tmp_path / "cas", run_id)
    assert result.returncode != 0
    assert "schema" in result.stderr


def test_git_source_tree_binding_and_sqlite_replay_are_fail_closed(tmp_path: Path) -> None:
    source = current_commit_tree(ROOT)
    assert validate_commit_tree(ROOT, source["source_commit"], source["source_tree_sha256"]) == source
    with pytest.raises(ValueError, match="commit-to-tree"):
        validate_commit_tree(ROOT, source["source_commit"], "0" * 40)
    ledger = tmp_path / "owner-replay.sqlite3"
    run_id = "b" * 64
    nonce_a, nonce_b = "c" * 64, "d" * 64
    initialize(ledger)
    reserve(ledger, run_id=run_id, nonce=nonce_a, purpose="audit")
    reserve(ledger, run_id=run_id, nonce=nonce_b, purpose="audit")
    assert validate(ledger, run_id=run_id, nonces=(nonce_a, nonce_b))["states"] == ["RESERVED", "RESERVED"]
    # The unique nonce/run contract rejects a second reservation and preserves the original row.
    with pytest.raises(Exception):
        reserve(ledger, run_id=run_id, nonce=nonce_a, purpose="audit")
    consume(ledger, run_id=run_id, nonce=nonce_a)


def test_owner_rotation_binds_run_nonce_and_opaque_old_new_bindings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id = "a" * 64
    nonce = "b" * 64
    old_binding = "c" * 64
    new_binding = "d" * 64
    identity_hmac = "e" * 64
    payload = {
        "status": "SIGNED",
        "old_binding_revoked": True,
        "new_binding_active": True,
        "account_trade_mode": "DEMO",
        "provider_issuer": "owner-provider",
        "semantic_state": "ROTATED_DEMO",
        "effective_at_utc": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    }

    def fake_verify(*args, **kwargs):
        expected = kwargs["expected"]
        fixed = {
            "run_id": run_id,
            "nonce": nonce,
            "source_commit": "1" * 40,
            "source_tree_oid": "2" * 40,
            "old_binding_id_sha256": old_binding,
            "new_binding_id_sha256": new_binding,
            "account_identity_hmac_sha256": identity_hmac,
        }
        if any(expected.get(key) != value for key, value in fixed.items()):
            raise ValueError("rotation evidence binding differs")
        return {
            **payload,
            **fixed,
        }

    monkeypatch.setattr(owner_acceptance, "_verify_signed_document", fake_verify)
    owner_acceptance._verify_rotation(
        tmp_path / "rotation.json",
        signer_key_path=tmp_path / "owner.pem",
        signer_key_sha256="f" * 64,
        run_id=run_id,
        source_commit="1" * 40,
        source_tree_oid="2" * 40,
        nonce=nonce,
        old_binding_sha256=old_binding,
        new_binding_sha256=new_binding,
        identity_hmac_sha256=identity_hmac,
    )
    with pytest.raises(ValueError, match="binding differs"):
        owner_acceptance._verify_rotation(
            tmp_path / "rotation.json",
            signer_key_path=tmp_path / "owner.pem",
            signer_key_sha256="f" * 64,
            run_id="0" * 64,
            source_commit="1" * 40,
            source_tree_oid="2" * 40,
            nonce=nonce,
            old_binding_sha256=old_binding,
            new_binding_sha256=new_binding,
            identity_hmac_sha256=identity_hmac,
        )
    with pytest.raises(ValueError, match="binding differs"):
        owner_acceptance._verify_rotation(
            tmp_path / "rotation.json",
            signer_key_path=tmp_path / "owner.pem",
            signer_key_sha256="f" * 64,
            run_id=run_id,
            source_commit="1" * 40,
            source_tree_oid="2" * 40,
            nonce=nonce,
            old_binding_sha256=old_binding,
            new_binding_sha256="0" * 64,
            identity_hmac_sha256=identity_hmac,
        )
