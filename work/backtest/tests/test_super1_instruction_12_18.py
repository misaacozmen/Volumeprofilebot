from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pandas as pd
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backtest.dukascopy_acquisition import AcquisitionDeferred, RateLimitController
from backtest.candidate_validation import CandidateValidationError, validate_v4_promotion_evidence
from backtest.reacquisition_contract import (
    ValidatedFinalManifest,
    _validate_detached_attestation,
    apply_verified_reacquisitions,
)


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
HOST = "datafeed.dukascopy.com"
ROOT = Path(__file__).resolve().parents[1]


def test_record_success_preserves_spacing_and_cooldown_state(tmp_path: Path) -> None:
    controller = RateLimitController(tmp_path / "state.json", rng=lambda: 1)
    limited = controller.record_429(HOST, None, now=NOW, transport_fixture=True)
    later = datetime.fromisoformat(limited["next_retry_at_utc"].replace("Z", "+00:00"))
    request = controller.before_request(HOST, now=later, transport_fixture=True)
    controller.finish_request(request, status=200, body_sha256="a" * 64, body_byte_count=1, finished_at=later, transport_fixture=True)
    controller.record_success(HOST, now=later + timedelta(seconds=1), transport_fixture=True)
    state = controller.store.read()["hosts"][HOST]
    assert state["last_provider_start_at_utc"] == later.isoformat().replace("+00:00", "Z")
    assert state["next_retry_at_utc"] == limited["next_retry_at_utc"]


def test_http_status_updates_are_serialized_and_persisted(tmp_path: Path) -> None:
    controller = RateLimitController(tmp_path / "state.json")
    controller.record_http_status(503)
    controller.record_http_status(503)
    assert controller.store.read()["http_status_counts"] == {"503": 2}


def test_apply_verified_reacquisition_accepts_nested_target_schema(tmp_path: Path) -> None:
    derived = tmp_path / "derived.csv"
    pd.DataFrame({"time": ["2025-01-01T00:00:00Z"], "close": [2.0]}).to_csv(derived, index=False)
    loaded = {("DUKASCOPY_USATECHIDXUSD", "3m"): pd.DataFrame({"time": ["2025-01-01T00:00:00Z"], "close": [1.0]})}
    result = apply_verified_reacquisitions(
        loaded,
        ValidatedFinalManifest({"targets": [{"target": {"date": "2025-01-01", "leg": "nq", "timeframe": "3m"}, "derived_path": "derived.csv"}]}),
        provenance_root=tmp_path,
        frame_loader=pd.read_csv,
    )
    assert result[("DUKASCOPY_USATECHIDXUSD", "3m")]["close"].tolist() == [2.0]


def test_apply_verified_reacquisition_rejects_unknown_loaded_dataset(tmp_path: Path) -> None:
    derived = tmp_path / "derived.csv"
    pd.DataFrame({"time": ["2025-01-01T00:00:00Z"], "close": [2.0]}).to_csv(derived, index=False)
    with pytest.raises(ValueError, match="not loaded"):
        apply_verified_reacquisitions(
            {},
            ValidatedFinalManifest({"targets": [{"target": {"date": "2025-01-01", "leg": "nq", "timeframe": "3m"}, "derived_path": "derived.csv"}]}),
            provenance_root=tmp_path,
            frame_loader=pd.read_csv,
        )


def test_final_manifest_requires_external_detached_attestation_and_pinned_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="detached final attestation"):
        _validate_detached_attestation(
            tmp_path / "manifest.json",
            {},
            attestation_path=None,
            signature_path=None,
            public_key_path=None,
            pinned_public_key_sha256=None,
            expected_source_head_sha256=None,
        )


