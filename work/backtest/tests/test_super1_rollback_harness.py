from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from v08_helpers import checkpoint_if_enabled, record_if_enabled


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "run_super1_rollback_harness.ps1"
PRODUCTION = ROOT / "deploy" / "rollover_super1_campaign_windows.ps1"
FAILURE_HELPER = ROOT / "deploy" / "super1_rollover_failure.ps1"


def test_power_shell_rollback_harness_latches_all_injected_failures(tmp_path: Path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        raise AssertionError("PowerShell is required for the rollback harness acceptance test.")
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(HARNESS),
            "-OutputRoot",
            str(tmp_path / "rollback-harness"),
            "-Failure",
            "all",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    rows = json.loads(result.stdout)
    expected_names = {
        "running_no_mutation_safe", "stopped_safe_restore", "preexisting_fatal", "preexisting_recovery",
        "restore_failure", "old_hash_failure", "main_start_failure", "watchdog_start_failure",
        "old_health_failure", "post_start_timeout", "post_watchdog_failure", "stop_failure",
        "evidence_writer_io_failure", "both_latch_writes_fail", "fatal_only_partial", "recovery_only_partial",
    }
    assert {row["case"] for row in rows} == expected_names
    unsafe_rows = [row for row in rows if row["policy_status"] == "UNSAFE_NO_SEND"]
    safe_rows = [row for row in rows if row["policy_status"] == "SAFE_ROLLBACK"]
    assert all(row["persistent_latch"] and row["persistent_recovery"] for row in unsafe_rows)
    assert all(not row["persistent_latch"] and not row["persistent_recovery"] for row in rows if row["case"] == "both_latch_writes_fail")
    assert all(row["process_recreation_latch_consumed"] for row in unsafe_rows)
    assert all(not row["process_recreation_latch_consumed"] for row in safe_rows)
    assert all(row["process_recreation_gate_exit_code"] != 0 for row in unsafe_rows)
    assert all(row["process_recreation_gate_exit_code"] is None for row in safe_rows)
    assert all(not row["automatic_restart"] for row in rows)
    assert all(not row["stopped_proof"] for row in rows)
    assert all(row["state"] != "STOPPED" for row in rows)
    assert all(not row["negative_gate_control_failed"] for row in rows)
    assert not next(row for row in rows if row["case"] == "evidence_writer_io_failure")["failure_evidence_written"]
    assert FAILURE_HELPER.is_file()
    assert ". $FailureHelper" in PRODUCTION.read_text(encoding="utf-8")
    record_if_enabled(request, evidence_token)


def test_o01_harness_uses_same_policy_ast_contract(request) -> None:
    token = checkpoint_if_enabled(request)
    source = HARNESS.read_text(encoding="utf-8")
    contract = json.loads((ROOT / "tests" / "fixtures" / "super1_o01_case_contract.json").read_text(encoding="utf-8"))
    assert "super1_o01_case_contract.json" in source
    assert "Invoke-Super1RolloverCatchPolicy" in source
    assert len(contract["cases"]) == 16
    record_if_enabled(request, token)


def test_o01_production_policy_call_ast_contract(request) -> None:
    token = checkpoint_if_enabled(request)
    source = PRODUCTION.read_text(encoding="utf-8")
    assert source.count("Invoke-Super1RolloverCatchPolicy") == 1
    assert "-BrokerSideEffectPossible ([bool]$brokerSideEffectPossible)" in source
    assert "-TasksStopped ([bool]$tasksStopped)" in source
    assert ". $FailureHelper" in source
    record_if_enabled(request, token)


def test_o01_removed_policy_call_mutation_fails(request) -> None:
    token = checkpoint_if_enabled(request)
    source = PRODUCTION.read_text(encoding="utf-8")
    assert "Invoke-Super1FailureOrchestration" not in source
    assert "Invoke-Super1RolloverCatchPolicy" in source
    assert "Start-ScheduledTask" in source
    record_if_enabled(request, token)


@pytest.mark.parametrize(
    "case",
    [
        "running_no_mutation_safe", "stopped_safe_restore", "preexisting_fatal", "preexisting_recovery",
        "restore_failure", "old_hash_failure", "main_start_failure", "watchdog_start_failure",
        "old_health_failure", "post_start_timeout", "post_watchdog_failure", "stop_failure",
        "evidence_writer_io_failure", "both_latch_writes_fail", "fatal_only_partial", "recovery_only_partial",
    ],
    ids=lambda value: value,
)
def test_o01_rollover_failure_policy_case(case: str, request) -> None:
    token = checkpoint_if_enabled(request)
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    assert powershell is not None
    output_root = request.config._tmp_path_factory.mktemp(f"o01-case-{case}")
    result = subprocess.run(
        [
            powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(HARNESS), "-OutputRoot", str(output_root), "-Failure", case,
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.stdout.strip(), result.stderr
    row = json.loads(result.stdout)
    contract = json.loads((ROOT / "tests" / "fixtures" / "super1_o01_case_contract.json").read_text(encoding="utf-8"))
    expected = next(item for item in contract["cases"] if item["name"] == case)
    assert row["case"] == case
    assert row["policy_status"] == expected["expectedStatus"]
    assert [item["name"] for item in row["callback_trace"]] == expected["expectedTrace"]
    assert all(value == 1 for value in row["callback_counts"].values())
    record_if_enabled(request, token)


@pytest.mark.parametrize("case", ["fatal_only", "recovery_only"], ids=lambda value: value)
def test_o01_startup_gate_rejects_latch(case: str, request) -> None:
    token = checkpoint_if_enabled(request)
    assert case in {"fatal_only", "recovery_only"}
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    assert powershell is not None
    harness_case = {"fatal_only": "fatal_only_partial", "recovery_only": "recovery_only_partial"}[case]
    output_root = request.config._tmp_path_factory.mktemp(f"o01-startup-{case}")
    result = subprocess.run(
        [
            powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(HARNESS), "-OutputRoot", str(output_root), "-Failure", harness_case,
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.stdout.strip(), result.stderr
    row = json.loads(result.stdout)
    assert row["persistent_latch"] and row["persistent_recovery"]
    assert row["process_recreation_gate_exit_code"] != 0
    assert row["negative_gate_disconnect_exit_code"] != 0
    record_if_enabled(request, token)
