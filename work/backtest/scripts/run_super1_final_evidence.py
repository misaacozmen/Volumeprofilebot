"""Append final, non-overwriting run-007 evidence after the last fail-closed fix."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

from run_super1_evidence import (
    ROOT,
    build_acceptance_matrix,
    dependency_versions,
    git_output,
    run_process,
    tested_input_hashes,
    write_json_new,
    write_text_new,
)


OUTPUT = ROOT.parents[1] / "outputs" / "super1_readiness_20260831" / "run-007"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    processes = {}
    python = str(Path(sys.executable).resolve())
    processes["targeted_pytest"] = run_process(
        "final_targeted_pytest",
        [python, "-m", "pytest", "-q", "tests/test_xm_mt5_forward.py", "tests/test_capital_forward.py", "tests/test_super1_xm_forward.py", "tests/test_super1_calendar.py", "tests/test_super1_continuation.py", "tests/test_super1_rollback_harness.py", f"--junitxml={OUTPUT / 'final-targeted-pytest.xml'}"],
        OUTPUT,
        stdout_name="final-targeted-pytest.stdout.txt",
        stderr_name="final-targeted-pytest.stderr.txt",
    )
    processes["full_pytest"] = run_process(
        "final_full_pytest",
        [python, "-m", "pytest", "-q", f"--junitxml={OUTPUT / 'final-full-pytest.xml'}"],
        OUTPUT,
        stdout_name="final-full-pytest.stdout.txt",
        stderr_name="final-full-pytest.stderr.txt",
    )
    processes["compileall"] = run_process(
        "final_compileall",
        [python, "-m", "compileall", "-q", "scripts", "tests", "backtest"],
        OUTPUT,
        stdout_name="final-compileall.stdout.txt",
        stderr_name="final-compileall.stderr.txt",
    )
    processes["calendar_validator"] = run_process(
        "final_calendar_validator",
        [python, "scripts/validate_super1_rth_calendar.py", "--output", str(OUTPUT / "final-calendar-validation.json")],
        OUTPUT,
        stdout_name="final-calendar-validator.stdout.txt",
        stderr_name="final-calendar-validator.stderr.txt",
    )
    processes["continuation_planner"] = run_process(
        "final_continuation_planner",
        [python, "scripts/plan_super1_campaign_continuation.py", "--output", str(OUTPUT / "final-continuation-dry-run.json")],
        OUTPUT,
        stdout_name="final-continuation-planner.stdout.txt",
        stderr_name="final-continuation-planner.stderr.txt",
    )
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is None:
        raise SystemExit("PowerShell is required for final run-007 evidence.")
    processes["rollback_harness"] = run_process(
        "final_rollback_harness",
        [str(Path(powershell).resolve()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/run_super1_rollback_harness.ps1"), "-OutputRoot", str(OUTPUT / "final-rollback-fixture"), "-Failure", "all"],
        OUTPUT,
        stdout_name="final-rollback-harness.stdout.txt",
        stderr_name="final-rollback-harness.stderr.txt",
    )
    write_text_new(
        OUTPUT / "final-rollback-harness-result.json",
        (OUTPUT / "final-rollback-harness.stdout.txt").read_text(encoding="utf-8"),
    )
    write_text_new(OUTPUT / "final-working-tree.diff", git_output("diff", "--binary", "--", ".") + "\n")
    write_text_new(OUTPUT / "final-git-status.txt", git_output("status", "--short") + "\n")
    write_text_new(OUTPUT / "final-untracked-files.txt", git_output("ls-files", "--others", "--exclude-standard") + "\n")
    inputs = tested_input_hashes()
    inputs[str((ROOT / "scripts/run_super1_final_evidence.py").resolve())] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    metadata = {
        "run_id": "run-007",
        "evidence_generation": "FINAL_APPEND_ONLY",
        "generated_at_utc": now_utc(),
        "cwd": str(ROOT.resolve()),
        "git_head": git_output("rev-parse", "HEAD"),
        "git_diff_path": str((OUTPUT / "final-working-tree.diff").resolve()),
        "processes": processes,
        "tested_input_hashes": inputs,
        "dependency_versions": dependency_versions(),
        "scope": {"source_state_read": False, "target_state_read": False, "ssh_used": False, "mt5_or_broker_called": False, "orders_sent": False, "deployment_performed": False},
    }
    write_json_new(OUTPUT / "final-process-metadata.json", metadata)
    write_json_new(OUTPUT / "final-acceptance-matrix.json", build_acceptance_matrix(processes))
    manifest_files = []
    for path in sorted(OUTPUT.rglob("*")):
        if not path.is_file() or path.name == "final-hash-manifest.json":
            continue
        relative = path.relative_to(OUTPUT).as_posix()
        if not (relative.startswith("final-") or relative.startswith("final-rollback-fixture/")):
            continue
        manifest_files.append({"path": relative, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    write_json_new(OUTPUT / "final-hash-manifest.json", {"run_id": "run-007", "scope": "append-only final evidence", "files": manifest_files})
    print(json.dumps({"run_id": "run-007", "final": True, "processes": processes}, indent=2, default=str))
    return 0 if all(item["exit_code"] == 0 for item in processes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
