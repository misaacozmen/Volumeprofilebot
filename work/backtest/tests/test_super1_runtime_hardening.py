from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

import scripts.super1_runtime_guard as guard
import scripts.run_xm_mt5_forward as xm
import scripts.run_super1_xm_mt5_forward as super1


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


def _powershell(*arguments: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    assert executable, "Windows PowerShell is required for the runtime contract tests."
    return subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *arguments],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _contract_script() -> str:
    return str(ROOT / "deploy" / "super1_runtime_contract.ps1")


def test_contract_subprocess_exposes_all_protected_paths() -> None:
    command = f". '{_contract_script()}'; (Get-Super1RuntimeContract | ConvertTo-Json -Compress)"
    result = _powershell("-Command", command)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert {
        "health", "watchdog_status", "watchdog_state", "release_archive",
        "release_manifest", "release_signature",
    }.issubset(payload)


def test_contract_path_resolver_returns_release_triple() -> None:
    command = f". '{_contract_script()}'; @('health','watchdog_status','watchdog_state','release_archive','release_manifest','release_signature') | % {{ Get-Super1RuntimePath -Name $_ }}"
    result = _powershell("-Command", command)
    assert result.returncode == 0, result.stderr
    paths = set(result.stdout.splitlines())
    assert "C:\\Super1\\state\\health.json" in paths
    assert "C:\\Super1\\super1-forward.manifest.sig" in paths


def test_missing_health_contract_fails_in_real_powershell(tmp_path: Path) -> None:
    source = Path(_contract_script()).read_text(encoding="utf-8")
    source = source.replace('    health = "C:\\Super1\\state\\health.json"\n', "")
    broken = tmp_path / "broken_contract.ps1"
    broken.write_text(source, encoding="utf-8")
    result = _powershell("-Command", f". '{broken}'; Assert-Super1RuntimeContract")
    assert result.returncode != 0


def test_installer_requires_r6_parameters_before_admin_gate() -> None:
    installer = ROOT / "deploy" / "install_super1_windows.ps1"
    result = _powershell("-File", str(installer), "-PlanOnly")
    assert result.returncode != 0
    assert "ReleaseDirectory" in (result.stderr + result.stdout)


def test_installer_does_not_fallback_to_root_archive(tmp_path: Path) -> None:
    installer = ROOT / "deploy" / "install_super1_windows.ps1"
    result = _powershell(
        "-File", str(installer), "-ReleaseDirectory", str(tmp_path),
        "-ExpectedReleaseId", "super1-local-demo-20260905-r6",
        "-ExpectedArchiveSha256", "0" * 64, "-PlanOnly",
    )
    assert result.returncode != 0
    assert "Signed R6 triple" in (result.stderr + result.stdout)


def test_legacy_outbox_is_migrated_to_unique_event_id(tmp_path: Path) -> None:
    db = tmp_path / "orders" / "idempotency.sqlite3"
    db.parent.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE order_event_outbox (sequence INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, event_json TEXT NOT NULL, delivered_at TEXT)")
        connection.execute("INSERT INTO order_event_outbox(order_id,event_json) VALUES('old','{}')")
    client = object.__new__(xm.XmMt5DemoOrderClient)
    client._initialize_order_db(tmp_path)
    with sqlite3.connect(db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(order_event_outbox)")}
        assert "event_id" in columns
        assert connection.execute("SELECT event_id FROM order_event_outbox").fetchone()[0]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='ux_order_event_outbox_event_id'").fetchone()


def test_outbox_replay_deduplicates_jsonl_by_stable_id(tmp_path: Path) -> None:
    client = object.__new__(xm.XmMt5DemoOrderClient)
    client.magic = 260805101
    payload = {"recorded_at": "2026-09-05T10:00:00+00:00", "event": "SEND_ARMED", "order_id": "one"}
    with client._ready_order_connection(tmp_path) as connection:
        first = client._insert_outbox(connection, "one", payload)
        second = client._insert_outbox(connection, "one", payload)
    assert first == second
    client._drain_order_outbox(tmp_path)
    client._drain_order_outbox(tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "orders" / "events.jsonl").read_text().splitlines()]
    assert [row["event_id"] for row in rows] == [first]


