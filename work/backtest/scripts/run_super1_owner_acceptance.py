"""Single parameterized owner acceptance runner; never creates secrets or orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import base64
import json
from pathlib import Path
import re
import subprocess
from typing import Any

try:
    from scripts.git_provenance import current_branch, validate_production_source_binding
    from scripts.mt5_read_only_acceptance import run_pinned_read_only_acceptance
    from scripts.owner_replay_ledger import validate as validate_replay_ledger, validate_receipt
    from scripts.owner_trust import validate_policy
    from scripts.scan_public_broker_identity import _deny_values, _history_scan, _quarantine_scan, _ref_name_matches, scan
except ModuleNotFoundError:
    from git_provenance import current_branch, validate_production_source_binding
    from mt5_read_only_acceptance import run_pinned_read_only_acceptance
    from owner_replay_ledger import validate as validate_replay_ledger, validate_receipt
    from owner_trust import validate_policy
    from scan_public_broker_identity import _deny_values, _history_scan, _quarantine_scan, _ref_name_matches, scan


HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
EVIDENCE_KEYS = frozenset({
    "schema_version", "status", "run_id", "source_commit", "source_tree_oid", "repository_identity", "branch",
    "inventory_sha256", "checks", "mt5_evidence_sha256", "rotation_evidence_sha256", "binding_signature_sha256",
    "created_at_utc",
})


def _external(path: Path, repo: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} is missing")
    try:
        resolved.relative_to(repo.resolve())
    except ValueError:
        return resolved
    raise ValueError(f"{label} must remain outside the repository")


def _external_dir(path: Path, repo: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError(f"{label} is missing")
    try:
        resolved.relative_to(repo.resolve())
    except ValueError:
        return resolved
    raise ValueError(f"{label} must remain outside the repository")


def _canonical_without_signature(value: dict[str, Any]) -> bytes:
    return json.dumps({key: item for key, item in value.items() if key != "signature_b64"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _verify_signed_document(path: Path, *, required: set[str], document_type: str, signer_key_path: Path, signer_key_sha256: str, expected: dict[str, str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("rotation evidence is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema_version") != 1 or payload.get("signature_algorithm") != "RSA-PSS-SHA256" or payload.get("signer_public_key_sha256") != signer_key_sha256:
        raise ValueError("rotation evidence closed schema or binding is invalid")
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"{document_type} binding differs")
    if sha256(signer_key_path.read_bytes()).hexdigest() != signer_key_sha256:
        raise ValueError(f"{document_type} signer key fingerprint differs")
    try:
        signature = base64.b64decode(payload["signature_b64"], validate=True)
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        serialization.load_pem_public_key(signer_key_path.read_bytes()).verify(signature, _canonical_without_signature(payload), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    except Exception as exc:
        raise ValueError(f"{document_type} signature verification failed") from exc
    return payload


def _verify_rotation(path: Path, *, signer_key_path: Path, signer_key_sha256: str, run_id: str, source_commit: str, source_tree_oid: str, nonce: str, old_binding_sha256: str, new_binding_sha256: str, identity_hmac_sha256: str) -> None:
    required = {"schema_version", "status", "run_id", "old_binding_id_sha256", "new_binding_id_sha256", "old_binding_revoked", "new_binding_active", "account_trade_mode", "account_identity_hmac_sha256", "provider_issuer", "semantic_state", "effective_at_utc", "nonce", "source_commit", "source_tree_oid", "signature_algorithm", "signature_b64", "signer_public_key_sha256"}
    payload = _verify_signed_document(path, required=required, document_type="rotation evidence", signer_key_path=signer_key_path, signer_key_sha256=signer_key_sha256, expected={"run_id": run_id, "nonce": nonce, "source_commit": source_commit, "source_tree_oid": source_tree_oid, "old_binding_id_sha256": old_binding_sha256, "new_binding_id_sha256": new_binding_sha256, "account_identity_hmac_sha256": identity_hmac_sha256})
    if payload["status"] != "SIGNED" or payload["old_binding_revoked"] is not True or payload["new_binding_active"] is not True or payload["account_trade_mode"] != "DEMO" or payload["semantic_state"] != "ROTATED_DEMO" or not str(payload["provider_issuer"]).strip() or old_binding_sha256 == new_binding_sha256 or not HEX64.fullmatch(old_binding_sha256) or not HEX64.fullmatch(new_binding_sha256) or not HEX64.fullmatch(identity_hmac_sha256):
        raise ValueError("rotation evidence semantic state or opaque binding is invalid")
    try:
        effective = datetime.fromisoformat(str(payload["effective_at_utc"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("rotation effective time is invalid") from exc
    if effective.tzinfo is None or effective.astimezone(timezone.utc) > datetime.now(timezone.utc):
        raise ValueError("rotation effective time is invalid")


def _verify_binding_signature(path: Path, *, signer_key_path: Path, signer_key_sha256: str, run_id: str, source_commit: str, source_tree_oid: str, nonce: str, old_binding_sha256: str, new_binding_sha256: str, identity_hmac_sha256: str) -> None:
    required = {"schema_version", "signature_type", "run_id", "nonce", "source_commit", "source_tree_oid", "old_binding_id_sha256", "new_binding_id_sha256", "account_identity_hmac_sha256", "semantic_state", "signature_algorithm", "signature_b64", "signer_public_key_sha256"}
    payload = _verify_signed_document(path, required=required, document_type="binding signature", signer_key_path=signer_key_path, signer_key_sha256=signer_key_sha256, expected={"run_id": run_id, "nonce": nonce, "source_commit": source_commit, "source_tree_oid": source_tree_oid, "old_binding_id_sha256": old_binding_sha256, "new_binding_id_sha256": new_binding_sha256, "account_identity_hmac_sha256": identity_hmac_sha256})
    if payload["signature_type"] != "MT5_BINDING_SIGNATURE_V1" or payload["semantic_state"] != "ROTATED_DEMO":
        raise ValueError("binding signature semantic state is invalid")


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise ValueError("owner acceptance repository is invalid")
    for path, label in ((args.owner_trust_policy, "owner trust policy"), (args.owner_replay_ledger, "owner replay ledger"), (args.owner_replay_receipt, "owner replay receipt"), (args.trusted_root_public_key, "trusted root public key"), (args.pinned_public_key, "pinned public key"), (args.binding_signature, "binding signature"), (args.owner_hmac_key, "owner HMAC key"), (args.rotation_evidence, "rotation evidence"), (args.denylist, "private denylist")):
        _external(path, repo, label)
    sanitized_mirror = _external_dir(args.sanitized_mirror, repo, "sanitized bare mirror")
    _external_dir(args.quarantine_object_dir, repo, "quarantine object directory")
    _external_dir(args.evidence_root, repo, "owner evidence root")
    if not HEX40.fullmatch(args.source_commit) or not HEX40.fullmatch(args.source_tree_oid) or not HEX64.fullmatch(args.run_id) or not HEX64.fullmatch(args.nonce) or not HEX64.fullmatch(args.inventory_sha256):
        raise ValueError("owner acceptance identity is incomplete")
    validate_production_source_binding(repo, args.source_commit, args.source_tree_oid)
    if current_branch(repo) != args.branch:
        raise ValueError("owner branch context differs from the checked-out Git branch")
    leaf_hash = sha256(args.pinned_public_key.read_bytes()).hexdigest()
    if leaf_hash != args.pinned_public_key_sha256 or not HEX64.fullmatch(args.pinned_public_key_sha256):
        raise ValueError("pinned public-key fingerprint differs")
    validate_policy(args.owner_trust_policy, trusted_root_public_key_path=args.trusted_root_public_key, trusted_root_public_key_sha256=args.trusted_root_public_key_sha256, repository_identity=args.repository_identity, branch=args.branch, inventory_sha256=args.inventory_sha256, source_commit=args.source_commit, source_tree_oid=args.source_tree_oid, leaf_public_key_sha256=leaf_hash, expected_purpose="SUPER1_FINAL_MANIFEST", expected_nonces=(args.nonce, args.nonce_b), repository_root=repo)
    validate_replay_ledger(args.owner_replay_ledger, run_id=args.run_id, nonces=(args.nonce, args.nonce_b), purpose="SUPER1_FINAL_MANIFEST", require_consumed=True)
    validate_receipt(args.owner_replay_receipt, signer_public_key_path=args.pinned_public_key, signer_public_key_sha256=leaf_hash, run_id=args.run_id, nonces=(args.nonce, args.nonce_b), source_commit=args.source_commit, source_tree_oid=args.source_tree_oid, inventory_sha256=args.inventory_sha256)
    matches = scan(repo, args.denylist)
    denied, _ = _deny_values(args.denylist)
    history_matches, counts = _history_scan(repo, denied)
    ref_matches = _ref_name_matches(repo, denied)
    quarantine_count = _quarantine_scan(args.quarantine_object_dir, denied, repository_root=repo)
    mirror_bare = subprocess.run(["git", "--git-dir", str(sanitized_mirror), "rev-parse", "--is-bare-repository"], capture_output=True, text=True, check=False)
    if mirror_bare.returncode != 0 or mirror_bare.stdout.strip().lower() != "true":
        raise ValueError("sanitized mirror is not a separately verified bare repository")
    mirror_history, mirror_counts = _history_scan(sanitized_mirror, denied)
    mirror_refs = _ref_name_matches(sanitized_mirror, denied)
    mt5_path = args.evidence_root.resolve() / "mt5_read_only_acceptance.json"
    mt5 = run_pinned_read_only_acceptance(owner_hmac_key_path=args.owner_hmac_key, binding_id=args.binding_id, run_id=args.run_id, source_commit=args.source_commit, source_tree_oid=args.source_tree_oid, nonce=args.nonce)
    if mt5.get("run_id") != args.run_id or mt5.get("source_commit") != args.source_commit or mt5.get("source_tree_oid") != args.source_tree_oid or mt5.get("nonce") != args.nonce or mt5.get("binding_id_sha256") != sha256(args.binding_id.encode("utf-8")).hexdigest():
        raise ValueError("MT5 evidence opaque binding differs")
    mt5_path.parent.mkdir(parents=True, exist_ok=True)
    mt5_path.write_bytes(json.dumps(mt5, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")
    old_binding_sha256 = args.old_binding_id_sha256
    new_binding_sha256 = args.new_binding_id_sha256
    if not HEX64.fullmatch(old_binding_sha256) or not HEX64.fullmatch(new_binding_sha256) or new_binding_sha256 != mt5.get("binding_id_sha256"):
        raise ValueError("old/new opaque binding hashes are invalid or inconsistent")
    identity_hmac_sha256 = str(mt5.get("identity_hmac_sha256") or "")
    if not HEX64.fullmatch(identity_hmac_sha256):
        raise ValueError("MT5 account binding HMAC is missing")
    _verify_rotation(args.rotation_evidence, signer_key_path=args.pinned_public_key, signer_key_sha256=leaf_hash, run_id=args.run_id, source_commit=args.source_commit, source_tree_oid=args.source_tree_oid, nonce=args.nonce, old_binding_sha256=old_binding_sha256, new_binding_sha256=new_binding_sha256, identity_hmac_sha256=identity_hmac_sha256)
    _verify_binding_signature(args.binding_signature, signer_key_path=args.pinned_public_key, signer_key_sha256=leaf_hash, run_id=args.run_id, source_commit=args.source_commit, source_tree_oid=args.source_tree_oid, nonce=args.nonce, old_binding_sha256=old_binding_sha256, new_binding_sha256=new_binding_sha256, identity_hmac_sha256=identity_hmac_sha256)
    checks = {
        "git_commit_tree": True,
        "owner_policy": True,
        "owner_replay_ledger": True,
        "denylist_worktree": len(matches),
        "denylist_history": len(history_matches),
        "denylist_ref_names": len(ref_matches),
        "denylist_quarantine": quarantine_count,
        "ignored_untracked_count": sum(1 for row in matches if row.get("scope") == "ignored"),
        "local_pre_push_status": "CLEAN" if not matches and not history_matches and not ref_matches and quarantine_count == 0 else "DIRTY",
        "sanitized_bare_mirror": True,
        "remote_post_push_status": "CLEAN" if not mirror_history and not mirror_refs else "DIRTY",
        "sanitized_mirror_history": len(mirror_history),
        "sanitized_mirror_ref_names": len(mirror_refs),
        "mt5_status": mt5.get("status"),
        "mt5_order_send": mt5.get("order_send"),
        "mt5_write_operations": mt5.get("write_operations"),
        "rotation": True,
    }
    clean = not matches and not history_matches and not ref_matches and quarantine_count == 0 and not mirror_history and not mirror_refs and mt5.get("status") == "PASS_EXTERNAL" and mt5.get("order_send") == 0 and mt5.get("write_operations") == 0 and mt5.get("shutdown_called") is True
    payload = {
        "schema_version": 1,
        "status": "PASS_EXTERNAL" if clean else "BLOCKED_EXTERNAL_ACCEPTANCE",
        "run_id": args.run_id,
        "source_commit": args.source_commit,
        "source_tree_oid": args.source_tree_oid,
        "repository_identity": args.repository_identity,
        "branch": args.branch,
        "inventory_sha256": args.inventory_sha256,
        "checks": checks,
        "mt5_evidence_sha256": sha256(mt5_path.read_bytes()).hexdigest(),
        "rotation_evidence_sha256": sha256(args.rotation_evidence.read_bytes()).hexdigest(),
        "binding_signature_sha256": sha256(args.binding_signature.read_bytes()).hexdigest(),
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--owner-trust-policy", type=Path, required=True)
    parser.add_argument("--owner-replay-ledger", type=Path, required=True)
    parser.add_argument("--trusted-root-public-key", type=Path, required=True)
    parser.add_argument("--trusted-root-public-key-sha256", required=True)
    parser.add_argument("--pinned-public-key", type=Path, required=True)
    parser.add_argument("--pinned-public-key-sha256", required=True)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--binding-signature", type=Path, required=True)
    parser.add_argument("--owner-hmac-key", type=Path, required=True)
    parser.add_argument("--owner-replay-receipt", type=Path, required=True)
    parser.add_argument("--rotation-evidence", type=Path, required=True)
    parser.add_argument("--denylist", type=Path, required=True)
    parser.add_argument("--quarantine-object-dir", type=Path, required=True)
    parser.add_argument("--sanitized-mirror", type=Path, required=True)
    parser.add_argument("--old-binding-id-sha256", required=True)
    parser.add_argument("--new-binding-id-sha256", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--nonce-b", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree-oid", required=True)
    parser.add_argument("--inventory-sha256", required=True)
    parser.add_argument("--repository-identity", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args()
    try:
        payload = run(args)
        args.evidence_root.resolve().mkdir(parents=True, exist_ok=True)
        (args.evidence_root.resolve() / "owner_acceptance.json").write_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")
        print(json.dumps({"status": payload["status"], "denylist_match_count": sum(value for key, value in payload["checks"].items() if key.startswith("denylist_"))}, sort_keys=True))
        raise SystemExit(0 if payload["status"] == "PASS_EXTERNAL" else 2)
    except Exception as exc:
        blocked = {"schema_version": 1, "status": "BLOCKED_EXTERNAL_ACCEPTANCE", "reason_code": type(exc).__name__}
        args.evidence_root.resolve().mkdir(parents=True, exist_ok=True)
        (args.evidence_root.resolve() / "owner_acceptance.json").write_bytes(json.dumps(blocked, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")
        print(json.dumps({"status": blocked["status"], "reason_code": blocked["reason_code"]}, sort_keys=True))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
