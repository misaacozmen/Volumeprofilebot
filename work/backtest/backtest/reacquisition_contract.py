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
MANIFEST_KEYS = frozenset({
    "schema_version", "inventory_sha256", "target_count", "residual_count", "targets",
    "semantic_root_sha256", "ordered_target_merkle_root_sha256", "calendar_sha256",
    "auditor_sha256", "node_helper_sha256", "package_lock_sha256", "http_event_root_sha256",
})


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _require_digest(value: object, label: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} is not a lowercase SHA-256 digest")
    return text


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
) -> dict[str, Any]:
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
    for field in ("semantic_root_sha256", "ordered_target_merkle_root_sha256", "calendar_sha256", "auditor_sha256", "node_helper_sha256", "package_lock_sha256", "http_event_root_sha256"):
        _require_digest(payload.get(field), f"final manifest {field}")
    repository_root = root.parents[2]
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
    _validate_detached_attestation(
        resolved,
        payload,
        attestation_path=detached_attestation_path,
        signature_path=detached_signature_path,
        public_key_path=pinned_public_key_path,
        pinned_public_key_sha256=pinned_public_key_sha256,
        expected_source_head_sha256=expected_source_head_sha256,
    )
    return payload


def apply_verified_reacquisitions(
    loaded: dict[tuple[str, str], pd.DataFrame],
    manifest: Mapping[str, Any],
    *,
    provenance_root: Path,
    frame_loader: Callable[[Path], pd.DataFrame],
) -> dict[tuple[str, str], pd.DataFrame]:
    """Apply only paths explicitly attested by the strict final manifest."""
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