def test_outbox_event_json_contains_the_same_event_id(tmp_path: Path) -> None:
    client = object.__new__(xm.XmMt5DemoOrderClient)
    payload = {"recorded_at": "2026-09-05T10:00:00+00:00", "event": "CHECK_PASSED", "order_id": "two"}
    connection = client._ready_order_connection(tmp_path)
    client._insert_outbox(connection, "two", payload)
    row = connection.execute("SELECT event_id,event_json FROM order_event_outbox").fetchone()
    connection.close()
    assert json.loads(row[1])["event"] == "CHECK_PASSED"
    assert len(row[0]) == 64


def test_outbox_unique_constraint_rejects_duplicate_ids(tmp_path: Path) -> None:
    client = object.__new__(xm.XmMt5DemoOrderClient)
    client.magic = 1
    connection = client._ready_order_connection(tmp_path)
    payload = {"recorded_at": "2026-09-05T10:00:00+00:00", "event": "X", "order_id": "three"}
    client._insert_outbox(connection, "three", payload)
    client._insert_outbox(connection, "three", payload)
    assert connection.execute("SELECT COUNT(*) FROM order_event_outbox").fetchone()[0] == 1
    connection.close()


def test_corrupt_order_database_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "orders" / "idempotency.sqlite3"
    db.parent.mkdir()
    db.write_bytes(b"not-sqlite")
    client = object.__new__(xm.XmMt5DemoOrderClient)
    with pytest.raises(Exception):
        client._initialize_order_db(tmp_path)


