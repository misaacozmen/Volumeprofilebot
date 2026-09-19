"""Offline validator for the scoped Item 22 DEMO read-only acceptance."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any


SCOPE = "ITEM22_DEMO_READ_ONLY_OWNER_DECLARATION_V1"
TRUSTED_PROMOTION_MANIFEST_PATH = Path(
    r"C:\Users\ISAAC\secure-owner-evidence\item20-21-promotion-full-source-20260918-v1-manifest.json"
)
TRUSTED_PROMOTION_MANIFEST_SHA256 = "135545dbc8749cff942c627e38e518bda33ad496f35bac9981d2c30f40231476"
TRUSTED_PROMOTION_COMMIT = "51b1a952ff0d473bce966b90b734ea0fa910ecd5"
TRUSTED_PROMOTION_TREE = "47d698ccb9b5c73f342c978926529ee68d685da0"
EXPECTED_OPERATIONS = [
    "initialize",
    "account_info",
    "positions_get",
    "orders_get",
    "history_deals_get",
    "shutdown",
]
WRITE_OPERATIONS = {
    "login",
    "order_send",
    "order_check",
    "symbol_select",
    "market_book_add",
    "market_book_release",
    "copy_ticks_from",
    "copy_ticks_range",
    "copy_rates_from",
    "copy_rates_from_pos",
    "copy_rates_range",
}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX64_ANY_CASE = re.compile(r"[0-9a-fA-F]{64}\Z")
OBSERVER_GIT_PATH = "work/backtest/scripts/item22_read_only_observer.py"


class ValidationError(RuntimeError):
    """A safe, non-secret validation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"MISSING_{label}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"INVALID_{label}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"INVALID_{label}_SHAPE")
    return value


def _hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValidationError("HASH_READ_FAILED") from exc


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValidationError(code)


def _hex(value: Any, length: int, code: str) -> str:
    _require(isinstance(value, str), code)
    pattern = HEX40 if length == 40 else HEX64
    _require(pattern.fullmatch(value) is not None, code)
    return value


def _hex_any_case(value: Any, code: str) -> str:
    _require(isinstance(value, str), code)
    _require(HEX64_ANY_CASE.fullmatch(value) is not None, code)
    return value.lower()


def _utc_time(value: Any, code: str) -> datetime:
    _require(isinstance(value, str), code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(code) from exc
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, code)
    return parsed


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValidationError("GIT_LOOKUP_FAILED") from exc


