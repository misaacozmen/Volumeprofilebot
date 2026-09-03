"""Non-mutating continuation planner and fixture validator for Super1."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
import zipfile

from super1_terminal_r import calculate_terminal_r


SCHEMA_VERSION = 2
STATE_ARTIFACTS = (
    "campaign_lock",
    "minute_bars_and_conflicts_sqlite",
    "order_intents",
    "order_event_outbox",
    "broker_execution_states",
    "order_and_deal_logs",
    "prefix_final_manual_records",
    "health_fatal_recovery_records",
    "counters_and_last_risk_broker_deal_ids",
    "broker_history_sidecar",
    "positions_sidecar",
)
ALLOWED_CHANGE_LIST = (
    "continuation transition record only",
    "new release/runtime/harness verification after source snapshot",
    "RTH calendar binding and local validation evidence",
)
REQUIRED_HASH_GROUPS = ("old", "new", "unchanged_engine_and_risk")
RSA_SIGNATURE_ALGORITHM = "RSA-SHA256-PKCS1v15"
CANCEL_CONTROL_STATES = {
    "CANCEL_ARMED",
    "CANCEL_ACKNOWLEDGED",
    "CANCEL_UNKNOWN",
    "CANCEL_REJECTED",
}
LOCK_BINDING_FIELDS = (
    "schema_version",
    "created_at",
    "parent_baseline_sha256",
    "engine_code_hash",
    "live_config_hash",
    "runtime_config_hash",
    "harness_hash",
    "feed",
    "epics",
    "symbols",
    "timeframes",
    "execution",
    "account_login",
    "server",
)
RELEASE_BINDING_FIELDS = (
    "schema_version",
    "release_id",
    "profile",
    "archive_file",
    "archive_sha256",
    "created_at_utc",
    "built_at_utc",
    "git_commit",
    "git_dirty",
    "python_version",
    "python_executable_sha256",
    "pytest_command",
    "pytest_passed",
    "pytest_passed_count",
    "dependencies",
    "artifact_pytest_passed",
    "artifact_pytest_count",
    "artifact_pytest_command",
    "artifact_test_files",
    "locked_dependencies",
    "wheelhouse",
    "linux_wheelhouse",
    "files",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock_binding(lock: dict[str, Any]) -> dict[str, Any]:
    return {field: lock.get(field) for field in LOCK_BINDING_FIELDS if field in lock}


def _release_binding(manifest: dict[str, Any]) -> dict[str, Any]:
    return {field: manifest.get(field) for field in RELEASE_BINDING_FIELDS if field in manifest}


def _broker_identity_from_lock(lock: dict[str, Any]) -> str:
    return "|".join(
        str(lock.get(field) or "")
        for field in ("account_login", "server")
    )


def _path_has_reparse_component(path: Path) -> bool:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            return True
        if current.exists() and os.name == "nt":
            try:
                if int(current.stat().st_file_attributes) & 0x400:
                    return True
            except AttributeError:
                pass
        if current.parent == current:
            return False
        current = current.parent


def write_exclusive_json(
    path: Path, payload: dict[str, Any], *, source_root: Path | None = None
) -> Path:
    """Write a new artifact only; never overwrite a source or an existing output."""
    target = Path(path).absolute()
    resolved_target = target.resolve(strict=False)
    if source_root is not None:
        source = Path(source_root).resolve(strict=True)
        try:
            resolved_target.relative_to(source)
        except ValueError:
            pass
        else:
            raise ValueError("Output path is source-contained or source-equivalent.")
        if os.path.normcase(str(resolved_target)) == os.path.normcase(str(source)):
            raise ValueError("Output path is case-equivalent to the source root.")
    if _path_has_reparse_component(target.parent) or _path_has_reparse_component(target):
        raise ValueError("Output path contains a symlink or reparse-point component.")
    if target.exists():
        raise FileExistsError(f"Output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(str(target), flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def sqlite_schema_fingerprint(path: Path) -> dict[str, Any]:
    """Read-only SQLite/WAL metadata; this function never creates or mutates the DB."""
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "wal_exists": Path(f"{path}-wal").is_file(),
        "shm_exists": Path(f"{path}-shm").is_file(),
        "schema": [],
        "pragma": {},
    }
    if not result["exists"]:
        return result
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        for pragma in ("journal_mode", "schema_version", "user_version", "application_id"):
            result["pragma"][pragma] = connection.execute(f"PRAGMA {pragma}").fetchone()[0]
        tables = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        result["schema"] = [
            {
                "name": str(name),
                "sql_sha256": _digest(str(sql or "")),
                "columns": [
                    {"name": str(row[1]), "type": str(row[2]), "notnull": int(row[3]), "pk": int(row[5])}
                    for row in connection.execute(f'PRAGMA table_info("{name}")').fetchall()
                ],
            }
            for name, sql in tables
        ]
    finally:
        connection.close()
    return result


def tree_inventory(root: Path) -> list[dict[str, Any]]:
    """Capture a deterministic relative-path/size/SHA inventory before copying."""
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Inventory root is not a directory: {root}")
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _path_has_reparse_component(path):
            raise ValueError(f"Inventory contains a reparse-point path: {path}")
        relative = path.relative_to(root).as_posix()
        inventory.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": file_hash(path)}
        )
    return inventory


def compare_tree_inventory(root: Path, expected: list[dict[str, Any]]) -> dict[str, Any]:
    observed = tree_inventory(root)
    return {
        "ok": observed == expected,
        "expected": expected,
        "observed": observed,
    }


def build_transition_record(
    *,
    created_at: str | None = None,
    plan_created_at: str | None = None,
    original_campaign_lock_sha256: str | None,
    previous_transition_hash: str | None,
    old_hashes: dict[str, str | None],
    new_hashes: dict[str, str | None],
    unchanged_engine_and_risk: dict[str, str | None],
    broker_identity: str,
    source_snapshot_manifest_sha256: str | None,
    state_schema_versions: dict[str, int],
    release_root_signature: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan_time = plan_created_at or created_at
    snapshot_status = (
        "VERIFIED" if source_snapshot_manifest_sha256 else "SOURCE_SNAPSHOT_NOT_VERIFIED"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_origin": "CURRENT_SUPER1_CAMPAIGN_CONTINUATION",
        "plan_created_at": plan_time,
        "source_campaign_created_at": None,
        "campaign_root_binding": None,
        "original_campaign_lock_sha256": original_campaign_lock_sha256,
        "source_root_lock": {
            "status": "VERIFIED" if original_campaign_lock_sha256 else "UNRESOLVED",
            "path": None,
            "sha256": original_campaign_lock_sha256,
        },
        "previous_transition": {
            "status": "VERIFIED" if previous_transition_hash else "UNRESOLVED",
            "path": None,
            "sha256": previous_transition_hash,
        },
        "previous_transition_hash": previous_transition_hash,
        "hashes": {
            "old": dict(old_hashes),
            "new": dict(new_hashes),
            "unchanged_engine_and_risk": dict(unchanged_engine_and_risk),
        },
        "broker_identity_digest": _digest(broker_identity),
        "source_state_snapshot": {
            "status": snapshot_status,
            "manifest_sha256": source_snapshot_manifest_sha256,
            "manifest_path": None,
            "inventory": list(STATE_ARTIFACTS),
            "members": [],
        },
        "state_schema_versions": dict(state_schema_versions),
        "allowed_change_list": list(ALLOWED_CHANGE_LIST),
        "release_root_signature": release_root_signature
        or {"status": "NOT_VERIFIED", "signature": None},
        "hash_semantics": {
            "old": "source snapshot bytes; null until the source is actually read",
            "new": "locally observed bytes; not a release or source-state claim",
            "release": "release root bytes/signature; null because no release is produced here",
            "harness": "harness input bytes; distinct from release and raw runtime bytes",
        },
        "apply_allowed": False,
    }


def validate_transition_record(record: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version")
    if record.get("campaign_origin") != "CURRENT_SUPER1_CAMPAIGN_CONTINUATION":
        errors.append("campaign_origin")
    if not isinstance(record.get("plan_created_at"), str):
        errors.append("plan_created_at")
    if record.get("source_campaign_created_at") is not None:
        errors.append("source_campaign_created_at")
    if not isinstance(record.get("broker_identity_digest"), str) or len(
        record.get("broker_identity_digest", "")
    ) != 64:
        errors.append("broker_identity_digest")
    hashes = record.get("hashes")
    if not isinstance(hashes, dict):
        errors.append("hashes")
    else:
        for group in REQUIRED_HASH_GROUPS:
            values = hashes.get(group)
            if not isinstance(values, dict) or not values:
                errors.append(f"hashes.{group}")
                continue
            if any(
                not isinstance(value, str) or len(value) != 64
                for value in values.values()
                if value is not None
            ):
                errors.append(f"hashes.{group}.format")
    if tuple(record.get("allowed_change_list", ())) != ALLOWED_CHANGE_LIST:
        errors.append("allowed_change_list")
    snapshot = record.get("source_state_snapshot")
    if not isinstance(snapshot, dict) or not set(STATE_ARTIFACTS).issubset(
        snapshot.get("inventory", ())
    ):
        errors.append("source_state_snapshot.inventory")
    snapshot_status = snapshot.get("status") if isinstance(snapshot, dict) else None
    if errors:
        return {"status": "INVALID", "safe_to_apply": False, "errors": errors}
    if snapshot_status != "VERIFIED":
        return {
            "status": "SOURCE_SNAPSHOT_NOT_VERIFIED",
            "safe_to_apply": False,
            "errors": [],
        }
    if record.get("release_root_signature", {}).get("status") != "VERIFIED":
        return {
            "status": "RELEASE_SIGNATURE_NOT_VERIFIED",
            "safe_to_apply": False,
            "errors": [],
        }
    return {
        "status": "EVIDENCE_NOT_VALIDATED",
        "safe_to_apply": False,
        "errors": ["actual root lock, snapshot members, transition bytes, and signatures required"],
    }


TEST_SIGNATURE_ALGORITHM = RSA_SIGNATURE_ALGORITHM


def canonical_transition_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Fields that authorize a transition; signatures never authorize extra fields."""
    source_root = record.get("source_root_lock")
    previous = record.get("previous_transition")
    snapshot = record.get("source_state_snapshot")
    release = record.get("release_root_signature")
    hashes = record.get("hashes")
    return {
        "schema_version": record.get("schema_version"),
        "campaign_origin": record.get("campaign_origin"),
        "plan_created_at": record.get("plan_created_at"),
        "source_campaign_created_at": record.get("source_campaign_created_at"),
        "campaign_root_binding": record.get("campaign_root_binding"),
        "original_campaign_lock_sha256": record.get("original_campaign_lock_sha256"),
        "source_root_lock": {
            "status": None if not isinstance(source_root, dict) else source_root.get("status"),
            "path": None if not isinstance(source_root, dict) else source_root.get("path"),
            "sha256": None if not isinstance(source_root, dict) else source_root.get("sha256"),
        },
        "previous_transition": {
            "status": None if not isinstance(previous, dict) else previous.get("status"),
            "path": None if not isinstance(previous, dict) else previous.get("path"),
            "sha256": None if not isinstance(previous, dict) else previous.get("sha256"),
        },
        "previous_transition_hash": record.get("previous_transition_hash"),
        "broker_identity_digest": record.get("broker_identity_digest"),
        "hashes": hashes,
        "source_state_snapshot": {
            "status": None if not isinstance(snapshot, dict) else snapshot.get("status"),
            "manifest_sha256": None if not isinstance(snapshot, dict) else snapshot.get("manifest_sha256"),
            "manifest_path": None if not isinstance(snapshot, dict) else snapshot.get("manifest_path"),
            "members": None if not isinstance(snapshot, dict) else snapshot.get("members"),
        },
        "state_schema_versions": record.get("state_schema_versions"),
        "allowed_change_list": record.get("allowed_change_list"),
        "unchanged_engine_and_risk": None if not isinstance(hashes, dict) else hashes.get("unchanged_engine_and_risk"),
        "release_root_binding": {
            "status": None if not isinstance(release, dict) else release.get("status"),
            "path": None if not isinstance(release, dict) else release.get("path"),
            "sha256": None if not isinstance(release, dict) else release.get("sha256"),
            "manifest": None if not isinstance(release, dict) else release.get("manifest"),
        },
    }


