"""Fail-closed demo repository acceptance without RSA/replay/lease artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any

try:
    from scripts.git_provenance import current_branch, current_commit_tree
except ModuleNotFoundError:
    from git_provenance import current_branch, current_commit_tree


HEX40 = set("0123456789abcdef")
HEX64 = set("0123456789abcdef")
OWNER_KEYS = frozenset({
    "schema_version", "statement_type", "status", "declarant_role", "purpose",
    "rotation_date", "old_credential_not_used", "credential_values_shared",
})
SCAN_KEYS = frozenset({
    "schema_version", "report_type", "status", "repository_identity", "branch",
    "source_commit", "source_tree_oid", "private_denylist_sha256", "working_tree_match_count",
    "reachable_history_match_count", "ref_name_match_count", "quarantine_match_count",
    "scanner_source_sha256", "inventory_sha256",
})
HISTORY_KEYS = frozenset({
    "schema_version", "report_type", "status", "repository_identity", "branch",
    "source_commit", "source_tree_oid", "private_denylist_sha256", "reachable_history_match_count",
    "sanitized_mirror_match_count", "source_ref_inventory_sha256", "sanitized_ref_inventory_sha256",
    "topology_preserved", "source_unchanged", "mirror_external", "scanner_source_sha256",
})
PUSH_KEYS = frozenset({
    "schema_version", "approval_type", "status", "repository_identity", "branch", "source_commit",
    "source_tree_oid", "destination_ref", "approved", "approved_at_utc", "approval_scope",
})
LEGACY_KEYS = frozenset({
    "signature", "signature_b64", "signature_algorithm", "signer_public_key_sha256",
    "owner_replay_receipt", "replay_receipt", "push_lease", "lease_signature", "nonce",
})


def _hex(value: object, length: int, label: str) -> str:
    text = str(value or "")
    allowed = HEX40 if length == 40 else HEX64
    if len(text) != length or any(char not in allowed for char in text):
        raise ValueError(f"{label} is invalid")
    return text


def _outside(path: Path | None, repo: Path, label: str) -> Path:
    if path is None:
        raise ValueError(f"{label} is missing")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} is missing")
    try:
        resolved.relative_to(repo.resolve())
    except ValueError:
        return resolved
    raise ValueError(f"{label} must remain outside the repository")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _parse_time(value: object, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def working_tree_inventory(repo: Path) -> str:
    """Hash current tracked and non-ignored files without exposing their contents."""
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "-z", "--", "work/backtest"], capture_output=True, check=True).stdout.split(b"\0")
    untracked = subprocess.run(["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z", "--", "work/backtest"], capture_output=True, check=True).stdout.split(b"\0")
    rows: list[str] = []
    for raw in sorted(set(item for item in [*tracked, *untracked] if item)):
        path = repo / raw.decode("utf-8")
        if not path.is_file():
            continue
        rows.append(f"{raw.decode('utf-8').replace(chr(92), '/')}	{path.stat().st_size}	{_hash(path)}")
    return sha256(("\n".join(rows) + "\n").encode("utf-8")).hexdigest()


def _common(payload: dict[str, Any], *, expected: dict[str, str], label: str, keys: frozenset[str]) -> None:
    if set(payload) != keys or payload.get("schema_version") != 1:
        raise ValueError(f"{label} schema is not closed")
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"{label} binding differs")
    if payload.keys() & LEGACY_KEYS:
        raise ValueError(f"{label} contains legacy signed acceptance fields")


def validate_owner_declaration(path: Path) -> dict[str, Any]:
    payload = _read_json(path, "owner declaration")
    _common(payload, expected={}, label="owner declaration", keys=OWNER_KEYS)
    if payload["statement_type"] != "DEMO_BROKER_ACCOUNT_ROTATION_OWNER_DECLARATION_V1" or payload["status"] != "DECLARED" or payload["declarant_role"] != "OWNER" or payload["purpose"] != "DEMO_REPOSITORY_PUBLICATION" or payload["old_credential_not_used"] is not True or payload["credential_values_shared"] is not False:
        raise ValueError("owner declaration semantic state is invalid")
    if not isinstance(payload["rotation_date"], str) or not __import__("re").fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", payload["rotation_date"]):
        raise ValueError("owner declaration rotation_date is invalid")
    return payload


def validate_sensitive_scan(path: Path, *, expected: dict[str, str]) -> dict[str, Any]:
    payload = _read_json(path, "sensitive scan")
    _common(payload, expected=expected, label="sensitive scan", keys=SCAN_KEYS)
    for key in ("private_denylist_sha256", "scanner_source_sha256", "inventory_sha256"):
        _hex(payload[key], 64, f"sensitive scan {key}")
    if payload["report_type"] != "DEMO_SENSITIVE_SCAN_V1" or payload["status"] != "CLEAN" or any(payload[key] != 0 for key in ("working_tree_match_count", "reachable_history_match_count", "ref_name_match_count", "quarantine_match_count")):
        raise ValueError("sensitive scan is not clean")
    return payload


def validate_history_scan(path: Path, *, expected: dict[str, str]) -> dict[str, Any]:
    payload = _read_json(path, "history scan")
    _common(payload, expected=expected, label="history scan", keys=HISTORY_KEYS)
    for key in ("private_denylist_sha256", "source_ref_inventory_sha256", "sanitized_ref_inventory_sha256", "scanner_source_sha256"):
        _hex(payload[key], 64, f"history scan {key}")
    if payload["report_type"] != "DEMO_HISTORY_SANITIZATION_V1" or payload["status"] != "CLEAN" or payload["reachable_history_match_count"] != 0 or payload["sanitized_mirror_match_count"] != 0 or payload["topology_preserved"] is not True or payload["source_unchanged"] is not True or payload["mirror_external"] is not True:
        raise ValueError("history scan is not verified clean")
    return payload


def validate_push_approval(path: Path, *, expected: dict[str, str]) -> dict[str, Any]:
    payload = _read_json(path, "push approval")
    _common(payload, expected=expected, label="push approval", keys=PUSH_KEYS)
    if payload["approval_type"] != "DEMO_REPOSITORY_PUSH_APPROVAL_V1" or payload["status"] != "APPROVED" or payload["approved"] is not True or payload["approval_scope"] != "DEMO_REPOSITORY_PUBLICATION" or not str(payload["destination_ref"]).startswith("refs/heads/"):
        raise ValueError("push approval semantic state is invalid")
    _parse_time(payload["approved_at_utc"], "push approval approved_at_utc")
    return payload


def evaluate(repo: Path, *, repository_identity: str, owner_declaration: Path | None, history_scan: Path | None, sensitive_scan: Path | None, push_approval: Path | None) -> dict[str, Any]:
    context = current_commit_tree(repo)
    branch = current_branch(repo)
    inventory = working_tree_inventory(repo)
    expected = {"repository_identity": repository_identity, "branch": branch, "source_commit": context["source_commit"], "source_tree_oid": context["source_tree_sha256"]}
    checks: dict[str, Any] = {"owner_declaration": False, "history_scan": False, "sensitive_scan": False, "push_approval": False, "scan_denylist_binding": False, "sensitive_inventory_binding": False}
    errors: list[str] = []
    values: dict[str, str] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for name, path, validator in (("owner_declaration", owner_declaration, validate_owner_declaration), ("history_scan", history_scan, validate_history_scan), ("sensitive_scan", sensitive_scan, validate_sensitive_scan), ("push_approval", push_approval, validate_push_approval)):
        try:
            external = _outside(path, repo, name)
            payload = validator(external) if name == "owner_declaration" else validator(external, expected=expected)
            evidence[name] = payload
            checks[name] = True
            values[f"{name}_sha256"] = _hash(external)
            if name == "push_approval":
                values["destination_ref"] = str(payload["destination_ref"])
        except ValueError as exc:
            errors.append(str(exc))
    if checks["sensitive_scan"] and evidence["sensitive_scan"]["inventory_sha256"] == inventory:
        checks["sensitive_inventory_binding"] = True
    elif checks["sensitive_scan"]:
        errors.append("sensitive scan inventory binding differs")
    if checks["history_scan"] and checks["sensitive_scan"] and evidence["history_scan"]["private_denylist_sha256"] == evidence["sensitive_scan"]["private_denylist_sha256"]:
        checks["scan_denylist_binding"] = True
    elif checks["history_scan"] and checks["sensitive_scan"]:
        errors.append("history and sensitive scan denylist bindings differ")
    ready = all(checks.values())
    return {
        "schema_version": 1,
        "acceptance_type": "DEMO_REPOSITORY_PUBLICATION_V1",
        "status": "DEMO_REPOSITORY_READY" if ready else "BLOCKED_EXTERNAL_ACCEPTANCE",
        "repository_identity": repository_identity,
        "branch": branch,
        "source_commit": context["source_commit"],
        "source_tree_oid": context["source_tree_sha256"],
        "working_tree_inventory_sha256": inventory,
        "owner_declaration_sha256": values.get("owner_declaration_sha256", ""),
        "history_scan_sha256": values.get("history_scan_sha256", ""),
        "sensitive_scan_sha256": values.get("sensitive_scan_sha256", ""),
        "push_approval_sha256": values.get("push_approval_sha256", ""),
        "legacy_signed_acceptance_used": False,
        "live_risk_controls_unchanged": True,
        "manifest_integrity_controls_unchanged": True,
        "checks": checks,
        "missing_or_invalid_inputs": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--repository-identity", required=True)
    parser.add_argument("--owner-declaration", type=Path)
    parser.add_argument("--history-scan", type=Path)
    parser.add_argument("--sensitive-scan", type=Path)
    parser.add_argument("--push-approval", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = evaluate(args.repo.resolve(), repository_identity=args.repository_identity, owner_declaration=args.owner_declaration, history_scan=args.history_scan, sensitive_scan=args.sensitive_scan, push_approval=args.push_approval)
    except Exception as exc:
        payload = {"schema_version": 1, "acceptance_type": "DEMO_REPOSITORY_PUBLICATION_V1", "status": "BLOCKED_EXTERNAL_ACCEPTANCE", "legacy_signed_acceptance_used": False, "live_risk_controls_unchanged": True, "manifest_integrity_controls_unchanged": True, "reason_code": type(exc).__name__}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": payload["status"], "legacy_signed_acceptance_used": False}, sort_keys=True))
    return 0 if payload["status"] == "DEMO_REPOSITORY_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
