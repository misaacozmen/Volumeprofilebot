from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
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
            "invocation_nonce": "7d77e4e9-1fd7-4e77-9d74-8e9bb22dcf7f",
        "campaign_id": "campaign-1",
        "trade_date_ny": guard.trade_date_ny(now),
        "issued_at_utc": (now - timedelta(minutes=1)).isoformat(),
        "not_before_utc": (now - timedelta(minutes=1)).isoformat(),
        "order_not_before_utc": (now - timedelta(seconds=1)).isoformat(),
        "expires_at_utc": (now + timedelta(minutes=20)).isoformat(),
            "release_id": "release-1",
            "release_manifest_sha256": "e" * 64,
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


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc), "2026-01-15"),
        (datetime(2026, 9, 6, 3, 59, tzinfo=timezone.utc), "2026-09-05"),
        (datetime(2026, 9, 6, 4, 1, tzinfo=timezone.utc), "2026-09-06"),
        (datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), "2026-03-08"),
        (datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc), "2026-11-01"),
    ],
)
def test_trade_date_ny_uses_one_injected_utc_clock(
    instant: datetime, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guard, "utc_now", lambda: instant)
    assert guard.trade_date_ny() == expected


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


def test_r8_read_only_lease_cli_classifies_missing_without_runtime_imports(tmp_path: Path) -> None:
    cli = ROOT / "scripts" / "super1_lease_cli.py"
    result = subprocess.run(
        [sys.executable, "-I", "-E", "-B", str(cli), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["state"] == "MISSING"
    assert "MetaTrader5" not in cli.read_text(encoding="utf-8")


def test_r8_readiness_is_a_validated_child_process_and_failure_is_transactional() -> None:
    installer = (ROOT / "deploy" / "install_super1_windows.ps1").read_text(encoding="utf-8")
    readiness = (ROOT / "deploy" / "test_super1_local_readiness.ps1").read_text(encoding="utf-8")
    assert "Start-Process" in installer
    assert "-RedirectStandardOutput" in installer
    assert "-RedirectStandardError" in installer
    assert "READY_FOR_DEMO_SMOKE" in installer
    assert "readinessEvidence.install_transaction_id" in installer
    assert "readinessEvidence.readiness_nonce" in installer
    assert '& (Join-Path $app "deploy\\test_super1_local_readiness.ps1")' not in installer
    assert "Seal-Super1SecureEvidenceTree -Path $transaction" in readiness
    assert "Assert-Super1SecureSealedTree -Path $transaction" in readiness
    assert "$InstallTransactionId" in readiness and "$ReadinessNonce" in readiness


def test_r8_production_order_source_has_no_fake_adapter_or_jsonl_source() -> None:
    super1_source = (ROOT / "scripts" / "run_super1_xm_mt5_forward.py").read_text(encoding="utf-8")
    transport_source = (ROOT / "scripts" / "run_xm_mt5_forward.py").read_text(encoding="utf-8")
    watchdog = (ROOT / "deploy" / "watchdog_windows.ps1").read_text(encoding="utf-8")
    assert "FAKE_ADAPTER" not in super1_source
    assert "append_jsonl" not in transport_source
    assert "SELECT event_json FROM order_event_outbox ORDER BY sequence" in transport_source
    assert "super1_lease_cli.py" in watchdog
    assert "Read-Super1LeaseState" in watchdog
    assert "Global\\Super1OrderTransport" not in watchdog or "order_mutex" in watchdog


def test_r8_runner_can_only_read_immutable_release_manifest_by_install_contract() -> None:
    installer = (ROOT / "deploy" / "install_super1_windows.ps1").read_text(encoding="utf-8")
    assert '"${runnerSid}:(RX)"' in installer
    assert "release_manifest" in installer
    assert "/setowner \"*S-1-5-18\"" in installer
    assert "ReadAndExecute" in (ROOT / "deploy" / "super1_secure_task.ps1").read_text(encoding="utf-8")


def test_contract_member_accesses_are_checked_with_powershell_ast() -> None:
    root_literal = str(ROOT).replace("'", "''")
    command = f"""
$contractPath = '{root_literal}\\deploy\\super1_runtime_contract.ps1'
. $contractPath
$contractKeys = @((Get-Super1RuntimeContract).PSObject.Properties.Name)
$used = @()
foreach ($file in @(Get-ChildItem -LiteralPath '{root_literal}\\deploy' -Filter '*.ps1' -File)) {{
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -ne 0) {{ throw "PowerShell parse failure: $($file.Name)" }}
    $used += @($ast.FindAll({{
        param($node)
        if ($node -isnot [System.Management.Automation.Language.MemberExpressionAst]) {{ return $false }}
        $expression = $node.Expression
        return $expression -is [System.Management.Automation.Language.VariableExpressionAst] -and
            $expression.VariablePath.UserPath -ieq 'Contract'
    }}, $true) | ForEach-Object {{ [string]$_.Member.Value }})
}}
$unknown = @($used | Sort-Object -Unique | Where-Object {{ $_ -notin $contractKeys }})
if ($unknown.Count -gt 0) {{ throw "Unknown Super1 contract members: $($unknown -join ',')" }}
Write-Output (($used | Sort-Object -Unique) -join ',')
"""
    result = _powershell("-Command", command)
    assert result.returncode == 0, result.stderr
    assert "health" in result.stdout and "release_signature" in result.stdout


def test_missing_health_contract_fails_in_real_powershell(tmp_path: Path) -> None:
    source = Path(_contract_script()).read_text(encoding="utf-8")
    source = source.replace('    health = "C:\\Super1\\state\\health.json"\n', "")
    broken = tmp_path / "broken_contract.ps1"
    broken.write_text(source, encoding="utf-8")
    result = _powershell("-Command", f". '{broken}'; Assert-Super1RuntimeContract")
    assert result.returncode != 0


def test_installer_requires_r7_parameters_before_admin_gate() -> None:
    installer = ROOT / "deploy" / "install_super1_windows.ps1"
    result = _powershell("-File", str(installer), "-PlanOnly")
    assert result.returncode != 0
    assert "ReleaseDirectory" in (result.stderr + result.stdout)


def test_installer_does_not_fallback_to_root_archive(tmp_path: Path) -> None:
    installer = ROOT / "deploy" / "install_super1_windows.ps1"
    result = _powershell(
        "-File", str(installer), "-ReleaseDirectory", str(tmp_path),
        "-ExpectedReleaseId", "super1-local-demo-20260907-r8",
        "-ExpectedArchiveSha256", "0" * 64, "-PlanOnly",
    )
    assert result.returncode != 0
    assert "Signed R8 triple" in (result.stderr + result.stdout)


def test_legacy_outbox_is_migrated_to_unique_event_id(tmp_path: Path) -> None:
    db = tmp_path / "orders" / "idempotency.sqlite3"
    db.parent.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE order_event_outbox (sequence INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, event_json TEXT NOT NULL, delivered_at TEXT)")
        connection.execute("INSERT INTO order_event_outbox(order_id,event_json) VALUES('old','{}')")
        connection.execute("INSERT INTO order_event_outbox(order_id,event_json) VALUES('old','{}')")
    client = object.__new__(xm.XmMt5DemoOrderClient)
    client._initialize_order_db(tmp_path)
    with sqlite3.connect(db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(order_event_outbox)")}
        assert "event_id" in columns
        event_ids = [row[0] for row in connection.execute("SELECT event_id FROM order_event_outbox ORDER BY sequence")]
        assert len(event_ids) == 2 and len(set(event_ids)) == 2
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
    assert len(row[0]) == 36
    assert json.loads(row[1])["event_id"] == row[0]


def test_outbox_replace_failure_keeps_committed_event_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = object.__new__(xm.XmMt5DemoOrderClient)
    connection = client._ready_order_connection(tmp_path)
    client._insert_outbox(connection, "crash", {"event": "COMMITTED", "order_id": "crash"})
    connection.commit()
    connection.close()
    original_replace = xm.os.replace

    def fail_replace(source, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(xm.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        client._drain_order_outbox(tmp_path)
    with sqlite3.connect(tmp_path / "orders" / "idempotency.sqlite3") as check:
        assert check.execute("SELECT delivered_at FROM order_event_outbox").fetchone()[0] is None
    monkeypatch.setattr(xm.os, "replace", original_replace)
    client._drain_order_outbox(tmp_path)
    client._drain_order_outbox(tmp_path)
    rows = (tmp_path / "orders" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(row)["event"] for row in rows] == ["COMMITTED"]


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
        "invocation_nonce": "7d77e4e9-1fd7-4e77-9d74-8e9bb22dcf7f",
        "campaign_id": "campaign-1",
        "trade_date_ny": now.astimezone().date().isoformat(),
        "issued_at_utc": (now - timedelta(minutes=1)).isoformat(),
        "not_before_utc": (now - timedelta(minutes=1)).isoformat(),
        "order_not_before_utc": (now - timedelta(seconds=1)).isoformat(),
        "expires_at_utc": (now + timedelta(minutes=20)).isoformat(),
        "release_id": "release-1",
        "release_manifest_sha256": "e" * 64,
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


def test_readiness_report_claims_only_sealed_demo_smoke() -> None:
    text = (ROOT / "deploy" / "test_super1_local_readiness.ps1").read_text(encoding="utf-8")
    assert "READY_FOR_DEMO_SMOKE" in text
    assert "cleanup_sealed" in text


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


def test_r7_production_rollback_restores_leaf_and_container(tmp_path: Path) -> None:
    installer = (ROOT / "deploy" / "install_super1_windows.ps1").read_text(encoding="utf-8")
    helper = (ROOT / "deploy" / "super1_install_transaction.ps1").read_text(encoding="utf-8")
    rollback_text = installer + helper
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
        assert marker in rollback_text
    assert "function Invoke-Super1TransactionRollback" in helper

    root = tmp_path / "root"
    transaction = tmp_path / "transaction"
    leaf = root / "super1-forward.zip"
    app = root / "app"
    leaf_archive = transaction / "super1-forward.zip.previous"
    app_archive = transaction / "app.previous"
    for path in (root, transaction, app):
        path.mkdir()
    leaf.write_text("old-leaf", encoding="utf-8")
    (app / "old.txt").write_text("old-dir", encoding="utf-8")
    leaf_archive.parent.mkdir(parents=True, exist_ok=True)
    leaf.replace(leaf_archive)
    app.replace(app_archive)
    leaf.write_text("new-leaf", encoding="utf-8")
    app.mkdir()
    (app / "new.txt").write_text("new-dir", encoding="utf-8")

    def ps_quote(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    command = f"""
. {ps_quote(ROOT / 'deploy' / 'super1_install_transaction.ps1')}
$errors = [System.Collections.Generic.List[string]]::new()
Invoke-Super1TransactionRollback `
    -RootPath {ps_quote(root)} `
    -TransactionPath {ps_quote(transaction)} `
    -TaskNameList @('NoTaskForRollbackTest') `
    -OldTasksRemoved:$false `
    -NewTargetList @({ps_quote(leaf)}, {ps_quote(app)}) `
    -MovedEntryList @(
        [pscustomobject]@{{source={ps_quote(leaf)}; archive={ps_quote(leaf_archive)}}},
        [pscustomobject]@{{source={ps_quote(app)}; archive={ps_quote(app_archive)}}}
    ) `
    -RunnerWasCreated:$false `
    -Errors $errors `
    -IcaclsPath (Join-Path $env:SystemRoot 'System32\\icacls.exe')
if (@($errors).Count -ne 0) {{ throw (@($errors) -join '; ') }}
if ((Get-Content -Raw -LiteralPath {ps_quote(leaf)}).Trim() -ne 'old-leaf') {{ throw 'leaf was not restored' }}
if (-not (Test-Path -LiteralPath (Join-Path {ps_quote(app)} 'old.txt'))) {{ throw 'container was not restored' }}
if (Test-Path -LiteralPath (Join-Path {ps_quote(app)} 'new.txt')) {{ throw 'new container survived rollback' }}
"""
    result = _powershell("-Command", command)
    assert result.returncode == 0, result.stderr or result.stdout
