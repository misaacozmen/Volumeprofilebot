"""Produce process-backed run-007 evidence without touching broker or deployment state."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
DEFAULT_OUTPUT = WORKSPACE / "outputs" / "super1_readiness_20260831" / "run-007"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_text_new(path: Path, content: str) -> None:
    if path.exists():
        raise FileExistsError(f"Evidence output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json_new(path: Path, payload: Any) -> None:
    write_text_new(path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def dependency_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in ("pytest", "pandas", "numpy", "MetaTrader5"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )
    return result.stdout.strip()


def run_process(
    name: str,
    argv: list[str],
    output: Path,
    *,
    stdout_name: str,
    stderr_name: str,
) -> dict[str, Any]:
    started = now_utc()
    process = subprocess.run(
        argv,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    ended = now_utc()
    stdout_path = output / stdout_name
    stderr_path = output / stderr_name
    write_text_new(stdout_path, process.stdout)
    write_text_new(stderr_path, process.stderr)
    return {
        "name": name,
        "interpreter": str(Path(argv[0]).resolve()),
        "python_version": sys.version if Path(argv[0]).resolve() == Path(sys.executable).resolve() else None,
        "argv": argv,
        "cwd": str(ROOT.resolve()),
        "utc_start": started,
        "utc_end": ended,
        "exit_code": process.returncode,
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "dependency_versions": dependency_versions(),
    }


def tested_input_hashes() -> dict[str, str]:
    relative_paths = [
        "scripts/run_capital_forward.py",
        "scripts/run_xm_mt5_forward.py",
        "scripts/run_super1_xm_mt5_forward.py",
        "scripts/run_forward_shadow.py",
        "scripts/super1_continuation.py",
        "scripts/plan_super1_campaign_continuation.py",
        "scripts/validate_super1_rth_calendar.py",
        "scripts/run_super1_rollback_harness.ps1",
        "tests/test_capital_forward.py",
        "tests/test_xm_mt5_forward.py",
        "tests/test_super1_xm_forward.py",
        "tests/test_super1_calendar.py",
        "tests/test_super1_continuation.py",
        "tests/test_super1_rollback_harness.py",
        "live_forward/super1_xm_mt5_demo_config.json",
        "research_candidates/super1/super1_signal_contract.json",
        "research_candidates/super1/super1_manifest.json",
        "live_forward/calendars/us_equity_rth_2026.json",
        "live_forward/calendars/provenance/nasdaq-trading-calendar-2026.html",
        "live_forward/calendars/provenance/nyse-2026-yearly-trading-calendar.pdf",
        "live_forward/calendars/provenance/nasdaq-2026-extracted.json",
        "live_forward/calendars/provenance/nyse-2026-extracted.json",
    ]
    return {str((ROOT / item).resolve()): sha256(ROOT / item) for item in relative_paths}


def matrix_row(code: str, expected: str, node_ids: list[str], passing: bool, persistent: str) -> dict[str, Any]:
    return {
        "code": code,
        "node_ids": node_ids,
        "expected": expected,
        "actual": "PASS" if passing else "FAIL",
        "new_entry": "NOT_APPLICABLE",
        "remove": "NOT_APPLICABLE",
        "persistent_state": persistent,
    }


def build_acceptance_matrix(processes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    passing = all(processes[name]["exit_code"] == 0 for name in ("targeted_pytest", "full_pytest", "compileall", "calendar_validator", "rollback_harness", "continuation_planner"))
    return {
        "run_id": "run-007",
        "decision": "NO_GO",
        "software_local_acceptance": "PASS" if passing else "FAIL",
        "entries": [
            matrix_row("E01", "ticket-scoped entry deal and position-scoped lifecycle; order keyword rejected", [
                "tests/test_xm_mt5_forward.py::test_history_deal_lookup_rejects_undocumented_order_keyword",
                "tests/test_xm_mt5_forward.py::test_documented_mt5_order_deal_position_chain_for_long_short_and_exit",
            ], passing, "PERSISTENT_ORDER_LEDGER_AND_DEAL_CHAIN_TESTED"),
            matrix_row("E02", "volume_initial is identity; current remainder balances partial/full fill", [
                "tests/test_xm_mt5_forward.py::test_partial_fill_is_not_full_fill_and_does_not_resend",
                "tests/test_xm_mt5_forward.py::test_documented_mt5_order_deal_position_chain_for_long_short_and_exit",
            ], passing, "VOLUME_CHAIN_TESTED"),
            matrix_row("E03", "same order-position chain only; exposure preserved and unsafe protection blocks", [
                "tests/test_xm_mt5_forward.py::test_pending_remove_keeps_order_specific_partial_exposure_without_fatal",
                "tests/test_xm_mt5_forward.py::test_pending_remove_does_not_bind_unrelated_same_magic_position",
                "tests/test_xm_mt5_forward.py::test_pending_remove_blocks_unprotected_order_specific_position",
            ], passing, "EXPOSURE_PRESERVED_FAIL_CLOSED"),
            matrix_row("E04", "UNKNOWN/PARTIAL blocks later candidate and persists across restart", [
                "tests/test_xm_mt5_forward.py::test_unknown_or_partial_candidate_blocks_later_candidate_in_same_cycle[none]",
                "tests/test_xm_mt5_forward.py::test_unknown_or_partial_candidate_blocks_later_candidate_in_same_cycle[timeout]",
                "tests/test_xm_mt5_forward.py::test_unknown_or_partial_candidate_blocks_later_candidate_in_same_cycle[connection]",
                "tests/test_xm_mt5_forward.py::test_unknown_or_partial_candidate_blocks_later_candidate_in_same_cycle[partial]",
            ], passing, "UNKNOWN_PARTIAL_PERSISTED"),
            matrix_row("T01", "common market_data_asof separated from actual provider observation", [
                "tests/test_capital_forward.py::test_fetch_window_preserves_provider_observation_after_requested_asof",
            ], passing, "COMMON_FETCH_SCOPE_RECORDED"),
            matrix_row("T02", "finalization waits for shared fetched scope and emits no marker", [
                "tests/test_capital_forward.py::test_finalize_waits_for_complete_shared_fetch_scope_without_marker",
            ], passing, "WAIT_FOR_FINAL_DATA_NO_MARKER"),
            matrix_row("C01", "independent Nasdaq/NYSE extraction records agree with runtime calendar; tamper fails", [
                "tests/test_super1_calendar.py::test_independent_calendar_extractions_match_each_other_and_runtime",
                "tests/test_super1_calendar.py::test_calendar_extraction_mutation_fails_cross_source_and_runtime_checks",
            ], passing, "OFFICIAL_RAW_SHA_AND_SOURCE_LOCATIONS_BOUND"),
            matrix_row("C02", "real Super1 overlay path and separate BLOCK/ALLOW/UNRESOLVED feature cases", [
                "tests/test_super1_xm_forward.py::test_super1_real_overlay_risk_request_reaches_controlled_broker_boundary",
                "tests/test_super1_xm_forward.py::test_super1_filter_blocks_only_frozen_rules_and_allows_valid_candidate",
                "tests/test_super1_xm_forward.py::test_overnight_direction_rejects_missing_previous_rth_close",
                "tests/test_super1_calendar.py::test_runtime_calendar_tamper_is_blocked_before_super1_feature_use",
            ], passing, "SUPER1_FILTER_AND_SDK_BOUNDARY_TESTED"),
            matrix_row("M01", "source timestamps/state unresolved; local output is exclusive and non-overwriting", [
                "tests/test_super1_continuation.py::test_exclusive_writer_rejects_existing_and_source_contained_outputs",
                "tests/test_super1_xm_forward.py::test_continuation_design_is_non_applying_and_snapshot_gated",
            ], passing, "SOURCE_SNAPSHOT_UNRESOLVED"),
            matrix_row("M02", "actual root/snapshot/member/transition bytes and signatures required", [
                "tests/test_super1_continuation.py::test_verified_signature_status_alone_is_not_accepted",
                "tests/test_super1_continuation.py::test_actual_root_snapshot_members_transition_and_test_signatures_are_verified",
            ], passing, "SYNTHETIC_TEST_ONLY_CRYPTO_APPLY_FALSE"),
            matrix_row("M03", "real inventory paths, SQLite/WAL fingerprint, positive/negative continuation fixtures", [
                "tests/test_super1_continuation.py::test_inventory_uses_real_runtime_paths_and_sqlite_wal_backup_readback",
                "tests/test_super1_continuation.py::test_continuation_fixture_covers_required_positive_and_negative_cases",
            ], passing, "FIXTURE_BLOCKERS_TESTED"),
            matrix_row("O01", "rollover failure latches daemon-consumed recovery; no restart or unproven STOPPED", [
                "tests/test_super1_rollback_harness.py::test_power_shell_rollback_harness_latches_all_injected_failures",
            ], passing, "PERSISTENT_FATAL_AND_RECOVERY_LATCH"),
        ],
    }


def main() -> int:
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUTPUT
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"run-007 must be new and empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    processes: dict[str, dict[str, Any]] = {}
    python = str(Path(sys.executable).resolve())
    targeted = [
        "-m", "pytest", "-q",
        "tests/test_xm_mt5_forward.py",
        "tests/test_capital_forward.py",
        "tests/test_super1_xm_forward.py",
        "tests/test_super1_calendar.py",
        "tests/test_super1_continuation.py",
        "tests/test_super1_rollback_harness.py",
        f"--junitxml={output / 'targeted-pytest.xml'}",
    ]
    processes["targeted_pytest"] = run_process(
        "targeted_pytest", [python, *targeted], output,
        stdout_name="targeted-pytest.stdout.txt", stderr_name="targeted-pytest.stderr.txt"
    )
    processes["full_pytest"] = run_process(
        "full_pytest", [python, "-m", "pytest", "-q", f"--junitxml={output / 'full-pytest.xml'}"], output,
        stdout_name="full-pytest.stdout.txt", stderr_name="full-pytest.stderr.txt"
    )
    processes["compileall"] = run_process(
        "compileall", [python, "-m", "compileall", "-q", "scripts", "tests", "backtest"], output,
        stdout_name="compileall.stdout.txt", stderr_name="compileall.stderr.txt"
    )
    processes["calendar_validator"] = run_process(
        "calendar_validator", [python, "scripts/validate_super1_rth_calendar.py", "--output", str(output / "calendar-validation.json")], output,
        stdout_name="calendar-validator.stdout.txt", stderr_name="calendar-validator.stderr.txt"
    )
    processes["continuation_planner"] = run_process(
        "continuation_planner", [python, "scripts/plan_super1_campaign_continuation.py", "--output", str(output / "continuation-dry-run.json")], output,
        stdout_name="continuation-planner.stdout.txt", stderr_name="continuation-planner.stderr.txt"
    )
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        raise SystemExit("PowerShell is required for run-007 rollback evidence.")
    rollback_root = output / "rollback-fixture"
    processes["rollback_harness"] = run_process(
        "rollback_harness",
        [str(Path(powershell).resolve()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/run_super1_rollback_harness.ps1"), "-OutputRoot", str(rollback_root), "-Failure", "all"],
        output,
        stdout_name="rollback-harness.stdout.txt", stderr_name="rollback-harness.stderr.txt"
    )
    rollback_stdout = (output / "rollback-harness.stdout.txt").read_text(encoding="utf-8")
    write_text_new(output / "rollback-harness-result.json", rollback_stdout)
    write_text_new(output / "working-tree.diff", git_output("diff", "--binary", "--", ".") + "\n")
    write_text_new(output / "git-status.txt", git_output("status", "--short") + "\n")
    write_text_new(output / "untracked-files.txt", git_output("ls-files", "--others", "--exclude-standard") + "\n")
    metadata = {
        "run_id": "run-007",
        "generated_at_utc": now_utc(),
        "cwd": str(ROOT.resolve()),
        "git_head": git_output("rev-parse", "HEAD"),
        "git_diff_path": str((output / "working-tree.diff").resolve()),
        "processes": processes,
        "tested_input_hashes": tested_input_hashes(),
        "scope": {
            "source_state_read": False,
            "target_state_read": False,
            "ssh_used": False,
            "mt5_or_broker_called": False,
            "orders_sent": False,
            "deployment_performed": False,
        },
    }
    write_json_new(output / "process-metadata.json", metadata)
    write_json_new(output / "acceptance-matrix.json", build_acceptance_matrix(processes))
    print(json.dumps({"run_id": "run-007", "output": str(output), "processes": processes}, indent=2, default=str))
    return 0 if all(item["exit_code"] == 0 for item in processes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
