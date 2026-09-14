"""Verification of the private, detached-signed broker deployment binding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class DeploymentBindingError(RuntimeError):
    pass


BINDING_SCHEMA_VERSION = 1
BINDING_FIELDS = frozenset(
    {"schema_version", "binding_id", "nonce", "account_login", "server", "company", "issued_at_utc"}
)
_OPAQUE_128 = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class VerifiedDeploymentBinding:
    binding: dict[str, Any]
    binding_sha256: str
    signature_sha256: str

    @property
    def account_key(self) -> str:
        return self.binding_sha256

    @property
    def binding_id(self) -> str:
        return str(self.binding["binding_id"])

    def lease_fields(self) -> dict[str, str]:
        return {
            "account_binding_sha256": self.binding_sha256,
            "account_binding_signature_sha256": self.signature_sha256,
            "binding_id": self.binding_id,
        }

    def assert_broker_account(self, account: object) -> None:
        expected = self.binding
        if (
            int(getattr(account, "login", -1)) != int(expected["account_login"])
            or str(getattr(account, "server", "")) != str(expected["server"])
            or str(getattr(account, "company", "")) != str(expected["company"])
        ):
            raise DeploymentBindingError("broker account does not equal the signed private binding")


def load_verified_deployment_binding(
    binding_path: str | Path,
    signature_path: str | Path,
    public_key_path: str | Path,
) -> VerifiedDeploymentBinding:
    binding_path = Path(binding_path)
    signature_path = Path(signature_path)
    public_key_path = Path(public_key_path)
    for path in (binding_path, signature_path, public_key_path):
        if not path.is_file() or path.is_symlink():
            raise DeploymentBindingError("deployment binding input is missing or reparse-backed")
    raw = binding_path.read_bytes()
    signature = signature_path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentBindingError("deployment binding JSON is unreadable") from exc
    if not isinstance(value, dict) or set(value) != BINDING_FIELDS:
        raise DeploymentBindingError("deployment binding schema is not exact")
    if value.get("schema_version") != BINDING_SCHEMA_VERSION:
        raise DeploymentBindingError("deployment binding schema version is unsupported")
    if not _OPAQUE_128.fullmatch(str(value.get("binding_id") or "")) or not _OPAQUE_128.fullmatch(str(value.get("nonce") or "")):
        raise DeploymentBindingError("deployment binding requires opaque random 128-bit identifiers")
    if not isinstance(value.get("account_login"), int) or int(value["account_login"]) <= 0:
        raise DeploymentBindingError("deployment binding account login is invalid")
    if not str(value.get("server") or "") or not str(value.get("company") or ""):
        raise DeploymentBindingError("deployment binding account identity is incomplete")
    try:
        issued = datetime.fromisoformat(str(value["issued_at_utc"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeploymentBindingError("deployment binding issue time is invalid") from exc
    if issued.tzinfo is None:
        raise DeploymentBindingError("deployment binding issue time must be timezone-aware")
    try:
        key = serialization.load_pem_public_key(public_key_path.read_bytes())
        if not isinstance(key, rsa.RSAPublicKey):
            raise DeploymentBindingError("deployment binding trust root must be RSA")
        key.verify(signature, raw, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise DeploymentBindingError("deployment binding signature is invalid") from exc
    return VerifiedDeploymentBinding(value, sha256(raw).hexdigest(), sha256(signature).hexdigest())
