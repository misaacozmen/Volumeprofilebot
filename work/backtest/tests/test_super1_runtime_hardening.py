from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.super1_runtime_guard as guard


ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict[str, object]:
    return json.loads((ROOT / "live_forward/super1_xm_mt5_demo_config.json").read_text(encoding="utf-8"))


def test_super1_manifest_is_local_manual_demo_only() -> None:
    manifest = json.loads((ROOT / "research_candidates/super1/super1_manifest.json").read_text(encoding="utf-8"))
    deployment = manifest["deployment"]
    assert deployment == {
        "target": "LOCAL_WINDOWS_PC",
        "campaign_days": 30,
        "isolation_required": True,
        "demo_order_execution_enabled": True,
        "real_money_live_enabled": False,
        "real_money_execution_allowed": False,
        "existing_campaign_must_remain_untouched": True,
        "daily_manual_start_required": True,
        "unattended_execution_allowed": False,
    }


def test_lease_binds_identity_and_order_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    now = guard.utc_now()
    control = tmp_path / "control"
    control.mkdir()
    lease = {
        "schema_version": 1,
        "lease_id": "6c66d8b4-6b03-4d4c-a2dd-9ac4e7ff5c26",
        "campaign_id": "campaign-1",
        "trade_date_ny": "2026-09-05",
        "issued_at_utc": (now - timedelta(minutes=1)).isoformat(),
        "not_before_utc": (now - timedelta(minutes=1)).isoformat(),
        "order_not_before_utc": (now - timedelta(seconds=1)).isoformat(),
        "expires_at_utc": (now + timedelta(minutes=20)).isoformat(),
        "release_id": "release-1",
        "app_manifest_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "candidate_sha256": "c" * 64,
        "harness_sha256": "d" * 64,
        "runner_sid": guard._current_user_sid() if guard.os.name == "nt" else "",
        "machine_binding": guard.machine_binding(),
        "expected_account_login": config["account_login"],
        "expected_server": config["expected_server"],
        "expected_company": config["expected_company"],
        "magic_number": config["magic_number"],
        "mode": "DEMO_ORDER",
        "state": "ACTIVE",
        "revoked_at_utc": None,
    }
    (control / "session-lease.json").write_text(json.dumps(lease), encoding="utf-8")
    loaded = guard.load_lease(tmp_path, config, "a" * 64)
    assert loaded["lease_id"] == lease["lease_id"]
    lease["expected_server"] = "wrong"
    (control / "session-lease.json").write_text(json.dumps(lease), encoding="utf-8")
    with pytest.raises(guard.AccountBindingMismatchError):
        guard.load_lease(tmp_path, config, "a" * 64)


def test_account_binding_mismatch_is_no_send() -> None:
    class FakeMt5:
        ACCOUNT_TRADE_MODE_DEMO = 0

        def account_info(self):
            return SimpleNamespace(
                login=1,
                server="wrong",
                company="wrong",
                trade_mode=0,
                trade_allowed=True,
                trade_expert=True,
            )

        def terminal_info(self):
            return SimpleNamespace(connected=True, trade_allowed=True, tradeapi_disabled=False)

    with pytest.raises(guard.AccountBindingMismatchError) as error:
        guard.assert_account_binding(FakeMt5(), _config())
    assert error.value.code == "ACCOUNT_BINDING_MISMATCH_NO_SEND"
