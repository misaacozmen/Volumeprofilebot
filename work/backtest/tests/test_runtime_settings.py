from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backtest.live.settings import SettingsError, load_settings


def test_settings_are_typed_mode_aware_and_secret_safe(tmp_path: Path) -> None:
    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"fake terminal")
    terminal_path = str(terminal.resolve())
    settings = load_settings(
        {"account_mode": "DEMO_ORDER", "terminal_path": terminal_path, "expected_server": "server", "terminal_sha256": hashlib.sha256(b"fake terminal").hexdigest(), "execution": "MT5_DEMO_ORDERS"},
        environment={
            "XM_MT5_TERMINAL_PATH": terminal_path,
            "XM_MT5_SERVER": "server",
            "XM_MT5_PASSWORD": "secret",
            "SUPER1_INVOCATION_NONCE": "4c4f2b60-80c8-46d0-9df1-3f8b6ddfcb22",
            "SUPER1_RUNNER_SID": "S-1-5-21-123-456-789-1001",
            "SUPER1_LAUNCHER_SHA256": "a" * 64,
            "SUPER1_INVOCATION_STARTED_AT": datetime.now(timezone.utc).isoformat(),
            "UNUSED": "ignored",
        },
    )
    assert "secret" not in repr(settings)
    assert "secret" not in str(settings.audit_payload())
    assert settings.terminal_path.endswith("terminal64.exe")


def test_demo_order_missing_required_settings_fails_closed() -> None:
    with pytest.raises(SettingsError):
        load_settings({"account_mode": "DEMO_ORDER", "execution": "MT5_DEMO_ORDERS"}, environment={})


def test_protected_invocation_metadata_is_validated() -> None:
    with pytest.raises(SettingsError, match="UUID"):
        load_settings(
            {"account_mode": "DEMO_READ_ONLY"},
            environment={"SUPER1_INVOCATION_NONCE": "not-a-uuid"},
            enforce_required=False,
        )
    with pytest.raises(SettingsError, match="timezone"):
        load_settings(
            {"account_mode": "DEMO_READ_ONLY"},
            environment={"SUPER1_INVOCATION_STARTED_AT": "2026-09-08T08:00:00"},
            enforce_required=False,
        )
