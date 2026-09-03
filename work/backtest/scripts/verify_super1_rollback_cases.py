"""Independent oracle for the local O01 callback/state-machine harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    result = json.loads(args.result.read_text(encoding="utf-8"))
    expected = {str(item["name"]): item for item in contract["cases"]}
    rows = result if isinstance(result, list) else [result]
    errors: list[str] = []
    if {str(row.get("case")) for row in rows} != set(expected):
        errors.append("O01 case set mismatch")
    for row in rows:
        name = str(row.get("case"))
        case = expected.get(name)
        if case is None:
            continue
        vector = {
            key: bool(row.get("input_vector", {}).get(key))
            for key in (
                "brokerSideEffectPossible", "tasksStopped", "appMutation", "stateMutation",
                "taskXmlMutation", "fatalLatchPreexisting", "recoveryLatchPreexisting",
            )
        }
        expected_vector = {key: bool(case[key]) for key in vector}
        if vector != expected_vector:
            errors.append(f"{name}: input vector mismatch")
        trace = [str(item.get("name")) for item in row.get("callback_trace", [])]
        if trace != [str(item) for item in case["expectedTrace"]]:
            errors.append(f"{name}: ordered callback trace mismatch")
        if row.get("policy_status") != case["expectedStatus"]:
            errors.append(f"{name}: policy status mismatch")
        counts = row.get("callback_counts", {})
        if any(counts.get(item) != 1 for item in trace) or set(counts) != set(trace):
            errors.append(f"{name}: callback count mismatch")
        if row.get("automatic_restart") is not False or row.get("stopped_proof") is not False:
            errors.append(f"{name}: unsafe restart/stopped claim")
        if name == "both_latch_writes_fail":
            if row.get("persistent_latch") or row.get("persistent_recovery") or row.get("latch_status") != "FAIL" or row.get("process_recreation_latch_consumed"):
                errors.append("both_latch_writes_fail: unlatch state mismatch")
        elif row.get("policy_status") == "UNSAFE_NO_SEND" and not (row.get("persistent_latch") or row.get("persistent_recovery")):
            errors.append(f"{name}: persistent gate not proven")
    payload = {"ok": not errors, "errors": errors, "case_count": len(rows)}
    print(json.dumps(payload, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
