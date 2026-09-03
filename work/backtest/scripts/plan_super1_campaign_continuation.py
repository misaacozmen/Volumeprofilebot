"""Create a non-applying Super1 continuation design record."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from super1_continuation import (
    build_transition_record,
    inventory_snapshot_root,
    write_exclusive_json,
    validate_transition_record,
)
import run_capital_forward as core


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "live_forward" / "super1_xm_mt5_demo_config.json"
PARENT_BASELINE = ROOT / "forward_shadow" / "baseline_lock.json"


def super1_harness_paths(runtime: dict[str, Any]) -> tuple[Path, ...]:
    """Return the exact ordered paths configured by Super1.configure_core."""
    return (
        ROOT / "scripts" / "run_super1_xm_mt5_forward.py",
        ROOT / "scripts" / "run_xm_mt5_forward.py",
        ROOT / "scripts" / "run_capital_forward.py",
        ROOT / "scripts" / "run_forward_shadow.py",
        (ROOT / str(runtime["candidate_path"])).resolve(),
        (ROOT / str(runtime["signal_contract_path"])).resolve(),
        ROOT / "research_candidates" / "super1" / "super1_manifest.json",
    )


def super1_harness_hash(runtime: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for path in (*super1_harness_paths(runtime), RUNTIME, PARENT_BASELINE):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_design(snapshot_root: Path | None = None) -> dict[str, Any]:
    runtime = json.loads(RUNTIME.read_text(encoding="utf-8"))
    local_hashes = {
        "release": None,
        "runtime": file_hash(RUNTIME),
        "harness": super1_harness_hash(runtime),
        "contract": file_hash(ROOT / runtime["signal_contract_path"]),
        "calendar": str(runtime["rth_session_calendar"]["sha256"]),
    }
    record = build_transition_record(
        plan_created_at=datetime.now(timezone.utc).isoformat(),
        original_campaign_lock_sha256=None,
        previous_transition_hash=None,
        old_hashes={key: None for key in local_hashes},
        new_hashes=local_hashes,
        unchanged_engine_and_risk={
            "engine": core.source_code_hash(),
            "frozen_candidate": str(runtime["candidate_artifact_sha256"]),
            "strategy_risk": hashlib.sha256(
                json.dumps(runtime["risk_rule"], sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        },
        broker_identity=(
            f"{runtime.get('account_login')}|{runtime.get('expected_server')}|"
            f"{runtime.get('expected_company')}"
        ),
        source_snapshot_manifest_sha256=None,
        state_schema_versions={},
    )
    record["expected_local_schema_versions"] = {
        "campaign_lock": 1,
        "minute_bars_and_conflicts_sqlite": 1,
        "order_idempotency_sqlite": 1,
        "broker_execution_states": 1,
    }
    record["super1_harness_paths"] = [str(path) for path in super1_harness_paths(runtime)]
    result = {
        "transition_record": record,
        "validation": validate_transition_record(record),
        "apply_performed": False,
        "source_state_access": "NOT_ATTEMPTED" if snapshot_root is None else "INSPECT_ONLY",
    }
    if snapshot_root is not None:
        result["source_root_inventory"] = inventory_snapshot_root(snapshot_root)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    design = build_design(args.snapshot_root)
    if args.output:
        write_exclusive_json(args.output, design, source_root=args.snapshot_root)
    else:
        print(json.dumps(design, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
