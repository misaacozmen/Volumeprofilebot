"""Closed owner trust-policy validation with an independently provisioned root."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import base64
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


POLICY_KEYS = frozenset({
    "schema_version", "policy_id", "issuer", "purpose", "repository_identity", "branch",
    "inventory_sha256", "allowed_source_commit", "allowed_source_tree_oid", "leaf_public_key_sha256",
    "valid_from_utc", "valid_until_utc", "revocation_state", "nonce", "signature_algorithm",
    "root_public_key_sha256", "signature_b64",
})
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TRUST_ANCHOR_ENV = "SUPER1_OWNER_TRUST_ANCHOR_PATH"
TRUST_ANCHOR_KEYS = frozenset({"schema_version", "store_type", "provisioned", "root_public_key_sha256", "provisioned_at_utc"})
FINAL_MANIFEST_PURPOSE = "SUPER1_FINAL_MANIFEST"


def canonical_policy_bytes(policy: Mapping[str, Any]) -> bytes:
    unsigned = {key: value for key, value in policy.items() if key != "signature_b64"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _provisioned_root_fingerprint(*, repository_root: Path, expected: str) -> str:
    """Read an owner-provisioned OS/signed-release trust anchor, never a CLI hash."""
    raw_path = os.environ.get(TRUST_ANCHOR_ENV, "").strip()
    if not raw_path:
        raise ValueError(f"{TRUST_ANCHOR_ENV} is not provisioned")
    anchor_path = Path(raw_path).resolve()
    if not anchor_path.is_file():
        raise ValueError("owner trust anchor is missing")
    try:
        anchor_path.relative_to(repository_root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("owner trust anchor must remain outside the repository")
    try:
        anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("owner trust anchor is unreadable") from exc
    if not isinstance(anchor, dict) or set(anchor) != TRUST_ANCHOR_KEYS or anchor.get("schema_version") != 1 or anchor.get("store_type") not in {"OS_TRUST_STORE", "SIGNED_RELEASE_PIN"} or anchor.get("provisioned") is not True:
        raise ValueError("owner trust anchor schema is not closed or not provisioned")
    fingerprint = str(anchor.get("root_public_key_sha256") or "")
    if not HEX64.fullmatch(fingerprint) or fingerprint != expected:
        raise ValueError("owner trust anchor root fingerprint differs")
    return fingerprint


def _parse_time(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"owner policy {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"owner policy {label} must be UTC")
    return parsed.astimezone(timezone.utc)


def validate_policy(
    policy_path: str | Path,
    *,
    trusted_root_public_key_path: str | Path,
    trusted_root_public_key_sha256: str,
    repository_identity: str,
    branch: str,
    inventory_sha256: str,
    source_commit: str,
    source_tree_oid: str,
    leaf_public_key_sha256: str,
    expected_purpose: str = FINAL_MANIFEST_PURPOSE,
    expected_nonces: tuple[str, ...] = (),
    repository_root: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy_file = Path(policy_path).resolve()
    root_file = Path(trusted_root_public_key_path).resolve()
    if not policy_file.is_file() or not root_file.is_file():
        raise ValueError("external owner trust policy or trusted root is missing")
    if not HEX64.fullmatch(str(trusted_root_public_key_sha256)):
        raise ValueError("trusted root public-key fingerprint is invalid")
    if _hash(root_file) != str(trusted_root_public_key_sha256):
        raise ValueError("trusted root public-key fingerprint differs")
    _provisioned_root_fingerprint(
        repository_root=Path(repository_root).resolve() if repository_root is not None else policy_file.parent,
        expected=str(trusted_root_public_key_sha256),
    )
    try:
        policy = json.loads(policy_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("owner trust policy is unreadable") from exc
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS or policy.get("schema_version") != 1:
        raise ValueError("owner trust policy schema is not closed")
    for name in ("policy_id", "issuer", "purpose", "repository_identity", "branch", "valid_from_utc", "valid_until_utc", "revocation_state", "nonce", "signature_algorithm", "signature_b64"):
        if not isinstance(policy.get(name), str) or not policy[name].strip():
            raise ValueError(f"owner trust policy field {name} is invalid")
    for name in ("inventory_sha256", "leaf_public_key_sha256", "root_public_key_sha256"):
        if not HEX64.fullmatch(str(policy.get(name) or "")):
            raise ValueError(f"owner trust policy field {name} is invalid")
    for name in ("allowed_source_commit", "allowed_source_tree_oid"):
        if not HEX40.fullmatch(str(policy.get(name) or "")):
            raise ValueError(f"owner trust policy field {name} is invalid")
    if policy["repository_identity"] != str(repository_identity) or policy["branch"] != str(branch):
        raise ValueError("owner trust policy repository or branch differs")
    if policy["purpose"] != str(expected_purpose):
        raise ValueError("owner trust policy purpose is not bound to the final-manifest operation")
    if expected_nonces and policy["nonce"] not in {str(value) for value in expected_nonces}:
        raise ValueError("owner trust policy nonce is not bound to the final-manifest run")
    if policy["inventory_sha256"] != str(inventory_sha256) or policy["allowed_source_commit"] != str(source_commit) or policy["allowed_source_tree_oid"] != str(source_tree_oid) or policy["leaf_public_key_sha256"] != str(leaf_public_key_sha256):
        raise ValueError("owner trust policy source or leaf binding differs")
    if policy["root_public_key_sha256"] != str(trusted_root_public_key_sha256) or policy["revocation_state"] != "ACTIVE" or policy["signature_algorithm"] != "RSA-PSS-SHA256":
        raise ValueError("owner trust policy is revoked or uses an invalid root binding")
    start = _parse_time(policy["valid_from_utc"], "valid_from_utc")
    end = _parse_time(policy["valid_until_utc"], "valid_until_utc")
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if end <= start or not (start <= observed < end):
        raise ValueError("owner trust policy is outside its validity interval")
    if not HEX64.fullmatch(policy["nonce"]):
        raise ValueError("owner trust policy nonce is invalid")
    try:
        signature = base64.b64decode(policy["signature_b64"], validate=True)
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        root_key = serialization.load_pem_public_key(root_file.read_bytes())
        root_key.verify(
            signature,
            canonical_policy_bytes(policy),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise ValueError("owner trust policy root signature verification failed") from exc
    return {"policy_id": policy["policy_id"], "issuer": policy["issuer"], "repository_identity": policy["repository_identity"], "branch": policy["branch"], "nonce": policy["nonce"], "leaf_public_key_sha256": policy["leaf_public_key_sha256"]}