def _git_bytes(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValidationError("GIT_BLOB_READ_FAILED") from exc


def _producer_commit_tree(repo: Path, source_commit: str) -> str:
    try:
        _git(repo, "cat-file", "-e", f"{source_commit}^{{commit}}")
    except ValidationError as exc:
        raise ValidationError("PRODUCER_COMMIT_MISSING") from exc
    try:
        return _git(repo, "rev-parse", f"{source_commit}^{{tree}}")
    except ValidationError as exc:
        raise ValidationError("PRODUCER_TREE_LOOKUP_FAILED") from exc


def _producer_observer_blob_sha256(repo: Path, source_commit: str) -> str:
    try:
        _git(repo, "cat-file", "-e", f"{source_commit}:{OBSERVER_GIT_PATH}")
        blob_oid = _git(repo, "rev-parse", f"{source_commit}:{OBSERVER_GIT_PATH}")
    except ValidationError as exc:
        raise ValidationError("PRODUCER_OBSERVER_BLOB_MISSING") from exc
    try:
        blob = _git_bytes(repo, "cat-file", "blob", blob_oid)
    except ValidationError as exc:
        raise ValidationError("PRODUCER_OBSERVER_BLOB_READ_FAILED") from exc
    return hashlib.sha256(blob).hexdigest()


def _validate_source_record(
    *,
    evidence: Path,
    record: dict[str, Any],
    repo: Path,
    source_commit: str,
    source_tree_oid: str,
    validator_commit: str,
    validator_tree_oid: str,
    producer_observer_blob_sha256: str,
    observer_source_sha256: str,
    probe_path: Path,
) -> list[dict[str, Any]]:
    manifest_path = TRUSTED_PROMOTION_MANIFEST_PATH
    _require(manifest_path.is_file(), "PROMOTION_MANIFEST_MISSING")
    manifest_hash = _hash(manifest_path)
    _require(manifest_hash == TRUSTED_PROMOTION_MANIFEST_SHA256, "PROMOTION_MANIFEST_HASH_MISMATCH")
    manifest = _json(manifest_path, "PROMOTION_MANIFEST")
    _require(record.get("promotion_manifest_path") == str(manifest_path), "PROMOTION_MANIFEST_PATH_UNTRUSTED")
    _require(record.get("promotion_manifest_sha256") == manifest_hash, "PROMOTION_MANIFEST_RECORD_HASH_MISMATCH")
    _require(record.get("promotion_manifest_sha256_matches") is True, "PROMOTION_MANIFEST_NOT_VERIFIED")
    _require(manifest.get("schema_version") == 1, "PROMOTION_MANIFEST_SCHEMA_INVALID")
    _require(manifest.get("package_type") == "FULL_SOURCE_DELIVERY", "PROMOTION_MANIFEST_TYPE_INVALID")
    _require(manifest.get("source_commit") == TRUSTED_PROMOTION_COMMIT, "PROMOTION_MANIFEST_COMMIT_INVALID")
    _require(manifest.get("source_tree_oid") == TRUSTED_PROMOTION_TREE, "PROMOTION_MANIFEST_TREE_INVALID")
    comparison = manifest.get("source_commit_comparison")
    _require(isinstance(comparison, dict) and comparison.get("status") == "PASS", "PROMOTION_MANIFEST_SOURCE_COMPARISON_INVALID")
    manifest_files = manifest.get("files")
    _require(isinstance(manifest_files, list) and manifest_files, "PROMOTION_MANIFEST_FILES_MISSING")
    manifest_by_path: dict[str, str] = {}
    for manifest_entry in manifest_files:
        _require(isinstance(manifest_entry, dict), "PROMOTION_MANIFEST_FILE_ENTRY_INVALID")
        manifest_file_path = manifest_entry.get("path")
        _require(isinstance(manifest_file_path, str) and manifest_file_path, "PROMOTION_MANIFEST_FILE_PATH_INVALID")
        _require(manifest_file_path not in manifest_by_path, "PROMOTION_MANIFEST_DUPLICATE_PATH")
        manifest_by_path[manifest_file_path] = _hex_any_case(manifest_entry.get("sha256"), "PROMOTION_MANIFEST_FILE_HASH_INVALID")
    _require(record.get("source_commit") == source_commit, "SOURCE_RECORD_COMMIT_MISMATCH")
    _require(record.get("source_tree_oid") == source_tree_oid, "SOURCE_RECORD_TREE_MISMATCH")
    _require(record.get("observer_commit") == source_commit, "OBSERVER_COMMIT_MISMATCH")
    _require(record.get("observer_tree_oid") == source_tree_oid, "OBSERVER_TREE_MISMATCH")
    _require(record.get("validator_commit") == validator_commit, "VALIDATOR_COMMIT_MISMATCH")
    _require(record.get("validator_tree_oid") == validator_tree_oid, "VALIDATOR_TREE_MISMATCH")
    _require(
        _hex(record.get("producer_observer_blob_sha256"), 64, "PRODUCER_OBSERVER_BLOB_HASH_INVALID")
        == producer_observer_blob_sha256,
        "PRODUCER_OBSERVER_BLOB_HASH_RECORD_MISMATCH",
    )
    _require(
        _hex(record.get("observer_source_sha256"), 64, "OBSERVER_SOURCE_HASH_INVALID")
        == observer_source_sha256,
        "OBSERVER_SOURCE_HASH_RECORD_MISMATCH",
    )
    validator_files = record.get("validator_files")
    _require(isinstance(validator_files, list) and validator_files, "VALIDATOR_FILE_RECORD_MISSING")
    expected_validator_paths = {
        "work/backtest/scripts/validate_item22_read_only_acceptance.py",
        "work/backtest/tests/test_item22_read_only_acceptance.py",
    }
    recorded_validator_paths: set[str] = set()
    for validator_file in validator_files:
        _require(isinstance(validator_file, dict), "VALIDATOR_FILE_RECORD_INVALID")
        validator_path = validator_file.get("path")
        _require(validator_path in expected_validator_paths, "VALIDATOR_FILE_PATH_INVALID")
        _require(validator_path not in recorded_validator_paths, "DUPLICATE_VALIDATOR_FILE_PATH")
        recorded_validator_paths.add(validator_path)
        actual_validator_path = (repo / validator_path).resolve()
        _require(actual_validator_path.is_file(), "VALIDATOR_FILE_MISSING")
        expected_hash = _hex(validator_file.get("sha256"), 64, "VALIDATOR_FILE_HASH_INVALID")
        _require(_hash(actual_validator_path) == expected_hash, "VALIDATOR_FILE_HASH_MISMATCH")
    _require(recorded_validator_paths == expected_validator_paths, "REQUIRED_VALIDATOR_FILE_HASH_MISSING")
    _require(record.get("promotion_manifest_expected_sha256") == TRUSTED_PROMOTION_MANIFEST_SHA256, "PROMOTION_MANIFEST_EXPECTED_HASH_UNTRUSTED")

    files = record.get("files")
    _require(isinstance(files, list) and files, "SOURCE_FILE_RECORD_MISSING")
    checks: list[dict[str, Any]] = []
    manifest_paths: set[str] = set()
    observer_entries: list[dict[str, Any]] = []
    for entry in files:
        _require(isinstance(entry, dict), "SOURCE_FILE_RECORD_INVALID")
        path_value = entry.get("source_path")
        _require(isinstance(path_value, str) and path_value, "SOURCE_FILE_PATH_INVALID")
        source_path = Path(path_value)
        _require(source_path.is_file(), "SOURCE_FILE_MISSING")
        actual = _hash(source_path)
        _require(actual == entry.get("actual_sha256"), "SOURCE_FILE_HASH_CHANGED")
        manifest_path = entry.get("manifest_path")
        if manifest_path is None:
            _require(
                entry.get("comparison") == "SEPARATE_OBSERVER_COMMIT_BOUND_SOURCE",
                "SEPARATE_OBSERVER_RECORD_INVALID",
            )
            observer_entries.append(entry)
        else:
            _require(isinstance(manifest_path, str) and manifest_path, "MANIFEST_FILE_PATH_INVALID")
            _require(manifest_path not in manifest_paths, "DUPLICATE_MANIFEST_FILE_PATH")
            manifest_paths.add(manifest_path)
            _require(manifest_path in manifest_by_path, "MANIFEST_FILE_NOT_IN_PROMOTION_MANIFEST")
            expected = manifest_by_path[manifest_path]
            _require(_hex(entry.get("manifest_sha256"), 64, "MANIFEST_FILE_HASH_INVALID") == expected, "MANIFEST_FILE_RECORD_HASH_CONFLICT")
            _require(actual == expected and entry.get("matches") is True, "MANIFEST_FILE_HASH_MISMATCH")
        checks.append({"name": f"source:{path_value}", "passed": True})

    expected_manifest_paths = {
        "work/backtest/scripts/mt5_read_only_probe.py",
        "work/backtest/scripts/mt5_read_only_acceptance.py",
        "work/backtest/scripts/run_super1_owner_acceptance.py",
        "work/backtest/scripts/owner_trust.py",
    }
    _require(expected_manifest_paths.issubset(manifest_paths), "REQUIRED_SOURCE_HASH_MISSING")
    _require(len(observer_entries) == 1, "OBSERVER_SOURCE_RECORD_INVALID")
    observer_path = (repo / "work/backtest/scripts/item22_read_only_observer.py").resolve()
    recorded_observer = Path(str(observer_entries[0]["source_path"])).resolve()
    _require(recorded_observer == observer_path, "OBSERVER_SOURCE_PATH_MISMATCH")
    _require(observer_entries[0].get("actual_sha256") == producer_observer_blob_sha256, "PRODUCER_OBSERVER_BLOB_HASH_MISMATCH")
    _require(observer_entries[0].get("actual_sha256") == observer_source_sha256, "OBSERVER_SOURCE_HASH_MISMATCH")
    _require(probe_path.is_file(), "PROBE_ARTIFACT_MISSING")
    return checks


def _validate_probe(
    *,
    probe: dict[str, Any],
    context: dict[str, Any],
    source_record: dict[str, Any],
    probe_path: Path,
) -> list[dict[str, Any]]:
    _require(probe.get("acceptance_scope") == SCOPE, "PROBE_SCOPE_MISMATCH")
    _require(probe.get("status") == "PASS_EXTERNAL", "PROBE_NOT_PASS_EXTERNAL")
    _require(probe.get("error") is None, "PROBE_ERROR_PRESENT")
    _require(probe.get("account_trade_mode") == "DEMO", "PROBE_NOT_DEMO")
    _hex(probe.get("account_identity_hmac_sha256"), 64, "ACCOUNT_HMAC_INVALID")
    _require(probe.get("run_id") == context.get("run_id"), "RUN_ID_MISMATCH")
    _require(probe.get("nonce") == context.get("nonce"), "NONCE_MISMATCH")
    _require(probe.get("source_commit") == context.get("source_commit"), "PROBE_COMMIT_MISMATCH")
    _require(probe.get("source_tree_oid") == context.get("source_tree_oid"), "PROBE_TREE_MISMATCH")

    events = probe.get("events")
    _require(isinstance(events, list), "EVENTS_MISSING")
    event_operations = []
    forbidden_events: list[str] = []
    for event in events:
        _require(isinstance(event, dict), "EVENT_INVALID")
        operation = event.get("operation")
        _require(isinstance(operation, str), "EVENT_OPERATION_INVALID")
        event_operations.append(operation)
        phase = event.get("phase")
        if phase == "call":
            _require(event.get("success") is True, "CALL_NOT_SUCCESSFUL")
            _require(event.get("reached_real_module") is True, "CALL_DID_NOT_REACH_MODULE")
            _require(event.get("result_is_none") is False, "CALL_RESULT_EMPTY")
        elif phase == "access":
            _require(event.get("reached_real_module") is False, "FORBIDDEN_ACCESS_REACHED_MODULE")
            _require(event.get("success") is False, "FORBIDDEN_ACCESS_MARKED_SUCCESS")
            forbidden_events.append(operation)
        else:
            raise ValidationError("EVENT_PHASE_INVALID")

    _require(event_operations == EXPECTED_OPERATIONS, "OPERATION_SEQUENCE_MISMATCH")
    _require(probe.get("operations") == EXPECTED_OPERATIONS, "PROBE_OPERATION_SEQUENCE_MISMATCH")
    _require(probe.get("forbidden_attempts") == forbidden_events, "FORBIDDEN_EVENT_MISMATCH")
    _require(probe.get("forbidden_calls") == len(forbidden_events), "FORBIDDEN_COUNTER_MISMATCH")
    order_send = sum(1 for event in events if event.get("operation") == "order_send")
    writes = sum(1 for event in events if event.get("operation") in WRITE_OPERATIONS)
    _require(probe.get("order_send") == order_send, "ORDER_SEND_COUNTER_MISMATCH")
    _require(probe.get("write_operations") == writes, "WRITE_COUNTER_MISMATCH")
    _require(probe.get("shutdown_attempted") is True, "SHUTDOWN_NOT_ATTEMPTED")
    _require(probe.get("shutdown_success") is True, "SHUTDOWN_NOT_SUCCESSFUL")
    _require(probe.get("initialize_attempted") is True, "INITIALIZE_NOT_ATTEMPTED")
    _require(events[-1].get("operation") == "shutdown", "SHUTDOWN_NOT_LAST")

    counts = probe.get("counts")
    _require(isinstance(counts, dict), "COUNTS_MISSING")
    for name in ("open_positions", "pending_orders", "history_deals"):
        value = counts.get(name)
        _require(type(value) is int and value >= 0, f"COUNT_INVALID_{name}")

    started = _utc_time(probe.get("started_at_utc"), "START_TIME_INVALID")
    finished = _utc_time(probe.get("finished_at_utc"), "FINISH_TIME_INVALID")
    _require(finished >= started, "TIME_ORDER_INVALID")

    expected_probe_hash = source_record.get("observed_acceptance_artifact_sha256")
    _require(_hex(expected_probe_hash, 64, "PROBE_ARTIFACT_HASH_INVALID") == _hash(probe_path), "PROBE_ARTIFACT_HASH_MISMATCH")
    return [
        {"name": "probe_scope_and_result", "passed": True},
        {"name": "operation_events_and_counters", "passed": True},
        {"name": "shutdown_and_time_bounds", "passed": True},
        {"name": "probe_artifact_hash", "passed": True},
    ]


def validate_evidence(*, evidence_dir: Path, repo: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        owner = _json(evidence_dir / "owner-declaration.json", "OWNER_DECLARATION")
        context = _json(evidence_dir / "run-context.json", "RUN_CONTEXT")
        probe = _json(evidence_dir / "mt5-read-only-observed.json", "PROBE")
        source_record = _json(evidence_dir / "source-artifact-verification.json", "SOURCE_RECORD")
        _require(not (evidence_dir / "owner-hmac.key").exists(), "OWNER_HMAC_KEY_INCLUDED")
        _require(owner.get("evidence_type") == "OWNER_CHAT_DECLARATION", "OWNER_DECLARATION_TYPE_INVALID")
        _require(owner.get("password_rotation") == "OWNER_DECLARED", "OWNER_ROTATION_NOT_DECLARED")
        _require(owner.get("rotation_time") == "NOT_PROVIDED", "UNDECLARED_ROTATION_TIME")
        _require(owner.get("read_only_probe_authorized") is True, "READ_ONLY_AUTHORIZATION_MISSING")
        _require(owner.get("push_authorized") is False, "PUSH_AUTHORIZATION_INVALID")
        checks.append({"name": "owner_declaration", "passed": True})

        run_id = _hex(context.get("run_id"), 64, "RUN_ID_INVALID")
        nonce = _hex(context.get("nonce"), 64, "NONCE_INVALID")
        source_commit = _hex(context.get("source_commit"), 40, "SOURCE_COMMIT_INVALID")
        source_tree_oid = _hex(context.get("source_tree_oid"), 40, "SOURCE_TREE_INVALID")
        validator_commit = _hex(context.get("validator_commit"), 40, "VALIDATOR_COMMIT_INVALID")
        validator_tree_oid = _hex(context.get("validator_tree_oid"), 40, "VALIDATOR_TREE_INVALID")
        observer_source_sha256 = _hex(context.get("observer_source_sha256"), 64, "OBSERVER_SOURCE_HASH_INVALID")
        _require(_git(repo, "rev-parse", "HEAD") == validator_commit, "GIT_COMMIT_MISMATCH")
        _require(_git(repo, "rev-parse", "HEAD^{tree}") == validator_tree_oid, "GIT_TREE_MISMATCH")
        _require(_git(repo, "status", "--porcelain") == "", "OBSERVER_WORKTREE_DIRTY")
        producer_tree_oid = _producer_commit_tree(repo, source_commit)
        _require(producer_tree_oid == source_tree_oid, "PRODUCER_TREE_MISMATCH")
        producer_observer_blob_sha256 = _producer_observer_blob_sha256(repo, source_commit)
        _require(observer_source_sha256 == producer_observer_blob_sha256, "OBSERVER_SOURCE_HASH_MISMATCH")
        checks.append({"name": "run_context_and_git_binding", "passed": True})
        checks.append({"name": "producer_commit_and_tree_binding", "passed": True})
        checks.append({"name": "producer_observer_blob_binding", "passed": True})

        probe_path = evidence_dir / "mt5-read-only-observed.json"
        checks.extend(
            _validate_source_record(
                evidence=evidence_dir,
                record=source_record,
                repo=repo,
                source_commit=source_commit,
                source_tree_oid=source_tree_oid,
                validator_commit=validator_commit,
                validator_tree_oid=validator_tree_oid,
                producer_observer_blob_sha256=producer_observer_blob_sha256,
                observer_source_sha256=observer_source_sha256,
                probe_path=repo / "work/backtest/scripts/item22_read_only_observer.py",
            )
        )
        checks.extend(
            _validate_probe(
                probe=probe,
                context=context,
                source_record=source_record,
                probe_path=probe_path,
            )
        )
        _require(probe.get("run_id") == run_id and probe.get("nonce") == nonce, "RUN_CONTEXT_PROBE_BINDING_MISMATCH")
        checks.append({"name": "evidence_binding", "passed": True})
        return {
            "schema_version": 1,
            "acceptance_scope": SCOPE,
            "item22_scoped_status": "CLOSED",
            "status": "PASS_ACCEPTANCE",
            "exit_code": 0,
            "checks": checks,
            "source_commit": source_commit,
            "source_tree_oid": source_tree_oid,
            "producer_observer_blob_sha256": producer_observer_blob_sha256,
            "observer_source_sha256": observer_source_sha256,
            "validator_commit": validator_commit,
            "validator_tree_oid": validator_tree_oid,
            "run_id": run_id,
            "nonce": nonce,
            "broker_credential_revocation": "NOT_INDEPENDENTLY_VERIFIED",
            "production_runtime_acceptance": "NOT_GRANTED",
            "push_authorization": "NOT_GRANTED",
        }
    except ValidationError as exc:
        return {
            "schema_version": 1,
            "acceptance_scope": SCOPE,
            "item22_scoped_status": "OPEN",
            "status": "BLOCKED_EXTERNAL_ACCEPTANCE",
            "exit_code": 2,
            "checks": checks,
            "errors": [exc.code],
            "broker_credential_revocation": "NOT_INDEPENDENTLY_VERIFIED",
            "production_runtime_acceptance": "NOT_GRANTED",
            "push_authorization": "NOT_GRANTED",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("refusing to overwrite existing validator output")
    payload = validate_evidence(evidence_dir=args.evidence_dir, repo=args.repo)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"status": payload["status"], "item22_scoped_status": payload["item22_scoped_status"], "exit_code": payload["exit_code"]}, sort_keys=True))
    return int(payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
