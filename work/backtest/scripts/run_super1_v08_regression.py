"""Sealed-by-runner regression gate for the V08 observation/publication fixes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def check(root: Path) -> dict[str, object]:
    observation = (root / "scripts" / "super1_observation.py").read_text(encoding="utf-8")
    reporter = (root / "scripts" / "super1_evidence_report.py").read_text(encoding="utf-8")
    errors: list[str] = []
    if (root / "tests" / "conftest.py").is_file():
        errors.append("V08_AUTOUSE_FALLBACK_PRESENT")
    if "SCHEMA = \"super1-observation/v3\"" not in observation:
        errors.append("V08_OBSERVATION_SCHEMA_NOT_V3")
    if "trace.get(\"actual\")" in observation or "trace[\"actual\"]" in observation:
        errors.append("TRACE_SELF_CLAIMED_ACTUAL")
    if "_measure_raw" in reporter and "from super1_observation" in reporter:
        errors.append("REPORTER_IMPORTS_PRODUCER_MEASUREMENT")
    if not (root / "scripts" / "super1_required_nodes.py").is_file():
        errors.append("V08_MANIFEST_LOADER_MISSING")
    result = {"source_root": str(root), "status": "PASS" if not errors else "FAIL", "errors": errors}
    print(json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    configured = os.environ.get("SUPER1_V08_REGRESSION_SOURCE_ROOT")
    if not configured:
        print(json.dumps({"status": "FAIL", "errors": ["SOURCE_ROOT_REQUIRED"]}, sort_keys=True))
        return 2
    result = check(Path(configured).resolve())
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
