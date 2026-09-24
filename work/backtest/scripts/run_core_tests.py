from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# The reliability audit is an internal engine gate. Deployment-host checks and
# owner-created Windows fixtures are collected by the full gate separately.
CORE_TESTS = (
    "tests/test_candidate_artifacts.py",
    "tests/test_engine_contracts.py",
    "tests/test_engine_pipeline.py",
    "tests/test_environment_schema_scanner.py",
    "tests/test_evaluation_window.py",
    "tests/test_financial_numeric_contracts.py",
    "tests/test_forward_shadow.py",
    "tests/test_instrument_contract.py",
    "tests/test_live_retry_policy.py",
    "tests/test_live_risk_guard.py",
    "tests/test_optimization_walk_forward.py",
    "tests/test_risk_causality.py",
    "tests/test_risk_xray.py",
    "tests/test_strategy_health.py",
    "tests/test_super1_continuation.py",
    "tests/test_super1_instruction_1_11.py",
    "tests/test_super1_runtime_hardening.py",
)


def main() -> None:
    command = [sys.executable, "-m", "pytest", "-q", *CORE_TESTS]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    output = (result.stdout + result.stderr).strip()
    if output:
        print(output)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
