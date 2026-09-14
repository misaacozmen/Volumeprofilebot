from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backtest.live.deployment_binding import DeploymentBindingError, load_verified_deployment_binding
from backtest.live.execution import Mt5WritePort


def _fixture(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    raw = (json.dumps({
        "schema_version": 1,
        "binding_id": "01" * 16,
        "nonce": "02" * 16,
        "account_login": 123456,
        "server": "fixture-server",
        "company": "fixture-company",
        "issued_at_utc": datetime.now(timezone.utc).isoformat(),
    }, sort_keys=True) + "\n").encode()
    binding = tmp_path / "account-binding.json"
    signature = tmp_path / "account-binding.sig"
    public = tmp_path / "deploy-public-key.pem"
    binding.write_bytes(raw)
    signature.write_bytes(key.sign(raw, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return binding, signature, public


def test_private_binding_signature_hash_and_account_equality(tmp_path) -> None:
    verified = load_verified_deployment_binding(*_fixture(tmp_path))
    assert verified.account_key == verified.binding_sha256
    assert set(verified.lease_fields()) == {"account_binding_sha256", "account_binding_signature_sha256", "binding_id"}
    verified.assert_broker_account(SimpleNamespace(login=123456, server="fixture-server", company="fixture-company"))


def test_private_binding_tamper_and_account_mismatch_fail_closed(tmp_path) -> None:
    binding, signature, public = _fixture(tmp_path)
    binding.write_bytes(binding.read_bytes().replace(b"fixture-server", b"changed-server"))
    with pytest.raises(DeploymentBindingError, match="signature"):
        load_verified_deployment_binding(binding, signature, public)
    binding, signature, public = _fixture(tmp_path / "second")
    verified = load_verified_deployment_binding(binding, signature, public)
    with pytest.raises(DeploymentBindingError, match="does not equal"):
        verified.assert_broker_account(SimpleNamespace(login=654321, server="fixture-server", company="fixture-company"))


def test_binding_is_reverified_immediately_before_every_physical_write(tmp_path) -> None:
    calls = []
    mt5 = SimpleNamespace(order_send=lambda request: calls.append(request) or "sent")
    blocked = Mt5WritePort(mt5, tmp_path / "orders.db", binding_verifier=lambda: (_ for _ in ()).throw(DeploymentBindingError("blocked")))
    with pytest.raises(DeploymentBindingError, match="blocked"):
        blocked._physical_send({"symbol": "fixture"})
    assert calls == []

    checks = []
    allowed = Mt5WritePort(mt5, tmp_path / "orders.db", binding_verifier=lambda: checks.append("verified"))
    assert allowed._physical_send({"symbol": "fixture"}) == "sent"
    assert checks == ["verified"]
