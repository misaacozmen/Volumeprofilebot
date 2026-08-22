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
from machine_policy_v1 import causal_fills_after_pair_cap, causal_policy_violations, filtered_decisions
import run_machine_metric_mechanics as mechanics


CONFIG = ROOT / "research_candidates" / "machine_metric_v1.json"
BASELINE = ROOT / "outputs" / "reports" / "canonical_production_full_history_research" / "canonical_decisions.csv"
OUTPUT = ROOT / "outputs" / "reports" / "machine_metric_candidate_v1"
POLICY_CODE = ROOT / "scripts" / "machine_policy_v1.py"


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    decisions = pd.read_csv(BASELINE)
    first = causal_fills_after_pair_cap(decisions, config)
    second = causal_fills_after_pair_cap(decisions, config)
    first_hash = stable_frame_hash(first)
    deterministic = first_hash == stable_frame_hash(second)
    violations = causal_policy_violations(decisions, config)
    eligible_decisions = filtered_decisions(decisions, config)
    blocked_count = len(decisions) - len(eligible_decisions)
    blocked_filled_count = int(
        (
            decisions["order_state"].eq("FILLED")
            & ~decisions.index.isin(eligible_decisions.index)
        ).sum()
    )
    dates = pd.to_datetime(first["date"])
    segments = {
        "all": first,
        "development_pre_2025": first[dates < mechanics.HOLDOUT_START],
        "holdout_2025_plus": first[dates >= mechanics.HOLDOUT_START],
    }
    summary = pd.DataFrame(
        [mechanics.metric_row(config["name"], segment, frame) for segment, frame in segments.items()]
    )
    manifest = {
        "schema_version": 1,
        "candidate": config["name"],
        "engine_code_hash": source_code_hash(),
        "baseline_decisions_sha256": file_hash(BASELINE),
        "baseline_result_hash": stable_frame_hash(decisions),
        "policy_code_sha256": file_hash(POLICY_CODE),
        "policy_config_sha256": file_hash(CONFIG),
        "candidate_result_hash": first_hash,
        "deterministic_rerun": deterministic,
        "causal_policy_violations": len(violations),
        "blocked_decision_count": blocked_count,
        "blocked_filled_pre_cap_count": blocked_filled_count,
        "post_cap_trade_count": len(first),
        "live_enabled": False,
        "status": (
            "LOCAL_CANDIDATE_FORWARD_REQUIRED"
            if deterministic and violations.empty
            else "REJECTED_TECHNICAL_GATE"
        ),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    first.to_csv(OUTPUT / "candidate_fills.csv", index=False)
    violations.to_csv(OUTPUT / "causal_policy_violations.csv", index=False)
    summary.to_csv(OUTPUT / "summary.csv", index=False)
    (OUTPUT / "candidate_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (OUTPUT / "report.md").write_text(
        "# MACHINE_METRIC_V1\n\n"
        "Machine-only candidate. Human/manual similarity is not used.\n\n"
        "```text\n" + summary.to_string(index=False) + "\n```\n\n"
        f"Status: `{manifest['status']}`. Live enabled: `False`.\n",
        encoding="utf-8",
    )
    print(summary.to_string(index=False))
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
