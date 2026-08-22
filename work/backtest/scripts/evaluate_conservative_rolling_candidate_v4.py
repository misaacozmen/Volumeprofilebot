from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from candidate_artifact import ArtifactValidationError, load_artifact  # noqa: E402
from freeze_strategy_improvement_loop_v4 import select  # noqa: E402
from freeze_risk_overlay_v4 import result_hash, risk_stats  # noqa: E402
from run_strategy_improvement_loop_v4 import causal_rolling_state_scale  # noqa: E402


SOURCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "selected_trades.csv"
CANDIDATE = ROOT / "research_candidates" / "v19_strategy_loop" / "nq_spx_locked_pair_conservative_rolling_v1.json"
REPORT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_097" / "conservative_rolling_historical_audit_v1"
SEGMENTS = {
    "learn_2016_2021": (2016, 2021),
    "validation_2022_2023": (2022, 2023),
    "validation_2024": (2024, 2024),
    "final_holdout_2025_2026": (2025, 2026),
}


def apply_payload(frame: pd.DataFrame, payload: dict[str, Any]) -> pd.DataFrame:
    rules = payload.get("setup_rules", payload.get("added_compound_rules", []))
    selected = frame
    if rules:
        selected = selected.copy()
        blocked = pd.Series(False, index=selected.index)
        for rule in rules:
            if rule.get("action") != "BLOCK" or not isinstance(rule.get("conditions"), dict):
                raise ArtifactValidationError("Only declarative BLOCK setup rules are supported")
            mask = pd.Series(True, index=selected.index)
            for column, value in rule["conditions"].items():
                if column not in selected:
                    raise ArtifactValidationError(f"Candidate rule references missing column: {column}")
                mask &= selected[column].fillna("<NA>").astype(str).eq(
                    "<NA>" if value is None else str(value)
                )
            blocked |= mask
        selected = selected[~blocked].copy()
    elif payload.get("base_candidate"):
        # V19 inherits the frozen setup selector from V10. This branch preserves the
        # legacy candidate shape while the new V20 artifact carries rules inline.
        selected = select(selected)

    risk = payload.get("risk_rule")
    if not isinstance(risk, dict):
        raise ArtifactValidationError("Candidate payload is missing risk_rule")
    required = {"lookback", "negative_scale", "nonnegative_scale"}
    if set(risk) != required:
        raise ArtifactValidationError(f"risk_rule must contain exactly {sorted(required)}")
    return causal_rolling_state_scale(
        selected,
        int(risk["lookback"]),
        float(risk["negative_scale"]),
        float(risk["nonnegative_scale"]),
    )


def evaluate(candidate_path: Path = CANDIDATE) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    locked = load_artifact(
        candidate_path,
        ROOT,
        artifact_type="strategy_candidate",
        verify_inputs=True,
    )
    source = pd.read_csv(SOURCE)
    selected = apply_payload(source, locked)
    repeated = apply_payload(source, locked)
    deterministic = result_hash(selected) == result_hash(repeated)
    expected_development_hash = locked.get("development_result_sha256")
    development = selected[selected["entry_year"].le(2024)].copy()
    development_hash_matches = (
        isinstance(expected_development_hash, str)
        and result_hash(development) == expected_development_hash
    )
    rows = [
        {
            "segment": name,
            **risk_stats(selected[selected["entry_year"].between(start, end)]),
        }
        for name, (start, end) in SEGMENTS.items()
    ]
    metrics = pd.DataFrame(rows)
    holdout = metrics[metrics["segment"] == "final_holdout_2025_2026"].iloc[0]
    holdout_criteria = {
        "net_r_positive": bool(holdout["net_r"] > 0),
        "max_drawdown_at_most_13": bool(holdout["max_drawdown_r"] <= 13),
        "retains_trades": bool(holdout["trades"] > 0),
    }
    checks = {
        "artifact_schema_valid": True,
        "artifact_inputs_verified": True,
        "payload_applied": True,
        "deterministic": deterministic,
        "development_result_hash_matches": development_hash_matches,
    }
    decision = (
        "ACCEPT_LOCAL_FRESH_FORWARD_CANDIDATE"
        if all(checks.values()) and all(holdout_criteria.values())
        else "REJECT_GATE_UNMET"
    )
    manifest = {
        "iteration": 97,
        "decision": decision,
        "candidate": candidate_path.relative_to(ROOT).as_posix(),
        "candidate_artifact_sha256": locked["artifact_sha256"],
        "risk_parameters": locked["risk_rule"],
        "holdout_criteria": holdout_criteria,
        "checks": checks,
        "selection_used_final_holdout": False,
        "live_enabled": False,
        "fresh_forward_required": True,
        "result_sha256": result_hash(selected),
    }
    return manifest, selected, metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, default=CANDIDATE)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    candidate_path = args.candidate
    if not candidate_path.is_absolute():
        candidate_path = ROOT / candidate_path
    manifest, selected, metrics = evaluate(candidate_path)
    args.report.mkdir(parents=True, exist_ok=True)
    selected.to_csv(args.report / "selected_trades.csv", index=False)
    metrics.to_csv(args.report / "segment_metrics.csv", index=False)
    (args.report / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(metrics.to_string(index=False))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    if manifest["decision"].startswith("REJECT"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
