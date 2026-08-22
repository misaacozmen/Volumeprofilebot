from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest.engine_pipeline import source_code_hash, stable_frame_hash
from machine_policy_v1 import causal_fills_after_pair_cap, causal_policy_violations
import run_machine_metric_mechanics as mechanics


SELECTION = ROOT / "outputs" / "reports" / "machine_candidate_family_v2" / "selection.json"
BASELINE = ROOT / "outputs" / "reports" / "canonical_production_full_history_research" / "canonical_decisions.csv"
POLICY_CODE = ROOT / "scripts" / "machine_policy_v1.py"
OUTPUT = ROOT / "outputs" / "reports" / "machine_candidate_v2_selected"


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main() -> None:
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    if selection.get("selected_candidate") == "NONE" or not selection.get("config_path"):
        raise SystemExit("No development-selected V2 candidate is available to freeze")
    config_path = ROOT / selection["config_path"]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    decisions = pd.read_csv(BASELINE)
    first = causal_fills_after_pair_cap(decisions, config)
    second = causal_fills_after_pair_cap(decisions, config)
    deterministic = stable_frame_hash(first) == stable_frame_hash(second)
    violations = causal_policy_violations(decisions, config)
    dates = pd.to_datetime(first["date"])
    segment_frames = {
        "all": first,
        "development_pre_2025": first[dates < mechanics.HOLDOUT_START],
        "holdout_2025_plus": first[dates >= mechanics.HOLDOUT_START],
    }
    summary = pd.DataFrame(
        [mechanics.metric_row(config["name"], name, frame) for name, frame in segment_frames.items()]
    )
    yearly = pd.DataFrame(
        [
            {"year": int(year), **mechanics.metric_row(config["name"], "year", sample)}
            for year, sample in first.assign(year=dates.dt.year).groupby("year")
        ]
    )
    manifest = {
        "schema_version": 1,
        "candidate": config["name"],
        "engine_code_hash": source_code_hash(),
        "baseline_decisions_sha256": file_hash(BASELINE),
        "policy_code_sha256": file_hash(POLICY_CODE),
        "policy_config_sha256": file_hash(config_path),
        "candidate_result_hash": stable_frame_hash(first),
        "deterministic_rerun": deterministic,
        "causal_policy_violations": len(violations),
        "selection_used_2025_plus_results": False,
        "fresh_forward_required": True,
        "live_enabled": False,
        "status": (
            "LOCAL_V2_CANDIDATE_FRESH_FORWARD_REQUIRED"
            if deterministic and violations.empty
            else "REJECTED_TECHNICAL_GATE"
        ),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    first.to_csv(OUTPUT / "candidate_fills.csv", index=False)
    violations.to_csv(OUTPUT / "causal_policy_violations.csv", index=False)
    summary.to_csv(OUTPUT / "summary.csv", index=False)
    yearly.to_csv(OUTPUT / "yearly.csv", index=False)
    (OUTPUT / "candidate_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (OUTPUT / "report.md").write_text(
        "# V2_SPX_QUALITY_TUESDAY\n\n"
        "New machine-only candidate; old candidates remain unchanged.\n\n"
        "Rules: block SPX FIRST_QUALIFIED_STRUCTURE, SELECTED_LIQUIDITY_SWEEP, and Tuesday entries.\n\n"
        "```text\n" + summary.to_string(index=False) + "\n```\n\n"
        f"Status: `{manifest['status']}`. Live enabled: `False`.\n",
        encoding="utf-8",
    )
    print(summary.to_string(index=False))
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