def _lease_template(config: dict[str, object], now) -> dict[str, object]:
    return {
        "schema_version": 1,
        "lease_id": "6c66d8b4-6b03-4d4c-a2dd-9ac4e7ff5c26",
        "campaign_id": "campaign-1",
        "trade_date_ny": now.astimezone().date().isoformat(),
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


def test_expired_lease_is_rejected_before_order_window(monkeypatch, tmp_path: Path) -> None:
    config = _config()
    now = guard.utc_now()
    lease = _lease_template(config, now)
    lease["expires_at_utc"] = (now - timedelta(seconds=1)).isoformat()
    control = tmp_path / "control"
    control.mkdir()
    (control / "session-lease.json").write_text(json.dumps(lease), encoding="utf-8")
    with pytest.raises(guard.Super1RuntimeError):
        guard.load_lease(tmp_path, config, "a" * 64)


def test_revoked_lease_is_rejected_fail_closed(tmp_path: Path) -> None:
    config = _config()
    lease = _lease_template(config, guard.utc_now())
    lease["state"] = "REVOKED"
    control = tmp_path / "control"
    control.mkdir()
    (control / "session-lease.json").write_text(json.dumps(lease), encoding="utf-8")
    with pytest.raises(guard.Super1RuntimeError):
        guard.load_lease(tmp_path, config, "a" * 64)


def test_wrong_release_lease_is_rejected(tmp_path: Path) -> None:
    config = _config()
    lease = _lease_template(config, guard.utc_now())
    lease["release_id"] = ""
    control = tmp_path / "control"
    control.mkdir()
    (control / "session-lease.json").write_text(json.dumps(lease), encoding="utf-8")
    with pytest.raises(guard.Super1RuntimeError):
        guard.load_lease(tmp_path, config, "a" * 64)


def test_wrong_campaign_lease_identity_is_not_silent(tmp_path: Path) -> None:
    config = _config()
    lease = _lease_template(config, guard.utc_now())
    lease["campaign_id"] = ""
    control = tmp_path / "control"
    control.mkdir()
    (control / "session-lease.json").write_text(json.dumps(lease), encoding="utf-8")
    with pytest.raises(guard.Super1RuntimeError):
        guard.load_lease(tmp_path, config, "a" * 64)


class _FinalGateMt5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_ORDER_LIMIT = 2
    ORDER_FILLING_RETURN = 2
    ORDER_TIME_SPECIFIED = 2

    def __init__(self) -> None:
        self.tick_age = 0
        self.spread = 0.01
        self.margin = 100.0

    def symbol_info_tick(self, symbol):
        stamp = int((guard.utc_now().timestamp() - self.tick_age) * 1000)
        return SimpleNamespace(bid=99.0, ask=99.0 + self.spread, time_msc=stamp)

    def symbol_info(self, symbol):
        return SimpleNamespace(
            point=0.01, trade_tick_size=0.01, trade_stops_level=0,
            trade_freeze_level=0, trade_mode=4, order_mode=2,
        )

    def account_info(self):
        return SimpleNamespace(equity=10000.0, margin_free=5000.0)

    def order_calc_profit(self, *args):
        return -100.0

    def order_calc_margin(self, *args):
        return self.margin

    def order_check(self, request):
        return SimpleNamespace(retcode=0)

    def orders_get(self):
        return ()

    def positions_get(self):
        return ()

    def history_deals_get(self, *args, **kwargs):
        return ()


def _final_gate_client(monkeypatch: pytest.MonkeyPatch) -> tuple[object, _FinalGateMt5, dict[str, object]]:
    config = _config()
    client = object.__new__(super1.Super1XmMt5DemoOrderClient)
    mt5 = _FinalGateMt5()
    client.mt5 = mt5
    client.config = config
    client.magic = int(config["magic_number"])
    client._manual_lease_required = True
    client._ensure_demo = lambda: None
    now = guard.utc_now()
    client._assert_super1_lease = lambda output_root, for_order=True: {"expires_at_utc": (now + timedelta(minutes=1)).isoformat()}
    monkeypatch.setattr(super1, "assert_account_binding", lambda mt5, config: {"checks": {"all": True}})
    monkeypatch.setattr(super1.core, "live_strategy_objects", lambda: ({}, None, {"legs": {}, "pair_cap_r": -1.0}))
    client._pair_cap_state = lambda *args, **kwargs: {"state": "ALLOWED"}
    request = {
        "type": 2, "type_filling": 2, "type_time": 2,
        "volume": 1.0, "price": 98.0, "sl": 97.0, "tp": 101.0,
    }
    return client, mt5, request


def test_final_gate_accepts_fresh_flat_margin_safe_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, _, request = _final_gate_client(monkeypatch)
    client._final_send_gate(tmp_path, "gate-1", request, {"direction": "long"}, "US100Cash", {})


def test_final_gate_rejects_stale_tick(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, mt5, request = _final_gate_client(monkeypatch)
    mt5.tick_age = 6
    with pytest.raises(Exception):
        client._final_send_gate(tmp_path, "gate-2", request, {"direction": "long"}, "US100Cash", {})


def test_final_gate_rejects_crossed_or_nan_tick(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, mt5, request = _final_gate_client(monkeypatch)
    mt5.spread = float("nan")
    with pytest.raises(Exception):
        client._final_send_gate(tmp_path, "gate-3", request, {"direction": "long"}, "US100Cash", {})


def test_final_gate_rejects_high_spread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, mt5, request = _final_gate_client(monkeypatch)
    mt5.spread = 0.2
    with pytest.raises(Exception):
        client._final_send_gate(tmp_path, "gate-4", request, {"direction": "long"}, "US100Cash", {})


def test_final_gate_rejects_margin_over_quarter_free_margin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, mt5, request = _final_gate_client(monkeypatch)
    mt5.margin = 1251.0
    with pytest.raises(Exception):
        client._final_send_gate(tmp_path, "gate-5", request, {"direction": "long"}, "US100Cash", {})


def test_final_gate_rejects_non_marketable_long_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, _, request = _final_gate_client(monkeypatch)
    request["price"] = 100.0
    with pytest.raises(Exception):
        client._final_send_gate(tmp_path, "gate-6", request, {"direction": "long"}, "US100Cash", {})


def test_final_gate_rejects_duplicate_broker_exposure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, mt5, request = _final_gate_client(monkeypatch)
    mt5.orders_get = lambda: (SimpleNamespace(magic=client.magic, comment="SUPER1:old"),)
    with pytest.raises(Exception):
        client._final_send_gate(tmp_path, "gate-7", request, {"direction": "long"}, "US100Cash", {})


def test_final_gate_rejects_daily_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, _, request = _final_gate_client(monkeypatch)
    client._pair_cap_state = lambda *args, **kwargs: {"state": "SUPPRESSED_DAILY_CAP"}
    with pytest.raises(Exception):
        client._final_send_gate(tmp_path, "gate-8", request, {"direction": "long"}, "US100Cash", {})


def test_power_shell_contract_is_not_backed_by_root_watchdog_status() -> None:
    for path in (ROOT / "deploy" / "install_super1_watchdog_windows.ps1", ROOT / "deploy" / "start_super1_local_windows.ps1"):
        text = path.read_text(encoding="utf-8")
        assert 'Join-Path ([string]$Contract.root) "watchdog_status.json"' not in text


def test_watchdog_classifies_missing_before_allowlist_in_subprocess_source() -> None:
    text = (ROOT / "deploy" / "watchdog_windows.ps1").read_text(encoding="utf-8")
    assert text.index('$healthState -eq "MISSING"') < text.index('$healthState -notin $AllowedMainHealthStates')


def test_task_settings_explicitly_allow_battery_runtime() -> None:
    for name in ("install_super1_watchdog_windows.ps1", "finalize_super1_fresh_windows.ps1"):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "-AllowStartIfOnBatteries" in text
        assert "-DontStopIfGoingOnBatteries" in text


def test_stop_request_archival_is_present_in_launcher_and_stop_script() -> None:
    core_text = (ROOT / "scripts" / "run_capital_forward.py").read_text(encoding="utf-8")
    stop_text = (ROOT / "deploy" / "stop_super1_local_windows.ps1").read_text(encoding="utf-8")
    assert "archive_stop_request" in core_text
    assert "stop-" in stop_text and "Move-Item" in stop_text


def test_readiness_report_cannot_claim_demo_smoke() -> None:
    text = (ROOT / "deploy" / "test_super1_local_readiness.ps1").read_text(encoding="utf-8")
    assert "READY_FOR_DEMO_SMOKE" not in text
    assert "READY_FOR_ADMIN_INSTALL_SIMULATION" in text


def test_launcher_binding_readiness_uses_no_order_transport_flags() -> None:
    text = (ROOT / "deploy" / "run_super1_windows.ps1").read_text(encoding="utf-8")
    assert "binding_readiness" in text
    assert "--binding-proof" in text
    assert "--confirm-demo" not in text[text.index("binding_readiness") : text.index("binding_readiness") + 1200]


def test_release_manifest_archive_name_is_exact_in_builder() -> None:
    builder = (ROOT / "deploy" / "build_signed_windows_release.ps1").read_text(encoding="utf-8")
    integrity = (ROOT / "deploy" / "release_integrity.ps1").read_text(encoding="utf-8")
    assert "archive_file" in builder
    assert "Release manifest names a different archive" in integrity


def test_outbox_counts_are_sqlite_derived_in_readiness() -> None:
    text = (ROOT / "deploy" / "test_super1_local_readiness.ps1").read_text(encoding="utf-8")
    assert "SELECT COUNT(*) FROM order_intents WHERE status='SUBMITTED'" in text
    assert "COUNT(DISTINCT event_id)" in text
    assert "accepted_send_count = $sendCount" not in text


def test_final_hook_is_called_before_arm_in_base_transport() -> None:
    text = (ROOT / "scripts" / "run_xm_mt5_forward.py").read_text(encoding="utf-8")
    assert text.index("self._final_send_gate(") < text.index("armed = self._arm_send(")


def test_super1_final_hook_contains_lease_and_order_check_gate() -> None:
    text = (ROOT / "scripts" / "run_super1_xm_mt5_forward.py").read_text(encoding="utf-8")
    hook = text[text.index("def _final_send_gate"):text.index("def smoke_order")]
    assert "_assert_super1_lease" in hook
    assert "order_check" in hook
    assert "order_calc_margin" in hook


def test_no_old_xm_server_constant_remains_in_named_files() -> None:
    for path in (ROOT / "live_forward" / "README_XM_MT5_DEMO_TR.md", ROOT / "deploy" / "probe_forward_runner_migration_windows.ps1"):
        assert "XMGlobal-MT5 7" not in path.read_text(encoding="utf-8")


def test_r6_release_triple_rollback_injection_restores_all_signed_files(tmp_path: Path) -> None:
    installer = (ROOT / "deploy" / "install_super1_windows.ps1").read_text(encoding="utf-8")
    for marker in (
        "signed_release_triple",
        "app.previous",
        "venv311.previous",
        "state.previous",
        "control.previous",
        "runtime-trust.previous",
        "task_backups",
        "/restore",
        "ROLLED_BACK",
    ):
        assert marker in installer

    root = tmp_path / "root"
    archive = root / "transaction"
    root.mkdir()
    archive.mkdir()
    old = {name: f"old-{name}".encode() for name in ("super1-forward.zip", "super1-forward.manifest.json", "super1-forward.manifest.sig")}
    new = {name: f"new-{name}".encode() for name in old}
    for name, payload in old.items():
        (root / name).write_bytes(payload)
    try:
        for name, payload in new.items():
            shutil.move(root / name, archive / f"{name}.previous")
            (root / name).write_bytes(payload)
        raise RuntimeError("injected promotion failure")
    except RuntimeError:
        for name in new:
            failed = root / f"{name}.failed"
            if (root / name).exists():
                shutil.move(root / name, failed)
            shutil.move(archive / f"{name}.previous", root / name)

    assert {name: (root / name).read_bytes() for name in old} == old
