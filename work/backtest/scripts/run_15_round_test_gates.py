from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "outputs" / "reports" / "ordered_15_round_research_v1"


def main() -> None:
    pytest_available = importlib.util.find_spec("pytest") is not None
    command = (
        [sys.executable, "-m", "pytest", "-q"]
        if pytest_available
        else [sys.executable, "scripts/run_core_tests.py"]
    )
    command_label = " ".join(command)
    for number in range(1, 16):
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        output = (completed.stdout + completed.stderr).strip() + "\n"
        directory = REPORT_ROOT / f"round_{number:02d}"
        result_path = directory / "tests.txt"
        result_path.write_text(output, encoding="utf-8")
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["test_package"] = command_label
        manifest["test_status"] = "PASS" if completed.returncode == 0 else "FAIL"
        manifest["test_output_sha256"] = sha256(output.encode()).hexdigest()
        manifest["test_completed_utc"] = datetime.now(timezone.utc).isoformat()
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"round_{number:02d}: {output.strip()}")
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
