"""Create immutable, process-backed Super1 local acceptance evidence."""

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

from super1_evidence_report import REQUIRED, build_report


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
DEFAULT_OUTPUT = WORKSPACE / "outputs" / "super1_readiness_20260831" / "run-008"
OLD_OUTPUT = WORKSPACE / "outputs" / "super1_readiness_20260831" / "run-007"
RUN_ID = "run-008"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    if not root.exists():
        return digest.hexdigest(), count
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(relative)
        digest.update(str(len(data)).encode("ascii"))
        digest.update(data)
        count += 1
    return digest.hexdigest(), count


def write_text_new(path: Path, content: str) -> None:
    if path.exists():
        raise FileExistsError(f"Evidence output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def write_json_new(path: Path, payload: Any) -> None:
    write_text_new(path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def output_manifest(
    output: Path,
    input_hashes: dict[str, str],
    predecessor: dict[str, Any],
    continuation_state: str,
) -> dict[str, Any]:
    files = []
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        if path.name == "output-manifest.json":
            continue
        files.append(
            {
                "path": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "campaign": "Super1",
        "decision": "NO_GO",
        "safe_to_apply": False,
        "apply_allowed": False,
        "proven": False,
        "parity": False,
        "continuation_state": continuation_state,
        "files": files,
        "input_hashes": input_hashes,
        "predecessor": predecessor,
        "manifest_is_last_output": True,
    }


def dependency_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in ("pytest", "pandas", "numpy", "MetaTrader5", "cryptography"):
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
    process = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, check=False)
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


def mutable_input_paths() -> list[Path]:
    relative = [
        "scripts/run_capital_forward.py",
        "scripts/run_xm_mt5_forward.py",
        "scripts/run_super1_xm_mt5_forward.py",
        "scripts/run_forward_shadow.py",
        "scripts/super1_continuation.py",
        "scripts/plan_super1_campaign_continuation.py",
        "scripts/validate_super1_rth_calendar.py",
        "scripts/super1_evidence_report.py",
        "scripts/run_super1_run008_evidence.py",
        "scripts/run_super1_run009_evidence.py",
        "scripts/run_super1_rollback_harness.ps1",
        "deploy/rollover_super1_campaign_windows.ps1",
        "deploy/super1_rollover_failure.ps1",
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
    return [ROOT / item for item in relative]


def hash_inputs(paths: list[Path]) -> dict[str, str]:
    return {str(path.resolve()): sha256(path) for path in paths if path.is_file()}


def build_acceptance_matrix(
    report: dict[str, Any],
    processes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    process_ok = all(item["exit_code"] == 0 for item in processes.values())
    entries = list(report["entries"].values())
    continuation_state = (
        "DESIGN_TESTED"
        if all(report["entries"][code]["actual"] == "PASS" for code in ("M01", "M02", "M03"))
        else "INCOMPLETE"
    )
    required_total = sum(int(item["measured_testcase_count"]) + len(item["missing_node_ids"]) for item in entries)
    observed_total = sum(int(item["measured_testcase_count"]) for item in entries)
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "decision": "NO_GO",
        "proven": False,
        "parity": False,
        "software_local_acceptance": "PASS" if process_ok and report["all_required_nodes_observed"] else "FAIL",
        "required_node_total": required_total,
        "observed_required_node_total": observed_total,
        "missing_required_node_total": required_total - observed_total,
        "continuation_state": continuation_state,
        "entries": entries,
        "process_exit_codes": {name: item["exit_code"] for name, item in processes.items()},
        "not_applicable_order_fields": report["not_applicable_order_fields"],
    }


def main() -> int:
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUTPUT
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"{RUN_ID} must be new and empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    if not OLD_OUTPUT.is_dir():
        raise SystemExit(f"immutable predecessor run is missing: {OLD_OUTPUT}")
    old_before, old_count_before = tree_hash(OLD_OUTPUT)
    inputs = mutable_input_paths()
    before_hashes = hash_inputs(inputs)
    before_r_cutoff_bytes = {
        key: before_hashes[str((ROOT / path).resolve())]
        for key, path in {
            "R01": "scripts/run_xm_mt5_forward.py",
            "R02": "scripts/run_xm_mt5_forward.py",
            "R03": "scripts/run_capital_forward.py",
        }.items()
    }

    python = str(Path(sys.executable).resolve())
    processes: dict[str, dict[str, Any]] = {}
    processes["targeted_pytest"] = run_process(
        "targeted_pytest",
        [
            python,
            "-m",
            "pytest",
            "-q",
            "tests/test_xm_mt5_forward.py",
            "tests/test_capital_forward.py",
            "tests/test_super1_xm_forward.py",
            "tests/test_super1_calendar.py",
            "tests/test_super1_continuation.py",
            "tests/test_super1_rollback_harness.py",
            f"--junitxml={output / 'targeted-pytest.xml'}",
        ],
        output,
        stdout_name="targeted-pytest.stdout.txt",
        stderr_name="targeted-pytest.stderr.txt",
    )
    processes["full_pytest"] = run_process(
        "full_pytest",
        [python, "-m", "pytest", "-q", f"--junitxml={output / 'full-pytest.xml'}"],
        output,
        stdout_name="full-pytest.stdout.txt",
        stderr_name="full-pytest.stderr.txt",
    )
    processes["compileall"] = run_process(
        "compileall",
        [python, "-m", "compileall", "-q", "scripts", "tests", "backtest"],
        output,
        stdout_name="compileall.stdout.txt",
        stderr_name="compileall.stderr.txt",
    )
    processes["calendar_validator"] = run_process(
        "calendar_validator",
        [
            python,
            "scripts/validate_super1_rth_calendar.py",
            "--output",
            str(output / "calendar-validation.json"),
        ],
        output,
        stdout_name="calendar-validator.stdout.txt",
        stderr_name="calendar-validator.stderr.txt",
    )
    processes["continuation_planner"] = run_process(
        "continuation_planner",
        [
            python,
            "scripts/plan_super1_campaign_continuation.py",
            "--output",
            str(output / "continuation-dry-run.json"),
        ],
        output,
        stdout_name="continuation-planner.stdout.txt",
        stderr_name="continuation-planner.stderr.txt",
    )
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        raise SystemExit(f"PowerShell is required for {RUN_ID} rollback evidence.")
    rollback_root = output / "rollback-fixture"
    processes["rollback_harness"] = run_process(
        "rollback_harness",
        [
            str(Path(powershell).resolve()),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts/run_super1_rollback_harness.ps1"),
            "-OutputRoot",
            str(rollback_root),
            "-Failure",
            "all",
        ],
        output,
        stdout_name="rollback-harness.stdout.txt",
        stderr_name="rollback-harness.stderr.txt",
    )
    processes["junit_evidence_report"] = run_process(
        "junit_evidence_report",
        [
            python,
            "scripts/super1_evidence_report.py",
            "--junit",
            str(output / "targeted-pytest.xml"),
            str(output / "full-pytest.xml"),
            "--output",
            str(output / "evidence-report.json"),
        ],
        output,
        stdout_name="evidence-report.stdout.txt",
        stderr_name="evidence-report.stderr.txt",
    )

    report = json.loads((output / "evidence-report.json").read_text(encoding="utf-8"))
    continuation_state = (
        "DESIGN_TESTED"
        if all(report["entries"][code]["actual"] == "PASS" for code in ("M01", "M02", "M03"))
        else "INCOMPLETE"
    )
    after_hashes = hash_inputs(inputs)
    after_r_cutoff_bytes = {
        key: after_hashes[str((ROOT / path).resolve())]
        for key, path in {
            "R01": "scripts/run_xm_mt5_forward.py",
            "R02": "scripts/run_xm_mt5_forward.py",
            "R03": "scripts/run_capital_forward.py",
        }.items()
    }
    old_after, old_count_after = tree_hash(OLD_OUTPUT)
    write_json_new(
        output / "process-metadata.json",
        {
            "schema_version": 2,
            "run_id": RUN_ID,
            "generated_at_utc": now_utc(),
            "cwd": str(ROOT.resolve()),
            "git_head": git_output("rev-parse", "HEAD"),
            "processes": processes,
            "tested_input_hashes_before": before_hashes,
            "tested_input_hashes_after": after_hashes,
            "before_after_input_hashes_equal": before_hashes == after_hashes,
            "r01_r02_r03_before_failure_after_success_bytes": {
                "before": before_r_cutoff_bytes,
                "after": after_r_cutoff_bytes,
                "equal": before_r_cutoff_bytes == after_r_cutoff_bytes,
            },
            "old_run_007_immutable": {
                "path": str(OLD_OUTPUT.resolve()),
                "before_tree_sha256": old_before,
                "after_tree_sha256": old_after,
                "before_file_count": old_count_before,
                "after_file_count": old_count_after,
                "equal": old_before == old_after and old_count_before == old_count_after,
            },
            "scope": {
                "source_state_read": False,
                "target_state_read": False,
                "ssh_used": False,
                "mt5_or_broker_called": False,
                "orders_sent": False,
                "deployment_performed": False,
                "release_or_transfer_performed": False,
                "run_007_modified": False,
            },
        },
    )
    write_text_new(output / "working-tree.diff", git_output("diff", "--binary", "--", ".") + "\n")
    write_text_new(output / "git-status.txt", git_output("status", "--short") + "\n")
    write_text_new(output / "untracked-files.txt", git_output("ls-files", "--others", "--exclude-standard") + "\n")
    write_json_new(output / "acceptance-matrix.json", build_acceptance_matrix(report, processes))
    write_json_new(
        output / "readiness-report.json",
        {
            "run_id": RUN_ID,
            "campaign": "Super1",
            "decision": "NO_GO",
            "proven": False,
            "parity": False,
            "software_local_acceptance": "PASS" if report["all_required_nodes_observed"] else "FAIL",
            "continuation_state": continuation_state,
            "required_node_inventory": report["entries"],
            "runtime_state": "DEMO_FRESH_FORWARD_UNPROVEN",
            "deployment_state": "NO_DEPLOYMENT",
            "reason": "Local engineering evidence can validate fail-closed behavior but cannot prove fresh-forward profitability or historical parity.",
        },
    )
    write_json_new(
        output / "continuation-contract-proposal.json",
            {
                "run_id": RUN_ID,
            "campaign": "CURRENT_SUPER1_CAMPAIGN_CONTINUATION",
            "status": "DESIGN_ONLY",
            "apply_allowed": False,
            "source_snapshot_status": "SOURCE_SNAPSHOT_NOT_VERIFIED",
            "release_status": "NOT_VERIFIED",
            "proven": False,
            "parity": False,
            "allowed_change_list": [
                "continuation transition record only",
                "new release/runtime/harness verification after source snapshot",
                "RTH calendar binding and local validation evidence",
            ],
        },
    )
    write_json_new(
        output / "output-manifest.json",
        output_manifest(
            output,
            after_hashes,
            {
                "run_id": "run-008" if RUN_ID == "run-009" else "run-007",
                "path": str(OLD_OUTPUT.resolve()),
                "tree_sha256": old_after,
                "file_count": old_count_after,
            },
            continuation_state,
        ),
    )
    print(json.dumps({"run_id": RUN_ID, "output": str(output), "processes": processes}, indent=2, default=str))
    return 0 if all(item["exit_code"] == 0 for item in processes.values()) and report["all_required_nodes_observed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
