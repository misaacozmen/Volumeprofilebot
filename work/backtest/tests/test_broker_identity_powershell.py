from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def text(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def test_powershell_identity_gate_is_strict_and_long_typed() -> None:
    helper = text("broker_identity_env.ps1")
    assert "^[0-9]+$" in helper
    assert "[long]::Parse" in helper
    assert "signed 64-bit" in helper
    for name in (
        "recover_super1_isolated_user.ps1",
        "rollover_forward_shadow_campaign_windows.ps1",
        "rollover_super1_campaign_windows.ps1",
    ):
        source = text(name)
        assert 'broker_identity_env.ps1' in source
        assert "Get-BrokerIdentityLogin" in source
        assert "[long]$ExpectedLogin" in source
        assert "account_login" in source.lower()


def test_recovery_passes_login_explicitly_and_cleans_worker_environment() -> None:
    source = text("recover_super1_isolated_user.ps1")
    assert 'EnvironmentVariables["XM_MT5_ACCOUNT_LOGIN"]' in source
    assert "$env:XM_MT5_ACCOUNT_LOGIN = $null" in source
    assert "ProcessStartInfo" in source