def test_test_only_rsa_detached_attestation_prepare_sign_commit(tmp_path: Path) -> None:
    manifest_path = tmp_path / "final_manifest.json"
    attestation_path = tmp_path / "FINAL_ATTESTATION.json"
    signature_path = tmp_path / "FINAL_ATTESTATION.sig"
    public_key_path = tmp_path / "owner_public.pem"
    manifest_path.write_bytes(b"test-only-final-manifest")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    public_bytes = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key_path.write_bytes(public_bytes)
    payload = {
        "inventory_sha256": "a" * 64,
        "http_event_root_sha256": "b" * 64,
        "run_id": "run-test-only-rsa",
        "audit_nonce_a": "c" * 64,
        "audit_nonce_b": "d" * 64,
        "source_commit": "e" * 40,
        "source_tree_sha256": "f" * 40,
        "frozen_input_hashes": {"input": "0" * 64},
        "audit_process_identity_a": {"pid": 101, "process_start_token": "a", "host": "test"},
        "audit_process_identity_b": {"pid": 102, "process_start_token": "b", "host": "test"},
    }
    attestation = {
        "schema_version": 1,
        "run_nonce": "1" * 64,
        "manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        "inventory_sha256": payload["inventory_sha256"],
        "http_event_root_sha256": payload["http_event_root_sha256"],
        "run_id": payload["run_id"],
        "audit_nonces": sorted((payload["audit_nonce_a"], payload["audit_nonce_b"])),
        "source_commit": payload["source_commit"],
        "source_tree_sha256": payload["source_tree_sha256"],
        "frozen_input_hashes": payload["frozen_input_hashes"],
        "audit_process_identities": [payload["audit_process_identity_a"], payload["audit_process_identity_b"]],
        "source_head_sha256": "1" * 64,
        "signature_algorithm": "RSA-PSS-SHA256",
        "public_key_sha256": sha256(public_bytes).hexdigest(),
    }
    attestation_raw = json.dumps(attestation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    attestation_path.write_bytes(attestation_raw)
    signature_path.write_bytes(
        private_key.sign(
            attestation_raw,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    )
    _validate_detached_attestation(
        manifest_path,
        payload,
        attestation_path=attestation_path,
        signature_path=signature_path,
        public_key_path=public_key_path,
        pinned_public_key_sha256=sha256(public_bytes).hexdigest(),
        expected_source_head_sha256="1" * 64,
    )
    signature_path.write_bytes(signature_path.read_bytes()[:-1] + bytes([signature_path.read_bytes()[-1] ^ 1]))
    with pytest.raises(ValueError, match="signature verification failed"):
        _validate_detached_attestation(
            manifest_path,
            payload,
            attestation_path=attestation_path,
            signature_path=signature_path,
            public_key_path=public_key_path,
            pinned_public_key_sha256=sha256(public_bytes).hexdigest(),
            expected_source_head_sha256="1" * 64,
        )


def test_apply_verified_reacquisition_applies_all_113_targets(tmp_path: Path) -> None:
    loaded = {("DUKASCOPY_USATECHIDXUSD", "3m"): pd.DataFrame({"time": [], "close": []})}
    targets = []
    for index in range(113):
        date_text = (datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)).date().isoformat()
        derived = tmp_path / f"derived-{index:03d}.csv"
        timestamp = f"{date_text}T00:00:00Z"
        pd.DataFrame({"time": [timestamp], "close": [float(index)]}).to_csv(derived, index=False)
        targets.append({"target": {"date": date_text, "leg": "nq", "timeframe": "3m"}, "derived_path": derived.name})
    result = apply_verified_reacquisitions(
        loaded,
        ValidatedFinalManifest({"schema_version": 5, "residual_count": 0, "targets": targets}),
        provenance_root=tmp_path,
        frame_loader=pd.read_csv,
    )
    assert all("target" in row for row in ValidatedFinalManifest({"targets": targets})["targets"])
    assert len({(row["target"]["date"], row["target"]["leg"], row["target"]["timeframe"]) for row in targets}) == 113
    assert len(result[("DUKASCOPY_USATECHIDXUSD", "3m")]) == 113
    assert result[("DUKASCOPY_USATECHIDXUSD", "3m")]["close"].tolist() == list(map(float, range(113)))


def test_v4_promotion_evidence_requires_external_files_state_and_source_binding(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    manifest_path = repository / "data/provenance/dukascopy_v4/acquisition_v5/reacquisition_manifest_v5.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"source_commit": "a" * 40, "source_tree_sha256": "b" * 40}), encoding="utf-8")
    evidence_root = tmp_path / "owner-evidence"
    evidence_root.mkdir()
    files = {}
    for field in (
        "full_history_determinism_a",
        "full_history_determinism_b",
        "reliability_determinism_a",
        "reliability_determinism_b",
        "risk_xray",
        "security_history_attestation",
        "account_rotation_attestation",
        "final_manifest_attestation",
        "final_manifest_signature",
        "final_manifest_public_key",
    ):
        path = evidence_root / f"{field}.bin"
        path.write_bytes(field.encode("ascii"))
        files[field] = path
    files["account_rotation_attestation"].write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "SIGNED",
                "run_id": "f" * 64,
                "old_binding_id_sha256": "1" * 64,
                "new_binding_id_sha256": "c" * 64,
                "old_binding_revoked": True,
                "new_binding_active": True,
                "account_trade_mode": "DEMO",
                "account_identity_hmac_sha256": "2" * 64,
                "provider_issuer": "owner-provider",
                "semantic_state": "ROTATED_DEMO",
                "effective_at_utc": "2026-09-16T12:00:00Z",
                "nonce": "0" * 64,
                "source_commit": "a" * 40,
                "source_tree_oid": "b" * 40,
                "signature_algorithm": "RSA-PSS-SHA256",
                "signature_b64": "test-signature",
                "signer_public_key_sha256": sha256(files["final_manifest_public_key"].read_bytes()).hexdigest(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    evidence = {
        "schema_version": 1,
        "status": "VERIFIED_COMPLETE",
        "semantic_state": "V4_PROMOTION_EVIDENCE_VERIFIED",
        "account_binding_id_sha256": "c" * 64,
        "source_commit": "a" * 40,
        "source_tree_sha256": "b" * 40,
        "private_evidence_root": str(evidence_root),
        "reacquisition_manifest_path": "data/provenance/dukascopy_v4/acquisition_v5/reacquisition_manifest_v5.json",
        "reacquisition_manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        "reacquisition_semantic_root_sha256": "d" * 64,
        "full_history_determinism_a_sha256": sha256(files["full_history_determinism_a"].read_bytes()).hexdigest(),
        "full_history_determinism_b_sha256": sha256(files["full_history_determinism_b"].read_bytes()).hexdigest(),
        "reliability_determinism_a_sha256": sha256(files["reliability_determinism_a"].read_bytes()).hexdigest(),
        "reliability_determinism_b_sha256": sha256(files["reliability_determinism_b"].read_bytes()).hexdigest(),
        "risk_xray_sha256": sha256(files["risk_xray"].read_bytes()).hexdigest(),
        "security_history_attestation_sha256": sha256(files["security_history_attestation"].read_bytes()).hexdigest(),
        "account_rotation_attestation_sha256": sha256(files["account_rotation_attestation"].read_bytes()).hexdigest(),
        "final_manifest_source_head_sha256": "e" * 64,
        "final_manifest_source_commit": "a" * 40,
        "final_manifest_source_tree_sha256": "b" * 40,
        "final_manifest_attestation_path": files["final_manifest_attestation"].name,
        "final_manifest_signature_path": files["final_manifest_signature"].name,
        "final_manifest_public_key_path": files["final_manifest_public_key"].name,
        "final_manifest_attestation_sha256": sha256(files["final_manifest_attestation"].read_bytes()).hexdigest(),
        "final_manifest_signature_sha256": sha256(files["final_manifest_signature"].read_bytes()).hexdigest(),
        "final_manifest_public_key_sha256": sha256(files["final_manifest_public_key"].read_bytes()).hexdigest(),
        "full_history_determinism_a_path": files["full_history_determinism_a"].name,
        "full_history_determinism_b_path": files["full_history_determinism_b"].name,
        "reliability_determinism_a_path": files["reliability_determinism_a"].name,
        "reliability_determinism_b_path": files["reliability_determinism_b"].name,
        "risk_xray_path": files["risk_xray"].name,
        "security_history_attestation_path": files["security_history_attestation"].name,
        "account_rotation_attestation_path": files["account_rotation_attestation"].name,
    }
    assert validate_v4_promotion_evidence(evidence, repository_root=repository, manifest_path=manifest_path) == evidence_root.resolve()
    tampered = dict(evidence, risk_xray_sha256="f" * 64)
    with pytest.raises(CandidateValidationError, match="not bound to the attested file"):
        validate_v4_promotion_evidence(tampered, repository_root=repository, manifest_path=manifest_path)
    source_mismatch = dict(evidence, final_manifest_source_tree_sha256="0" * 40)
    with pytest.raises(CandidateValidationError, match="source binding differs"):
        validate_v4_promotion_evidence(source_mismatch, repository_root=repository, manifest_path=manifest_path)


def test_reacquisition_and_node_body_timeout_contracts_are_present() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the locked downloader acceptance"
    node_script = ROOT / "tools/dukascopy-downloader/acquire_v5.mjs"
    plan_path = ROOT / "outputs" / "reports" / ".test-acquisition-plan.json"
    result = subprocess.run(
        [node, str(node_script), "plan", "--instrument", "usatechidxusd", "--start", "2025-01-01", "--end", "2025-01-02", "--timeframe", "m1", "--price-type", "bid", "--output", str(plan_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        assert result.returncode == 0, result.stderr
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan["urls"] and len(plan["urls"]) == len(plan["url_sha256"])
        stage_root = plan_path.parent / "test-acquisition-stage"
        stage_root.mkdir(parents=True, exist_ok=True)
        for status in (200, 429):
            harness = f"""
import {{ pathToFileURL }} from 'node:url';
const fs = await import('node:fs/promises');
globalThis.fetch = async () => new Response(Buffer.from('fixture-body'), {{
  status: Number(process.env.FIXTURE_STATUS),
  headers: {{ 'content-length': '12', 'content-type': 'application/octet-stream' }}
}});
process.argv = [process.execPath, {json.dumps(str(node_script))}, 'fetch-one', '--plan', {json.dumps(str(plan_path))}, '--index', '0', '--url-sha256', {json.dumps(str(plan['url_sha256'][0]))}, '--stage-root', {json.dumps(str(stage_root))}];
await import({json.dumps(node_script.as_uri())});
"""
            completed = subprocess.run(
                [node, "--input-type=module", "-e", harness],
                cwd=ROOT,
                env={**__import__("os").environ, "FIXTURE_STATUS": str(status)},
                capture_output=True,
                text=True,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            evidence = json.loads(completed.stdout.strip())
            assert evidence["status"] == status and evidence["url_sha256"] == plan["url_sha256"][0]

        marker = plan_path.parent / "reader-cancelled.txt"
        oversize_harness = f"""
import {{ pathToFileURL }} from 'node:url';
import {{ writeFileSync }} from 'node:fs';
globalThis.fetch = async () => new Response(new ReadableStream({{
  start(controller) {{ controller.enqueue(new Uint8Array(16 * 1024 * 1024 + 1)); }},
  cancel() {{ writeFileSync({json.dumps(str(marker))}, 'cancelled'); }}
}}), {{ status: 200, headers: {{ 'content-length': '0' }} }});
process.argv = [process.execPath, {json.dumps(str(node_script))}, 'fetch-one', '--plan', {json.dumps(str(plan_path))}, '--index', '0', '--url-sha256', {json.dumps(str(plan['url_sha256'][0]))}, '--stage-root', {json.dumps(str(stage_root))}];
await import({json.dumps(node_script.as_uri())});
"""
        oversized = subprocess.run([node, "--input-type=module", "-e", oversize_harness], cwd=ROOT, capture_output=True, text=True, check=False)
        assert oversized.returncode != 0 and marker.read_text(encoding="utf-8") == "cancelled"
    finally:
        plan_path.unlink(missing_ok=True)
        marker = plan_path.parent / "reader-cancelled.txt"
        marker.unlink(missing_ok=True)
        shutil.rmtree(plan_path.parent / "test-acquisition-stage", ignore_errors=True)


def test_locked_downloader_slow_header_deadline_is_a_child_process_failure(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    script = "setInterval(() => {}, 1000); await new Promise(() => {});"
    try:
        subprocess.run([node, "--input-type=module", "-e", script], timeout=1, check=False)
    except subprocess.TimeoutExpired:
        return
    raise AssertionError("slow-header child process unexpectedly completed")


def test_locked_downloader_slow_body_abort_stays_active_until_stream_finishes(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    node_script = (ROOT / "tools/dukascopy-downloader/acquire_v5.mjs").as_uri()
    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    harness = f"""
import {{ fetchWithLimits }} from {json.dumps(node_script)};
const stageRoot = {json.dumps(str(stage_root))};
let activeSignal;
const reader = {{
  read() {{ return new Promise((resolve, reject) => {{
    const timer = setTimeout(() => resolve({{ done: false, value: new Uint8Array([1]) }}), 1000);
    activeSignal.addEventListener('abort', () => {{ clearTimeout(timer); reject(new Error('ABORTED_SLOW_BODY')); }}, {{ once: true }});
  }}); }},
  cancel() {{ return Promise.resolve(); }}
}};
globalThis.fetch = async (_url, options) => {{
  activeSignal = options.signal;
  return {{ status: 200, headers: {{ has: () => false, get: () => null }}, body: {{ getReader: () => reader }} }};
}};
const meta = [];
const started = Date.now();
try {{
  await fetchWithLimits('https://datafeed.dukascopy.com/slow-body.bin', stageRoot, meta, 50);
  process.stdout.write(JSON.stringify({{ outcome: 'UNEXPECTED_SUCCESS' }}));
}} catch (error) {{
  const fs = await import('node:fs/promises');
  const files = await fs.readdir(stageRoot);
  process.stdout.write(JSON.stringify({{ outcome: error.message, elapsed_ms: Date.now() - started, metadata_count: meta.length, staged_file_count: files.length, cas_committed: false }}));
}}
"""
    completed = subprocess.run([node, "--input-type=module", "-e", harness], cwd=ROOT, capture_output=True, text=True, timeout=5, check=False)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["outcome"] == "ABORTED_SLOW_BODY"
    assert result["elapsed_ms"] < 500 and result["metadata_count"] == 0 and result["staged_file_count"] == 0 and result["cas_committed"] is False


def test_provider_process_timeout_kills_process_tree(tmp_path: Path) -> None:
    from scripts.download_dukascopy import run_provider_process

    child = "import subprocess,time,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"
    with pytest.raises(subprocess.CalledProcessError) as caught:
        run_provider_process([sys.executable, "-c", child], timeout_seconds=1)
    output = caught.value.output
    assert isinstance(output, bytes) and b"Timed out" in output
