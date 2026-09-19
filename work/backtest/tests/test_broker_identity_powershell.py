from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess


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


def test_recovery_worker_revalidates_in_a_clean_powershell_child(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        import pytest

        pytest.skip("PowerShell is required for the worker subprocess regression test.")

    source = text("recover_super1_isolated_user.ps1")
    match = re.search(r"\$WorkerSource = @'\r?\n(?P<body>.*?)\r?\n'@", source, re.DOTALL)
    assert match, "recovery worker template is missing"

    root = tmp_path / "Super1"
    helper_path = root / "app" / "deploy" / "broker_identity_env.ps1"
    helper_path.parent.mkdir(parents=True)
    helper_path.write_text(text("broker_identity_env.ps1"), encoding="utf-8")

    worker_template = match.group("body").replace("C:\\Super1", str(root))
    worker_template = worker_template.split("$Password =", 1)[0]
    assert match.group("body").index("Get-BrokerIdentityLogin") < match.group("body").index("$Password =")
    worker_path = tmp_path / "worker.ps1"

    def run_worker(env_value: str | None, discovered_login: int, password: str = "fixture-password") -> subprocess.CompletedProcess[str]:
        worker_path.write_text(
            worker_template
            + f'\n$Discovery = [pscustomobject]@{{ login = {discovered_login} }}\n'
            + 'if ([long]$Discovery.login -ne [long]$ExpectedLogin) { throw "Unexpected isolated XM identity." }\n'
            + '"WORKER_IDENTITY_OK"\n',
            encoding="utf-8",
        )
        child_env = os.environ.copy()
        if env_value is None:
            child_env.pop("XM_MT5_ACCOUNT_LOGIN", None)
        else:
            child_env["XM_MT5_ACCOUNT_LOGIN"] = env_value
        return subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(worker_path)],
            input=(password + "\n") if password else "",
            text=True,
            capture_output=True,
            env=child_env,
            timeout=30,
        )

    valid = run_worker("42424242", 42424242)
    assert valid.returncode == 0, valid.stderr
    assert "WORKER_IDENTITY_OK" in valid.stdout

    mismatch = run_worker("42424242", 42424243)
    assert mismatch.returncode != 0
    assert "Unexpected isolated XM identity" in mismatch.stderr

    missing = run_worker(None, 42424242, password="")
    assert missing.returncode != 0
    assert "XM_MT5_ACCOUNT_LOGIN is required" in missing.stderr

    for invalid in ("", "0", "-1", "+1", "１２３", "9223372036854775808"):
        rejected = run_worker(invalid, 42424242)
        assert rejected.returncode != 0, invalid
        assert "XM_MT5_ACCOUNT_LOGIN" in rejected.stderr
