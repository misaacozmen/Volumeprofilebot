"""Strict V5 reacquisition manifest validation and application contract."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping

import pandas as pd


INVENTORY_SHA256 = "a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075"
TARGET_COUNT = 113
TARGET_KEYS = frozenset({"date", "leg", "timeframe"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_KEYS = frozenset({
    "schema_version", "inventory_sha256", "target_count", "residual_count", "targets",
    "semantic_root_sha256", "ordered_target_merkle_root_sha256", "calendar_sha256",
    "auditor_sha256", "node_helper_sha256", "package_lock_sha256", "http_event_root_sha256",
    "run_id", "audit_nonce_a", "audit_nonce_b", "audit_process_identity_a", "audit_process_identity_b",
    "audit_identity_a_path", "audit_identity_a_sha256", "audit_identity_b_path", "audit_identity_b_sha256",
    "source_commit", "source_tree_sha256", "frozen_input_hashes", "trust_state",
    "owner_trust_policy_path", "owner_trust_policy_sha256", "owner_replay_ledger_path", "owner_replay_ledger_sha256",
    "owner_signature_status",
})


class ValidatedFinalManifest(dict[str, Any]):
    """Runtime type for a final manifest after every trust-chain check passed."""

    @property
    def run_id(self) -> str:
        return str(self["run_id"])

    @property
    def trust_state(self) -> str:
        return str(self["trust_state"])


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _require_digest(value: object, label: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} is not a lowercase SHA-256 digest")
    return text


def _external_file(value: object, *, repository_root: Path, expected_sha256: object, label: str) -> Path:
    raw = str(value or "")
    raw_path = Path(raw)
    if not raw_path.is_absolute():
        raise ValueError(f"{label} path must be absolute and external")
    path = raw_path.resolve()
    if not path.is_file():
        raise ValueError(f"{label} is missing")
    try:
        path.relative_to(repository_root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError(f"{label} must remain outside the repository")
    if digest(path) != _require_digest(expected_sha256, f"{label} SHA-256"):
        raise ValueError(f"{label} hash mismatch")
    return path


def _validate_owner_trust(
    payload: Mapping[str, Any],
    *,
    repository_root: Path,
    require_signature: bool,
    trusted_root_public_key_path: Path | None,
    trusted_root_public_key_sha256: str | None,
    repository_identity: str | None,
    branch: str | None,
    leaf_public_key_sha256: str | None,
    owner_trust_policy_path: Path | None,
    owner_replay_ledger_path: Path | None,
) -> None:
    if owner_trust_policy_path is not None and Path(str(payload.get("owner_trust_policy_path") or "")).resolve() != owner_trust_policy_path.resolve():
        raise ValueError("owner trust policy path differs from the canonical manifest binding")
    if owner_replay_ledger_path is not None and Path(str(payload.get("owner_replay_ledger_path") or "")).resolve() != owner_replay_ledger_path.resolve():
        raise ValueError("owner replay ledger path differs from the canonical manifest binding")
    policy_path = _external_file(
        owner_trust_policy_path or payload.get("owner_trust_policy_path"),
        repository_root=repository_root,
        expected_sha256=payload.get("owner_trust_policy_sha256"),
        label="owner trust policy",
    )
    replay_path = _external_file(
        payload.get("owner_replay_ledger_path"),
        repository_root=repository_root,
        expected_sha256=payload.get("owner_replay_ledger_sha256"),
        label="owner replay ledger",
    )
    if trusted_root_public_key_path is None or trusted_root_public_key_sha256 is None or not repository_identity or not branch:
        raise ValueError("independent owner trust root, repository identity, and branch are required")
    trusted_root_public_key_path = _external_file(
        trusted_root_public_key_path,
        repository_root=repository_root,
        expected_sha256=trusted_root_public_key_sha256,
        label="trusted owner root public key",
    )
    try:
        from scripts.owner_replay_ledger import validate as validate_replay_ledger
        from scripts.owner_trust import validate_policy
        validate_policy(
            policy_path,
            trusted_root_public_key_path=trusted_root_public_key_path,
            trusted_root_public_key_sha256=trusted_root_public_key_sha256,
            repository_identity=repository_identity,
            branch=branch,
            inventory_sha256=str(payload.get("inventory_sha256") or ""),
            source_commit=str(payload.get("source_commit") or ""),
            source_tree_oid=str(payload.get("source_tree_sha256") or ""),
            leaf_public_key_sha256=str(leaf_public_key_sha256 or ""),
        )
        validate_replay_ledger(
            replay_path,
            run_id=str(payload.get("run_id") or ""),
            nonces=(str(payload.get("audit_nonce_a") or ""), str(payload.get("audit_nonce_b") or "")),
        )
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("owner trust inputs are invalid") from exc
    if require_signature and payload.get("owner_signature_status") != "SIGNED":
        raise ValueError("owner signature is not complete")


def target_key(value: Mapping[str, Any]) -> tuple[str, str, str]:
    if isinstance(value.get("target"), Mapping):
        value = value["target"]
    return (str(value.get("date")), str(value.get("leg")), str(value.get("timeframe")))


def load_inventory(path: Path) -> list[dict[str, Any]]:
    if digest(path) != INVENTORY_SHA256:
        raise ValueError("frozen inventory SHA mismatch")
    rows = pd.read_csv(path).to_dict(orient="records")
    keys = [target_key(row) for row in rows]
    if len(rows) != TARGET_COUNT or len(set(keys)) != TARGET_COUNT:
        raise ValueError("frozen inventory must contain exactly 113 unique targets")
    if ("2025-04-16", "nq", "3m") not in set(keys):
        raise ValueError("required 2025-04-16/nq/3m target is absent")
    return rows


def _hash_pair(left: str, right: str) -> str:
    return sha256((left + right).encode("ascii")).hexdigest()


def ordered_merkle(rows: Iterable[Mapping[str, Any]]) -> str:
    leaves = [sha256(json.dumps(dict(row), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest() for row in rows]
    if not leaves:
        return sha256(b"EMPTY").hexdigest()
    while len(leaves) > 1:
        if len(leaves) % 2:
            leaves.append(leaves[-1])
        leaves = [_hash_pair(leaves[index], leaves[index + 1]) for index in range(0, len(leaves), 2)]
    return leaves[0]


def semantic_root(rows: Iterable[Mapping[str, Any]]) -> str:
    ordered = sorted((dict(row) for row in rows), key=lambda row: target_key(row))
    return sha256(json.dumps(ordered, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def safe_provenance_path(root: Path, value: object, label: str) -> Path:
    relative = str(value or "")
    if not relative or Path(relative).is_absolute():
        raise ValueError(f"{label} must be a relative provenance path")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes provenance root") from exc
    return resolved


def _manifest_artifact_path(root: Path, value: object, label: str) -> Path:
    """Resolve a path stored by the bundle manifest without widening its trust root."""
    relative = str(value or "")
    prefix = "data/provenance/dukascopy_v4/"
    if relative.startswith(prefix):
        relative = relative[len(prefix):]
    return safe_provenance_path(root, relative, label)


def validate_committed_bundle(bundle_dir: Path, *, target: Mapping[str, Any], repository_root: Path) -> dict[str, Any]:
    """Validate one V5 bundle before it can be resumed or published."""
    bundle = bundle_dir.resolve()
    if not bundle.is_dir():
        raise ValueError("V5 bundle directory is missing")
    files = {item.name for item in bundle.iterdir()}
    if len(files) != 6 or "COMMITTED.json" not in files or "ATTESTATION.json" not in files:
        raise ValueError("V5 bundle file set is not closed")
    committed_path = bundle / "COMMITTED.json"
    attestation_path = bundle / "ATTESTATION.json"
    try:
        committed = json.loads(committed_path.read_text(encoding="utf-8"))
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("V5 bundle control evidence is unreadable") from exc
    expected_target = {"date": str(target.get("date")), "leg": str(target.get("leg")), "timeframe": str(target.get("timeframe"))}
    names = {str(committed.get(key) or "") for key in ("manifest_name", "minute_name", "derived_name", "decoded_name", "attestation_name")}
    if committed.get("schema_version") != 1 or committed.get("target") != expected_target or committed.get("attestation_name") != "ATTESTATION.json" or names != files - {"COMMITTED.json"}:
        raise ValueError("V5 COMMITTED evidence does not bind the closed target file set")
    for key, name_key in (("manifest_sha256", "manifest_name"), ("minute_sha256", "minute_name"), ("derived_sha256", "derived_name"), ("decoded_sha256", "decoded_name"), ("attestation_sha256", "attestation_name")):
        file_path = bundle / str(committed[name_key])
        if _require_digest(committed.get(key), f"V5 committed {key}") != digest(file_path):
            raise ValueError(f"V5 committed {key} does not match bytes")
    expected_attestation = {
        "schema_version": 1,
        "attestation_type": "V5_BUNDLE_BYTES",
        "target": expected_target,
        "manifest_sha256": committed["manifest_sha256"],
        "minute_sha256": committed["minute_sha256"],
        "derived_sha256": committed["derived_sha256"],
        "decoded_sha256": committed["decoded_sha256"],
    }
    if attestation != expected_attestation:
        raise ValueError("V5 byte attestation does not bind exact bundle bytes")
    manifest_path = bundle / str(committed["manifest_name"])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("V5 bundle manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 5:
        raise ValueError("V5 bundle manifest schema is invalid")
    ordered_urls = manifest.get("ordered_urls")
    plan_hashes = manifest.get("plan_url_sha256")
    raw_chunks = manifest.get("raw_chunks")
    if not isinstance(ordered_urls, list) or not isinstance(plan_hashes, list) or plan_hashes != [sha256(str(url).encode()).hexdigest() for url in ordered_urls] or not isinstance(raw_chunks, list) or len(raw_chunks) != len(ordered_urls):
        raise ValueError("V5 bundle URL plan is invalid")
    repo = repository_root.resolve()
    cas_root = (repo / "data/provenance/dukascopy_v4/acquisition_v5/http_cas").resolve()
    for index, chunk in enumerate(raw_chunks):
        if not isinstance(chunk, dict) or chunk.get("url") != ordered_urls[index] or chunk.get("url_sha256") != plan_hashes[index]:
            raise ValueError("V5 raw chunk URL binding is invalid")
        raw_path_text = str(chunk.get("raw_path") or "")
        prefix = "data/provenance/dukascopy_v4/"
        if raw_path_text.startswith(prefix):
            raw_path_text = raw_path_text[len(prefix):]
        raw_path = (repo / "data/provenance/dukascopy_v4" / raw_path_text).resolve() if not Path(raw_path_text).is_absolute() else Path(raw_path_text).resolve()
        try:
            raw_path.relative_to(cas_root)
        except ValueError as exc:
            raise ValueError("V5 raw evidence is outside the HTTP CAS") from exc
        raw_hash = _require_digest(chunk.get("raw_sha256"), "V5 raw_sha256")
        if not raw_path.is_file() or digest(raw_path) != raw_hash or raw_path.stat().st_size != int(chunk.get("raw_byte_count", -1)):
            raise ValueError("V5 raw CAS bytes do not match evidence")
    return {"bundle_path": str(bundle), "target": expected_target, "manifest_sha256": committed["manifest_sha256"], "attestation_sha256": committed["attestation_sha256"]}


def _validate_detached_attestation(
    manifest_path: Path,
    payload: Mapping[str, Any],
    *,
    attestation_path: Path | None,
    signature_path: Path | None,
    public_key_path: Path | None,
    pinned_public_key_sha256: str | None,
    expected_source_head_sha256: str | None,
) -> None:
    if not all((attestation_path, signature_path, public_key_path, pinned_public_key_sha256)):
        raise ValueError("detached final attestation, signature, and pinned public key are required")
    assert attestation_path is not None and signature_path is not None and public_key_path is not None
    if not attestation_path.is_file() or not signature_path.is_file() or not public_key_path.is_file():
        raise ValueError("detached final attestation, signature, or pinned public key is missing")
    attestation_raw = attestation_path.read_bytes()
    try:
        attestation = json.loads(attestation_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("detached final attestation is not valid JSON") from exc
    if not isinstance(attestation, dict) or attestation.get("schema_version") != 1:
        raise ValueError("detached final attestation schema is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(attestation.get("run_nonce") or "")):
        raise ValueError("detached final attestation run nonce is invalid")
    if attestation.get("manifest_sha256") != sha256(manifest_path.read_bytes()).hexdigest():
        raise ValueError("detached final attestation is not bound to final manifest bytes")
    if attestation.get("inventory_sha256") != payload.get("inventory_sha256") or attestation.get("http_event_root_sha256") != payload.get("http_event_root_sha256"):
        raise ValueError("detached final attestation inventory or HTTP event root differs")
    if attestation.get("run_id") != payload.get("run_id") or attestation.get("audit_nonces") != sorted((payload.get("audit_nonce_a"), payload.get("audit_nonce_b"))) or attestation.get("source_commit") != payload.get("source_commit") or attestation.get("source_tree_sha256") != payload.get("source_tree_sha256") or attestation.get("frozen_input_hashes") != payload.get("frozen_input_hashes") or attestation.get("audit_process_identities") != [payload.get("audit_process_identity_a"), payload.get("audit_process_identity_b")]:
        raise ValueError("detached final attestation trust identity differs")
    source_head = _require_digest(attestation.get("source_head_sha256"), "detached final attestation source_head_sha256")
    if expected_source_head_sha256 is not None and source_head != _require_digest(expected_source_head_sha256, "expected source head SHA-256"):
        raise ValueError("detached final attestation source head differs from tested head")
    if attestation.get("signature_algorithm") != "RSA-PSS-SHA256":
        raise ValueError("detached final attestation signature algorithm is invalid")
    observed_key_hash = sha256(public_key_path.read_bytes()).hexdigest()
    pinned_key_hash = _require_digest(pinned_public_key_sha256, "pinned final public key SHA-256")
    if observed_key_hash != pinned_key_hash or attestation.get("public_key_sha256") != pinned_key_hash:
        raise ValueError("detached final attestation public key is not pinned")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
        public_key.verify(
            signature_path.read_bytes(),
            attestation_raw,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise ValueError("detached final attestation signature verification failed") from exc


def validate_final_manifest(
    path: Path,
    *,
    provenance_root: Path,
    inventory_path: Path,
    detached_attestation_path: Path | None = None,
    detached_signature_path: Path | None = None,
    pinned_public_key_path: Path | None = None,
    pinned_public_key_sha256: str | None = None,
    expected_source_head_sha256: str | None = None,
    require_owner_signature: bool = True,
    trusted_root_public_key_path: Path | None = None,
    trusted_root_public_key_sha256: str | None = None,
    repository_identity: str | None = None,
    branch: str | None = None,
    owner_trust_policy_path: Path | None = None,
    owner_replay_ledger_path: Path | None = None,
) -> ValidatedFinalManifest:
    resolved = path.resolve()
    root = provenance_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("reacquisition manifest is outside provenance root") from exc
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != MANIFEST_KEYS:
        raise ValueError("schema-5 final manifest has an incomplete or extra top-level key set")
    if payload.get("schema_version") != 5 or payload.get("inventory_sha256") != INVENTORY_SHA256 or payload.get("target_count") != TARGET_COUNT or payload.get("residual_count") != 0:
        raise ValueError("schema-5 final manifest header is invalid")
    if (
        not re.fullmatch(r"[0-9a-f]{32,64}", str(payload.get("run_id") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("audit_nonce_a") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("audit_nonce_b") or ""))
        or payload.get("audit_nonce_a") == payload.get("audit_nonce_b")
        or payload.get("trust_state") not in {"AWAITING_OWNER_SIGNATURE", "OWNER_SIGNED"}
        or payload.get("owner_signature_status") not in {"AWAITING_OWNER_SIGNATURE", "SIGNED"}
        or (payload.get("trust_state") == "OWNER_SIGNED") != (payload.get("owner_signature_status") == "SIGNED")
        or not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_commit") or ""))
        or not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_tree_sha256") or ""))
    ):
        raise ValueError("final manifest trust identity header is invalid")
    for field in ("audit_process_identity_a", "audit_process_identity_b"):
        identity = payload.get(field)
        if not isinstance(identity, dict) or set(identity) != {"pid", "process_start_token", "host"} or isinstance(identity.get("pid"), bool) or not isinstance(identity.get("pid"), int) or identity["pid"] <= 0 or not all(isinstance(identity.get(key), str) and identity[key] for key in ("process_start_token", "host")):
            raise ValueError(f"{field} is invalid")
    frozen = payload.get("frozen_input_hashes")
    if not isinstance(frozen, dict) or not frozen or any(not isinstance(key, str) or not key or not SHA256_RE.fullmatch(str(value)) for key, value in frozen.items()):
        raise ValueError("final manifest frozen input hash set is invalid")
    for field in ("semantic_root_sha256", "ordered_target_merkle_root_sha256", "calendar_sha256", "auditor_sha256", "node_helper_sha256", "package_lock_sha256", "http_event_root_sha256"):
        _require_digest(payload.get(field), f"final manifest {field}")
    repository_root = root.parents[2]
    from scripts.git_provenance import current_branch, validate_commit_tree
    validate_commit_tree(repository_root, str(payload.get("source_commit")), str(payload.get("source_tree_sha256")))
    if branch is not None and current_branch(repository_root) != branch:
        raise ValueError("owner branch context differs from the checked-out Git branch")
    audit_a_path = Path(str(payload.get("audit_identity_a_path") or ""))
    audit_b_path = Path(str(payload.get("audit_identity_b_path") or ""))
    if audit_a_path.is_absolute() or audit_b_path.is_absolute():
        raise ValueError("audit identity paths must be repository-relative")
    audit_a_path = (repository_root / audit_a_path).resolve()
    audit_b_path = (repository_root / audit_b_path).resolve()
    for audit_path in (audit_a_path, audit_b_path):
        try:
            audit_path.relative_to(repository_root)
        except ValueError as exc:
            raise ValueError("audit identity path escapes repository") from exc
    for audit_path, expected_hash, label in ((audit_a_path, payload.get("audit_identity_a_sha256"), "audit identity A"), (audit_b_path, payload.get("audit_identity_b_sha256"), "audit identity B")):
        if not audit_path.is_file() or digest(audit_path) != _require_digest(expected_hash, f"{label} SHA-256"):
            raise ValueError(f"{label} is missing or changed")
    audit_payloads = []
    for audit_path, label in ((audit_a_path, "audit identity A"), (audit_b_path, "audit identity B")):
        try:
            identity = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} is unreadable") from exc
        if not isinstance(identity, dict) or identity.get("schema_version") != 1:
            raise ValueError(f"{label} schema is invalid")
        audit_payloads.append(identity)
    if audit_payloads[0].get("run_id") != payload["run_id"] or audit_payloads[1].get("run_id") != payload["run_id"] or audit_payloads[0].get("audit_nonce") != payload["audit_nonce_a"] or audit_payloads[1].get("audit_nonce") != payload["audit_nonce_b"] or audit_payloads[0].get("source_commit") != payload["source_commit"] or audit_payloads[1].get("source_commit") != payload["source_commit"] or audit_payloads[0].get("source_tree_sha256") != payload["source_tree_sha256"] or audit_payloads[1].get("source_tree_sha256") != payload["source_tree_sha256"] or audit_payloads[0].get("frozen_input_hashes") != payload["frozen_input_hashes"] or audit_payloads[1].get("frozen_input_hashes") != payload["frozen_input_hashes"] or audit_payloads[0].get("process_identity") != payload["audit_process_identity_a"] or audit_payloads[1].get("process_identity") != payload["audit_process_identity_b"]:
        raise ValueError("final manifest independent audit identity bindings differ")
    external_files = {
        "calendar_sha256": repository_root / "live_forward/calendars/us_equity_rth_2022_2026_v4.json",
        "auditor_sha256": repository_root / "scripts/audit_dukascopy_reacquisition_v5.py",
        "node_helper_sha256": repository_root / "tools/dukascopy-downloader/acquire_v5.mjs",
        "package_lock_sha256": repository_root / "tools/dukascopy-downloader/package-lock.json",
        "http_event_root_sha256": repository_root / "outputs/reports/.dukascopy_acquisition_v5/http_events.jsonl",
    }
    for field, artifact in external_files.items():
        if not artifact.is_file() or digest(artifact) != payload[field]:
            raise ValueError(f"final manifest {field} is not bound to current bytes")
    inventory = load_inventory(inventory_path)
    target_rows = payload.get("targets")
    if not isinstance(target_rows, list) or len(target_rows) != TARGET_COUNT:
        raise ValueError("final manifest target list is not exactly 113 rows")
    keys = [target_key(row) for row in target_rows if isinstance(row, dict)]
    expected = {target_key(row) for row in inventory}
    if len(keys) != TARGET_COUNT or set(keys) != expected or len(set(keys)) != TARGET_COUNT or keys != sorted(keys):
        raise ValueError("final manifest target key set differs from frozen inventory")
    for row in target_rows:
        if not isinstance(row, dict) or row.get("state") not in {"VERIFIED_LEGACY", "VERIFIED_V5_BUNDLE"}:
            raise ValueError("every final target must be a verified legacy or schema-5 bundle")
        for field in ("bundle_path", "manifest_path", "minute_path", "derived_path", "attestation_sha256", "bundle_sha256"):
            if field not in row:
                raise ValueError(f"final target is missing {field}")
        for field in ("attestation_sha256", "bundle_sha256"):
            _require_digest(row[field], f"final target {field}")
        for field in ("manifest_sha256", "minute_sha256", "derived_sha256"):
            _require_digest(row.get(field), f"final target {field}")
        bundle_fields = {key: value for key, value in row.items() if key not in {"attestation_sha256", "bundle_sha256"}}
        expected_bundle_hash = sha256(json.dumps(bundle_fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if row["bundle_sha256"] != expected_bundle_hash:
            raise ValueError("final target bundle hash is invalid")
        for field in ("bundle_path", "manifest_path", "minute_path", "derived_path"):
            target_path = safe_provenance_path(root, row[field], field)
            if (target_path.is_dir() if field == "bundle_path" else target_path.is_file()) is not True:
                raise ValueError(f"final target {field} does not exist")
        bundle_dir = safe_provenance_path(root, row["bundle_path"], "bundle_path")
        if any(safe_provenance_path(root, row[field], field).parent != bundle_dir for field in ("manifest_path", "minute_path", "derived_path")):
            raise ValueError("final target files are not contained by their bundle")
        if digest(safe_provenance_path(root, row["manifest_path"], "manifest_path")) != str(row["manifest_sha256"]):
            raise ValueError("final target manifest bytes do not match")
        if digest(safe_provenance_path(root, row["minute_path"], "minute_path")) != str(row["minute_sha256"]):
            raise ValueError("final target minute bytes do not match")
        if digest(safe_provenance_path(root, row["derived_path"], "derived_path")) != str(row["derived_sha256"]):
            raise ValueError("final target derived bytes do not match")
        if row["state"] == "VERIFIED_LEGACY":
            attestation = safe_provenance_path(root, row.get("acceptance_attestation_path"), "acceptance_attestation_path")
            if not attestation.is_file() or digest(attestation) != str(row.get("acceptance_attestation_sha256")):
                raise ValueError("legacy acceptance attestation is missing or changed")
            if row["attestation_sha256"] != str(row.get("acceptance_attestation_sha256")):
                raise ValueError("legacy target attestation hash is not bound")
        else:
            if not bundle_dir.is_dir():
                raise ValueError("V5 bundle path is not a directory")
            committed = bundle_dir / "COMMITTED.json"
            if not committed.is_file():
                raise ValueError("V5 final target is not committed")
            manifest_name = safe_provenance_path(root, row["manifest_path"], "manifest_path").name
            minute_name = safe_provenance_path(root, row["minute_path"], "minute_path").name
            derived_name = safe_provenance_path(root, row["derived_path"], "derived_path").name
            attestation_name = "ATTESTATION.json"
            decoded_path = safe_provenance_path(root, row.get("decoded_path"), "decoded_path")
            if not decoded_path.is_file() or digest(decoded_path) != str(row.get("decoded_sha256")):
                raise ValueError("V5 decoded bytes do not match final target")
            if decoded_path.parent != bundle_dir:
                raise ValueError("V5 decoded file is not contained by its bundle")
            allowed = {manifest_name, minute_name, derived_name, decoded_path.name, attestation_name, "COMMITTED.json"}
            if {item.name for item in bundle_dir.iterdir()} != allowed:
                raise ValueError("V5 final target bundle contains extra files")
            committed_payload = json.loads(committed.read_text(encoding="utf-8"))
            if committed_payload != {
                "schema_version": 1,
                "manifest_name": manifest_name,
                "minute_name": minute_name,
                "derived_name": derived_name,
                "decoded_name": decoded_path.name,
                "attestation_name": attestation_name,
                "manifest_sha256": row["manifest_sha256"],
                "minute_sha256": row["minute_sha256"],
                "derived_sha256": row["derived_sha256"],
                "decoded_sha256": row["decoded_sha256"],
                "attestation_sha256": row["attestation_sha256"],
                "target": {"date": target_key(row)[0], "leg": target_key(row)[1], "timeframe": target_key(row)[2]},
            }:
                raise ValueError("V5 COMMITTED evidence does not bind final target bytes")
            bundle_manifest = json.loads(safe_provenance_path(root, row["manifest_path"], "manifest_path").read_text(encoding="utf-8"))
            if not isinstance(bundle_manifest, dict) or bundle_manifest.get("schema_version") != 5:
                raise ValueError("V5 bundle manifest schema is invalid")
            raw_chunks = bundle_manifest.get("raw_chunks")
            ordered_urls = bundle_manifest.get("ordered_urls")
            plan_hashes = bundle_manifest.get("plan_url_sha256")
            if not isinstance(raw_chunks, list) or not raw_chunks or not isinstance(ordered_urls, list) or len(raw_chunks) != len(ordered_urls):
                raise ValueError("V5 raw CAS evidence is incomplete")
            if not isinstance(plan_hashes, list) or plan_hashes != [sha256(str(url).encode()).hexdigest() for url in ordered_urls]:
                raise ValueError("V5 URL plan evidence is invalid")
            for index, chunk in enumerate(raw_chunks):
                if not isinstance(chunk, dict) or str(chunk.get("url")) != str(ordered_urls[index]) or chunk.get("url_sha256") != plan_hashes[index]:
                    raise ValueError("V5 raw chunk URL binding is invalid")
                raw_path = _manifest_artifact_path(root, chunk.get("raw_path"), "raw_path")
                raw_sha = _require_digest(chunk.get("raw_sha256"), "V5 raw_sha256")
                if not raw_path.is_file() or digest(raw_path) != raw_sha or raw_path.stat().st_size != int(chunk.get("raw_byte_count", -1)):
                    raise ValueError("V5 raw CAS bytes do not match attestation")
                if "acquisition_v5/http_cas" not in raw_path.relative_to(root).as_posix():
                    raise ValueError("V5 raw evidence is outside the HTTP CAS")
            for field, file_name in (("derived_sha256", "derived_path"), ("minute_sha256", "minute_path")):
                if bundle_manifest.get(field) != row[field]:
                    raise ValueError(f"V5 bundle manifest {field} differs from final target")
            attestation_path = row.get("attestation_path")
            if not attestation_path:
                raise ValueError("V5 detached acceptance attestation is missing")
            attestation = safe_provenance_path(root, attestation_path, "attestation_path")
            if not attestation.is_file() or digest(attestation) != row["attestation_sha256"]:
                raise ValueError("V5 detached acceptance attestation is missing or changed")
            attestation_payload = json.loads(attestation.read_text(encoding="utf-8"))
            if attestation_payload != {
                "schema_version": 1,
                "attestation_type": "V5_BUNDLE_BYTES",
                "target": {"date": target_key(row)[0], "leg": target_key(row)[1], "timeframe": target_key(row)[2]},
                "manifest_sha256": row["manifest_sha256"],
                "minute_sha256": row["minute_sha256"],
                "derived_sha256": row["derived_sha256"],
                "decoded_sha256": row["decoded_sha256"],
            }:
                raise ValueError("V5 byte attestation does not bind final target bytes")
    if semantic_root(target_rows) != payload["semantic_root_sha256"] or ordered_merkle(target_rows) != payload["ordered_target_merkle_root_sha256"]:
        raise ValueError("final manifest semantic root is invalid")
    signature_required = require_owner_signature or payload.get("owner_signature_status") == "SIGNED"
    _validate_owner_trust(
        payload,
        repository_root=repository_root,
        require_signature=signature_required,
        trusted_root_public_key_path=trusted_root_public_key_path,
        trusted_root_public_key_sha256=trusted_root_public_key_sha256,
        repository_identity=repository_identity,
        branch=branch,
        leaf_public_key_sha256=pinned_public_key_sha256,
        owner_trust_policy_path=owner_trust_policy_path,
        owner_replay_ledger_path=owner_replay_ledger_path,
    )
    if signature_required:
        _validate_detached_attestation(
            resolved,
            payload,
            attestation_path=detached_attestation_path,
            signature_path=detached_signature_path,
            public_key_path=pinned_public_key_path,
            pinned_public_key_sha256=pinned_public_key_sha256,
            expected_source_head_sha256=expected_source_head_sha256,
        )
    elif any((detached_attestation_path, detached_signature_path, pinned_public_key_path, pinned_public_key_sha256)):
        raise ValueError("owner signature inputs must be supplied together")
    return ValidatedFinalManifest(payload)


def apply_verified_reacquisitions(
    loaded: dict[tuple[str, str], pd.DataFrame],
    manifest: Mapping[str, Any],
    *,
    provenance_root: Path,
    frame_loader: Callable[[Path], pd.DataFrame],
) -> dict[tuple[str, str], pd.DataFrame]:
    """Apply only paths explicitly attested by the strict final manifest."""
    if not isinstance(manifest, ValidatedFinalManifest):
        raise ValueError("validated final manifest object is required")
    result = {key: frame.copy() for key, frame in loaded.items()}
    for row in manifest["targets"]:
        if not isinstance(row, Mapping):
            raise ValueError("verified target row is not an object")
        date_text, leg, timeframe = target_key(row)
        if date_text == "None" or leg == "None" or timeframe == "None":
            raise ValueError("verified target key is incomplete")
        symbol = "DUKASCOPY_USATECHIDXUSD" if leg == "nq" else "DUKASCOPY_USA500IDXUSD"
        path = safe_provenance_path(provenance_root.resolve(), row["derived_path"], "derived_path")
        replacement = frame_loader(path)
        if replacement.empty:
            raise ValueError(f"verified replacement is empty: {target_key(row)}")
        if (symbol, timeframe) not in result:
            raise ValueError(f"verified target dataset is not loaded: {target_key(row)}")
        result[(symbol, timeframe)] = pd.concat([result[(symbol, timeframe)], replacement], ignore_index=True).sort_values("time", kind="mergesort").drop_duplicates("time", keep="last").reset_index(drop=True)
    return result
