"""Strict V5 reacquisition manifest validation and application contract."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
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


def validate_final_manifest(path: Path, *, provenance_root: Path, inventory_path: Path) -> dict[str, Any]:
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
        value = str(payload.get(field) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"final manifest {field} is not a SHA-256 digest")
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
            value = str(row[field])
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"final target {field} is invalid")
        for field in ("manifest_sha256", "minute_sha256", "derived_sha256"):
            value = str(row.get(field) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"final target {field} is invalid")
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
        else:
            if not bundle_dir.is_dir():
                raise ValueError("V5 bundle path is not a directory")
            committed = bundle_dir / "COMMITTED.json"
            if not committed.is_file():
                raise ValueError("V5 final target is not committed")
            manifest_name = safe_provenance_path(root, row["manifest_path"], "manifest_path").name
            minute_name = safe_provenance_path(root, row["minute_path"], "minute_path").name
            derived_name = safe_provenance_path(root, row["derived_path"], "derived_path").name
            allowed = {manifest_name, minute_name, derived_name, "COMMITTED.json"}
            if {item.name for item in bundle_dir.iterdir()} != allowed:
                raise ValueError("V5 final target bundle contains extra files")
            committed_payload = json.loads(committed.read_text(encoding="utf-8"))
            if any(committed_payload.get(key) != value for key, value in {
                "manifest_name": manifest_name,
                "minute_name": minute_name,
                "derived_name": derived_name,
                "manifest_sha256": row["manifest_sha256"],
                "minute_sha256": row["minute_sha256"],
                "derived_sha256": row["derived_sha256"],
            }.items()):
                raise ValueError("V5 COMMITTED evidence does not bind final target bytes")
    if semantic_root(target_rows) != payload["semantic_root_sha256"] or ordered_merkle(target_rows) != payload["ordered_target_merkle_root_sha256"]:
        raise ValueError("final manifest semantic root is invalid")
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
        timeframe = str(row["timeframe"])
        leg = str(row["leg"])
        symbol = "DUKASCOPY_USATECHIDXUSD" if leg == "nq" else "DUKASCOPY_USA500IDXUSD"
        path = safe_provenance_path(provenance_root.resolve(), row["derived_path"], "derived_path")
        replacement = frame_loader(path)
        if replacement.empty:
            raise ValueError(f"verified replacement is empty: {target_key(row)}")
        result[(symbol, timeframe)] = pd.concat([result[(symbol, timeframe)], replacement], ignore_index=True).sort_values("time", kind="mergesort").drop_duplicates("time", keep="last").reset_index(drop=True)
    return result