def _verify_rsa_signature(
    path: Path, signature: dict[str, Any], trusted_public_key: bytes | None
) -> bool:
    if (
        not isinstance(signature, dict)
        or signature.get("algorithm") != RSA_SIGNATURE_ALGORITHM
        or trusted_public_key is None
        or signature.get("payload_sha256") != file_hash(path)
    ):
        return False
    try:
        from base64 import b64decode
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        public_key = serialization.load_pem_public_key(trusted_public_key)
        public_key.verify(
            b64decode(str(signature.get("signature") or ""), validate=True),
            path.read_bytes(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def validate_actual_transition_evidence(
    record: dict[str, Any],
    evidence_root: Path,
    *,
    trusted_public_key: bytes | None = None,
    test_key: bytes | None = None,
) -> dict[str, Any]:
    """Validate a complete RSA-signed transition chain without trusting payload key claims."""
    if trusted_public_key is None and test_key is not None and test_key.startswith(b"-----BEGIN"):
        trusted_public_key = test_key
    errors: list[str] = []

    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("record.schema_version")
    hashes = record.get("hashes")
    if not isinstance(hashes, dict):
        errors.append("record.hashes")
    else:
        for group in REQUIRED_HASH_GROUPS:
            values = hashes.get(group)
            if not isinstance(values, dict) or not values:
                errors.append(f"record.hashes.{group}")
            elif any(
                not isinstance(value, str) or len(value) != 64
                for value in values.values()
                if value is not None
            ):
                errors.append(f"record.hashes.{group}.format")
    state_schema_versions = record.get("state_schema_versions")
    if (
        not isinstance(state_schema_versions, dict)
        or any(
            not isinstance(state_schema_versions.get(name), int)
            for name in STATE_ARTIFACTS
        )
    ):
        errors.append("record.state_schema_versions")

    def verify_file(spec: Any, label: str, *, signature: bool = False) -> Path | None:
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
            errors.append(f"{label}.path")
            return None
        path = (evidence_root / spec["path"]).resolve()
        try:
            path.relative_to(evidence_root.resolve())
        except ValueError:
            errors.append(f"{label}.containment")
            return None
        if not path.is_file():
            errors.append(f"{label}.missing")
            return None
        if spec.get("sha256") != file_hash(path) or spec.get("bytes") != path.stat().st_size:
            errors.append(f"{label}.bytes")
        if signature and not _verify_rsa_signature(
            path, spec.get("signature"), trusted_public_key
        ):
            errors.append(f"{label}.signature")
        return path

    root_lock = record.get("source_root_lock")
    # campaign_lock is an input artifact, not an independently signed claim;
    # its raw bytes/hash and exact producer fields are signed through the
    # canonical transition payload.
    root_path = verify_file(root_lock, "source_root_lock", signature=False)
    release_root = record.get("release_root_signature")
    release_path = verify_file(release_root, "release_root_signature", signature=True)
    release_manifest: dict[str, Any] | None = None
    if release_path is not None:
        try:
            parsed_release = json.loads(release_path.read_text(encoding="utf-8"))
            if isinstance(parsed_release, dict):
                release_manifest = parsed_release
            else:
                errors.append("release_root_signature.schema")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append("release_root_signature.json")
    if release_manifest is not None:
        missing_release_fields = [
            field for field in RELEASE_BINDING_FIELDS if field not in release_manifest
        ]
        if missing_release_fields:
            errors.append("release_root_signature.manifest_fields")
        else:
            release_binding = release_root.get("manifest") if isinstance(release_root, dict) else None
            if release_binding != _release_binding(release_manifest):
                errors.append("release_root_signature.manifest_binding")
            archive_name = str(release_manifest.get("archive_file") or "")
            archive_path = (evidence_root / archive_name).resolve()
            try:
                archive_path.relative_to(evidence_root.resolve())
            except ValueError:
                errors.append("release_root_signature.archive_containment")
            else:
                if not archive_path.is_file():
                    errors.append("release_root_signature.archive_missing")
                elif file_hash(archive_path) != str(release_manifest.get("archive_sha256")):
                    errors.append("release_root_signature.archive_hash")
                else:
                    try:
                        with zipfile.ZipFile(archive_path) as archive:
                            actual_members = []
                            for entry in sorted(archive.infolist(), key=lambda item: item.filename):
                                if entry.is_dir():
                                    continue
                                actual_members.append(
                                    {
                                        "path": entry.filename,
                                        "sha256": hashlib.sha256(archive.read(entry)).hexdigest(),
                                    }
                                )
                            if actual_members != release_manifest.get("files"):
                                errors.append("release_root_signature.archive_members")
                    except (OSError, zipfile.BadZipFile):
                        errors.append("release_root_signature.archive_invalid")
    snapshot = record.get("source_state_snapshot")
    manifest_path = verify_file(snapshot, "source_state_snapshot", signature=False)
    if manifest_path is not None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            members = manifest.get("members") if isinstance(manifest, dict) else None
            expected_members = snapshot.get("members") if isinstance(snapshot, dict) else None
            if not isinstance(members, list) or members != expected_members:
                errors.append("source_state_snapshot.members")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append("source_state_snapshot.json")
    for member in (snapshot.get("members", []) if isinstance(snapshot, dict) else []):
        verify_file(member, f"snapshot_member:{member.get('path')}")
    transition = record.get("transition_evidence")
    transition_path = verify_file(transition, "transition_evidence", signature=True)
    if root_path is not None and root_lock.get("sha256") != file_hash(root_path):
        errors.append("source_root_lock.sha256")
    if root_path is not None and record.get("original_campaign_lock_sha256") != file_hash(root_path):
        errors.append("original_campaign_lock_sha256")
    if root_path is not None:
        try:
            lock_payload = json.loads(root_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            lock_payload = None
            errors.append("source_root_lock.json")
        binding = record.get("campaign_root_binding")
        forbidden = {"campaign", "account_server_binding"}
        if not isinstance(lock_payload, dict) or not isinstance(binding, dict):
            errors.append("campaign_root_binding.missing")
        elif (
            binding.get("raw_lock_sha256") != file_hash(root_path)
            or binding.get("fields") != _lock_binding(lock_payload)
            or not set(LOCK_BINDING_FIELDS).issubset(lock_payload)
            or forbidden.intersection(lock_payload)
            or forbidden.intersection(binding.get("fields", {}))
            or record.get("source_campaign_created_at") != lock_payload.get("created_at")
            or record.get("broker_identity_digest")
            != _digest(_broker_identity_from_lock(lock_payload))
        ):
            errors.append("campaign_root_binding.semantic")
    if manifest_path is not None and snapshot.get("manifest_sha256") != file_hash(manifest_path):
        errors.append("source_state_snapshot.manifest_sha256")
    if transition_path is not None:
        try:
            signed_payload = json.loads(transition_path.read_text(encoding="utf-8"))
            if signed_payload != canonical_transition_payload(record):
                errors.append("transition_evidence.payload")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append("transition_evidence.json")

    previous = record.get("previous_transition")
    previous_path: Path | None = None
    if (
        not isinstance(previous, dict)
        or previous.get("status") != "VERIFIED"
        or not record.get("previous_transition_hash")
    ):
        errors.append("previous_transition.unresolved")
    else:
        if previous.get("sha256") != record.get("previous_transition_hash"):
            errors.append("previous_transition.hash_link")
        previous_path = verify_file(previous, "previous_transition", signature=True)
        if previous_path is not None:
            try:
                predecessor = json.loads(previous_path.read_text(encoding="utf-8"))
                predecessor_hash = predecessor.get("predecessor_hash") if isinstance(predecessor, dict) else None
                if predecessor_hash in {
                    str((transition or {}).get("sha256")),
                    str(record.get("previous_transition_hash")),
                }:
                    errors.append("previous_transition.cycle")
                if not isinstance(predecessor, dict):
                    errors.append("previous_transition.schema")
                else:
                    predecessor_hashes = predecessor.get("hashes") or {}
                    current_hashes = record.get("hashes") or {}
                    if predecessor_hashes.get("new") != current_hashes.get("old"):
                        errors.append("previous_transition.old_new_link")
                    if predecessor.get("campaign_root_binding") != record.get("campaign_root_binding"):
                        errors.append("previous_transition.root_binding")
                    if predecessor.get("source_campaign_created_at") != record.get(
                        "source_campaign_created_at"
                    ):
                        errors.append("previous_transition.created_at")
                    if predecessor.get("state_schema_versions") != record.get(
                        "state_schema_versions"
                    ):
                        errors.append("previous_transition.state_schema")
                    predecessor_snapshot = predecessor.get("source_state_snapshot") or {}
                    current_snapshot = record.get("source_state_snapshot") or {}
                    if predecessor_snapshot.get("inventory") != current_snapshot.get("inventory"):
                        errors.append("previous_transition.snapshot_inventory")
                    if predecessor_hashes.get("unchanged_engine_and_risk") != (
                        current_hashes.get("unchanged_engine_and_risk")
                    ):
                        errors.append("previous_transition.frozen_risk_link")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                errors.append("previous_transition.json")
        if previous.get("path") == (transition or {}).get("path"):
            errors.append("previous_transition.duplicate_current")
    return {
        "status": "PASS" if not errors else "FAIL",
        "safe_to_apply": False,
        "errors": errors,
        "synthetic_crypto_fixture": bool(trusted_public_key),
    }


def inventory_snapshot_root(root: Path) -> dict[str, Any]:
    """Inspect only; never copies, changes, or deletes campaign state."""
    paths = {
        "campaign_lock": root / "campaign_lock.json",
        "minute_bars_and_conflicts_sqlite": root / "cache" / "capital_bars.sqlite3",
        "order_intents": root / "orders" / "idempotency.sqlite3",
        "order_event_outbox": root / "orders" / "idempotency.sqlite3",
        "broker_execution_states": root / "orders" / "idempotency.sqlite3",
        "order_and_deal_logs": root / "orders" / "events.jsonl",
        "prefix_final_manual_records": root / "prefix",
        "finalized_records": root / "finalized",
        "raw_records": root / "raw",
        "manual_records": root / "manual",
        "daily_health": root / "daily_health",
        "health": root / "health.json",
        "fatal_latch": root / "fatal_latch.json",
        "launcher_failure": root / "launcher_failure.json",
        "broker_recovery_required": root / "runtime" / "broker_recovery_required.json",
        "broker_history_sidecar": root / "orders" / "broker-history.json",
        "positions_sidecar": root / "orders" / "positions.json",
        "broker_recovery": root / "broker_recovery",
        "technical_failures": root / "technical_failures",
        "counters_and_last_risk_broker_deal_ids": root / "orders" / "idempotency.sqlite3",
    }
    result = {
        name: {"path": str(path), "exists": path.exists(), "is_file": path.is_file()}
        for name, path in paths.items()
    }
    result["minute_bars_and_conflicts_sqlite"]["sqlite"] = sqlite_schema_fingerprint(paths["minute_bars_and_conflicts_sqlite"])
    result["order_intents"]["sqlite"] = sqlite_schema_fingerprint(paths["order_intents"])
    result["counters_and_last_risk_broker_deal_ids"]["derived_from"] = {
        "sqlite_path": str(paths["counters_and_last_risk_broker_deal_ids"]),
        "query": "broker_execution_states ordered by updated_at; terminal R values from recorded event/store history",
    }
    return result


def validate_fixture_snapshot(fixture: dict[str, Any]) -> dict[str, Any]:
    if "source_root" in fixture or "snapshot_root" in fixture:
        return validate_real_snapshot_state(
            Path(str(fixture.get("source_root"))),
            Path(str(fixture.get("snapshot_root"))),
        )
    checks = {
        "created_at_preserved": fixture.get("created_at_before") == fixture.get("created_at_after"),
        "order_ids_preserved": fixture.get("order_ids_before") == fixture.get("order_ids_after"),
        "unknown_status_preserved": fixture.get("unknown_before") == fixture.get("unknown_after"),
        "outbox_order_preserved": fixture.get("outbox_before") == fixture.get("outbox_after"),
        "deal_dedup_preserved": len(set(fixture.get("deal_tickets_after", ())))
        == len(fixture.get("deal_tickets_after", ())),
        "completed_results_preserved": fixture.get("completed_before") == fixture.get("completed_after"),
        "last_ten_terminal_r_preserved": fixture.get("terminal_r_before")[-10:]
        == fixture.get("terminal_r_after")[-10:],
        "more_than_ten_terminal_r_supported": len(fixture.get("terminal_r_after", ())) > 10,
    }
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks}


FIXTURE_CASES = (
    "over_ten_r",
    "unknown_intent",
    "outbox_ack_order",
    "deal_set",
    "lost_wal",
    "deal_mutate",
    "deal_delete",
    "outbox_corruption",
    "truncated_risk_history",
    "missing_required",
    "other_campaign",
)


def validate_fixture_cases(cases: dict[str, Any]) -> dict[str, bool]:
    """Evaluate recorded validator results; caller cannot supply a blocked flag."""
    result: dict[str, bool] = {}
    for case in FIXTURE_CASES:
        item = cases.get(case) if isinstance(cases, dict) else None
        if not isinstance(item, dict):
            result[f"fixture_case_{case}_blocked"] = False
            continue
        observation = item.get("observation")
        result[f"fixture_case_{case}_blocked"] = bool(
            isinstance(observation, dict)
            and observation.get("status") == "FAIL"
            and observation.get("errors")
        )
    return result


def _sqlite_rows(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "tables": {}}
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        payload: dict[str, Any] = {
            "exists": True,
            "wal_exists": Path(f"{path}-wal").is_file(),
            "shm_exists": Path(f"{path}-shm").is_file(),
            "tables": {},
        }
        for table in tables:
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            payload["tables"][table] = {
                "columns": columns,
                "rows": [
                    [value.hex() if isinstance(value, bytes) else value for value in row]
                    for row in rows
                ],
            }
        committed = json.dumps(payload["tables"], sort_keys=True, separators=(",", ":"))
        payload["last_committed_rows_sha256"] = _digest(committed)
        return payload
    finally:
        connection.close()


def _json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _jsonl_file(path: Path) -> list[Any]:
    if not path.is_file():
        return []
    rows: list[Any] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    return rows


BROKER_HISTORY_FIELDS = (
    "time_msc",
    "ticket",
    "position_id",
    "order",
    "entry",
    "reason",
    "symbol",
    "volume",
    "magic",
    "raw_r",
)
POSITION_FIELDS = (
    "ticket",
    "identifier",
    "magic",
    "symbol",
    "type",
    "volume",
    "sl",
    "tp",
)


def _sidecar_records(root: Path, name: str, fields: tuple[str, ...]) -> dict[str, Any]:
    payload = _json_file(root / name)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return {"invalid": True}
    records = payload.get("records")
    if not isinstance(records, list):
        return {"invalid": True}
    if any(not isinstance(row, dict) or not set(fields).issubset(row) for row in records):
        return {"invalid": True}
    return payload


def _terminal_r_from_sidecars(
    broker_history: dict[str, Any], positions: dict[str, Any],
    reward_by_symbol: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    history = list(broker_history.get("records", []))
    magic_values = {int(row.get("magic")) for row in history if row.get("magic") is not None}
    if len(magic_values) != 1:
        raise ValueError("sidecar terminal-R magic identity is ambiguous")
    computed = calculate_terminal_r(
        history,
        list(positions.get("records", [])),
        magic=next(iter(magic_values)),
        reward_by_symbol=reward_by_symbol or {"US100Cash": 3.0, "US500Cash": 2.5},
        lookback=len(history),
    )
    by_ticket = {int(row.get("ticket")): row for row in history}
    return [
        {**by_ticket.get(int(row["ticket"]), {}), "computed_r": row["r"], "r": row["r"]}
        for row in computed
    ]


def read_state_snapshot(root: Path) -> dict[str, Any]:
    """Read the economic/state records from independent read-only connections."""
    root = Path(root).resolve()
    if not root.is_dir():
        return {}
    lock_path = root / "campaign_lock.json"
    bars_path = root / "cache" / "capital_bars.sqlite3"
    orders_path = root / "orders" / "idempotency.sqlite3"
    events_path = root / "orders" / "events.jsonl"
    broker_history_path = root / "orders" / "broker-history.json"
    positions_path = root / "orders" / "positions.json"
    required = (
        lock_path,
        bars_path,
        orders_path,
        events_path,
        root / "health.json",
        broker_history_path,
        positions_path,
    )
    if not all(path.exists() for path in required):
        return {
            "missing_required": [str(path.relative_to(root)) for path in required if not path.exists()]
        }
    events = _jsonl_file(events_path)
    broker_history = _sidecar_records(root, "orders/broker-history.json", BROKER_HISTORY_FIELDS)
    positions = _sidecar_records(root, "orders/positions.json", POSITION_FIELDS)
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or path.name in {"capital_bars.sqlite3", "idempotency.sqlite3"}
            or path.name.endswith((".sqlite3-wal", ".sqlite3-shm"))
        ):
            continue
        files[path.relative_to(root).as_posix()] = file_hash(path)
    terminal_r: list[dict[str, Any]] = []
    terminal_r_error: str | None = None
    if not broker_history.get("invalid") and not positions.get("invalid"):
        try:
            terminal_r = _terminal_r_from_sidecars(broker_history, positions)
        except (TypeError, ValueError, KeyError) as exc:
            terminal_r_error = str(exc)
    return {
        "campaign_lock": _json_file(lock_path),
        "health": _json_file(root / "health.json"),
        "fatal_latch": _json_file(root / "fatal_latch.json"),
        "launcher_failure": _json_file(root / "launcher_failure.json"),
        "broker_recovery_required": _json_file(root / "runtime" / "broker_recovery_required.json"),
        "minute_bars_and_conflicts": _sqlite_rows(bars_path),
        "order_store": _sqlite_rows(orders_path),
        "order_events": events,
        "broker_history": broker_history,
        "positions": positions,
        "sidecar_bindings": {
            "orders/broker-history.json": {
                "bytes": broker_history_path.stat().st_size,
                "sha256": file_hash(broker_history_path),
            },
            "orders/positions.json": {
                "bytes": positions_path.stat().st_size,
                "sha256": file_hash(positions_path),
            },
        },
        "terminal_r": terminal_r,
        "terminal_r_error": terminal_r_error,
        "deal_tickets": sorted(
            {
                int(row.get("broker_ticket"))
                for row in events
                if row.get("event") == "DEAL" and row.get("broker_ticket") is not None
            }
        ),
        "files": files,
    }


def validate_real_snapshot_state(source_root: Path, snapshot_root: Path) -> dict[str, Any]:
    """Compare source and SQLite-backup snapshot state; no expected outcome is supplied."""
    source = read_state_snapshot(Path(source_root))
    snapshot = read_state_snapshot(Path(snapshot_root))
    errors: list[str] = []
    if not source or "missing_required" in source:
        errors.append("source.required_state_missing")
    if not snapshot or "missing_required" in snapshot:
        errors.append("snapshot.required_state_missing")
    def integrity_errors(state: dict[str, Any], label: str) -> list[str]:
        result: list[str] = []
        lock = state.get("campaign_lock")
        if (
            not isinstance(lock, dict)
            or lock.get("schema_version") != 1
            or not isinstance(lock.get("created_at"), str)
            or not isinstance(lock.get("account_login"), int)
            or not isinstance(lock.get("server"), str)
            or not isinstance(lock.get("magic_number"), int)
            or not isinstance(lock.get("campaign_root"), str)
            or "campaign" in lock
            or "account_server_binding" in lock
        ):
            result.append(f"{label}.campaign_lock.schema")
        broker_history = state.get("broker_history") or {}
        positions = state.get("positions") or {}
        if broker_history.get("invalid"):
            result.append(f"{label}.broker_history.sidecar")
        if positions.get("invalid"):
            result.append(f"{label}.positions.sidecar")
        for sidecar_name, sidecar in (
            ("broker_history", broker_history),
            ("positions", positions),
        ):
            if (
                not isinstance(sidecar.get("observed_at"), str)
                or sidecar.get("terminal_history_days") != 365
            ):
                result.append(f"{label}.{sidecar_name}.observation_window")
        if isinstance(lock, dict):
            for sidecar_name, sidecar in (
                ("broker_history", broker_history),
                ("positions", positions),
            ):
                if (
                    sidecar.get("account_login") != lock.get("account_login")
                    or sidecar.get("server") != lock.get("server")
                    or sidecar.get("campaign_root") != lock.get("campaign_root")
                ):
                    result.append(f"{label}.{sidecar_name}.identity")
                records = sidecar.get("records", [])
                if lock.get("magic_number") is not None and any(
                    int(row.get("magic", -1)) != int(lock.get("magic_number"))
                    for row in records if isinstance(row, dict)
                ):
                    result.append(f"{label}.{sidecar_name}.magic")
        order_store = state.get("order_store") or {}
        tables = order_store.get("tables") or {}
        required_tables = {"order_intents", "order_event_outbox", "broker_execution_states"}
        if not required_tables.issubset(tables):
            result.append(f"{label}.order_store.required_tables")
        for table in required_tables:
            if table in tables and not tables[table].get("rows"):
                result.append(f"{label}.order_store.{table}.empty")
        outbox_rows = tables.get("order_event_outbox", {}).get("rows", [])
        sequences: list[int] = []
        for row in outbox_rows:
            try:
                sequence = int(row[0])
                event = json.loads(str(row[2]))
            except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                result.append(f"{label}.outbox.row")
                continue
            if not isinstance(event, dict) or str(event.get("order_id")) != str(row[1]):
                result.append(f"{label}.outbox.binding")
            sequences.append(sequence)
        if sequences != sorted(set(sequences)):
            result.append(f"{label}.outbox.sequence")
        intent_rows = tables.get("order_intents", {}).get("rows", [])
        intent_ids = {str(row[0]) for row in intent_rows if row}
        event_rows = state.get("order_events") or []
        if not event_rows:
            result.append(f"{label}.order_events.empty")
        deal_tickets = [
            int(row["broker_ticket"])
            for row in event_rows
            if row.get("event") == "DEAL" and row.get("broker_ticket") is not None
        ]
        if len(deal_tickets) != len(set(deal_tickets)):
            result.append(f"{label}.deal_tickets.duplicate")
        for row in event_rows:
            if row.get("event") in CANCEL_CONTROL_STATES:
                if not row.get("order_id") or row.get("broker_order_ticket", row.get("ticket")) is None:
                    result.append(f"{label}.cancel_event.binding")
                if str(row.get("order_id")) not in intent_ids:
                    result.append(f"{label}.cancel_event.intent_missing")
        for row in state.get("terminal_r", ()):
            if not all(key in row for key in ("time_msc", "ticket", "computed_r")):
                result.append(f"{label}.terminal_r.fields")
            if row.get("computed_r") not in {-1.0, 3.0, 2.5}:
                result.append(f"{label}.terminal_r.value")
        bars = (state.get("minute_bars_and_conflicts") or {}).get("tables", {}).get("minute_bars", {})
        if not bars.get("rows"):
            result.append(f"{label}.bars.empty")
        return result

    if source and "missing_required" not in source:
        errors.extend(integrity_errors(source, "source"))
    if snapshot and "missing_required" not in snapshot:
        errors.extend(integrity_errors(snapshot, "snapshot"))
    if not errors:
        source_bars = source.get("minute_bars_and_conflicts") or {}
        snapshot_bars = snapshot.get("minute_bars_and_conflicts") or {}
        def rows_only(db: dict[str, Any]) -> dict[str, Any]:
            return {
                "tables": db.get("tables", {}),
                "last_committed_rows_sha256": db.get("last_committed_rows_sha256"),
            }

        if rows_only(source_bars) != rows_only(snapshot_bars):
            errors.append("minute_bars_and_conflicts.wal_continuity")
        source_orders = source.get("order_store") or {}
        snapshot_orders = snapshot.get("order_store") or {}
        if rows_only(source_orders) != rows_only(snapshot_orders):
            errors.append("order_store.wal_continuity")
        for key in (
            "campaign_lock",
            "health",
            "fatal_latch",
            "launcher_failure",
            "broker_recovery_required",
            "order_events",
            "broker_history",
            "positions",
            "sidecar_bindings",
            "terminal_r",
            "deal_tickets",
            "files",
        ):
            if source.get(key) != snapshot.get(key):
                errors.append(f"{key}.mismatch")
        if len(source.get("terminal_r", ())) <= 10:
            errors.append("fixture.terminal_r_coverage")
        if len(source.get("deal_tickets", ())) != len(set(source.get("deal_tickets", ()) )):
            errors.append("deal_tickets.duplicate")
    return {
        "status": "PASS" if not errors else "FAIL",
        "safe_to_apply": False,
        "errors": errors,
        "source_observed": source,
        "snapshot_observed": snapshot,
    }


def canonical_hash(record: dict[str, Any]) -> str:
    return _digest(json.dumps(record, sort_keys=True, separators=(",", ":")))
