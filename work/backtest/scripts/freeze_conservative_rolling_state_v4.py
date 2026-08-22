from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_strategy_improvement_loop_v4 import causal_rolling_state_scale  # noqa: E402
from freeze_risk_overlay_v4 import result_hash, risk_stats  # noqa: E402
from freeze_strategy_improvement_loop_v4 import IDENTITY  # noqa: E402
from candidate_artifact import build_provenance, seal_artifact, write_json_atomic  # noqa: E402

SOURCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv"
EVIDENCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "audit" / "trade_evidence_index.csv"
BASE = ROOT / "research_candidates" / "v10_strategy_loop" / "nq_spx_locked_pair_2016_2024_v1.json"
REPORT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_096" / "locked_conservative_rolling_v1"
CANDIDATE_DIR = ROOT / "research_candidates" / "v19_strategy_loop"
PARAMETERS = {"lookback": 10, "negative_scale": 0.75, "nonnegative_scale": 1.10}


def apply(frame: pd.DataFrame) -> pd.DataFrame:
    return causal_rolling_state_scale(frame, **PARAMETERS)


def main() -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(SOURCE)
    selected = apply(source)
    rolling = []
    for years in (3, 4, 5):
        for start in range(2016, 2025 - years + 1):
            end = start + years - 1
            result = apply(source[source["entry_year"].between(start, end)].copy())
            rolling.append({"window_years": years, "start_year": start, "end_year": end, **risk_stats(result)})
    rolling_frame = pd.DataFrame(rolling)
    evidence = pd.read_csv(EVIDENCE)
    keys = set(selected[IDENTITY].astype(str).agg("|".join, axis=1))
    evidence_rows = evidence[evidence[IDENTITY].astype(str).agg("|".join, axis=1).isin(keys)]
    graph_pass = len(evidence_rows) == len(selected) and bool(evidence_rows["all_event_bars_found"].all())
    payload = seal_artifact({
        "artifact_type": "strategy_candidate",
        "artifact_id": "nq_spx_locked_pair_conservative_rolling_v1",
        "name": "NQ_SPX_LOCKED_PAIR_CONSERVATIVE_ROLLING_V1", "status": "LOCAL_LOCKED_PENDING_HISTORICAL_AUDIT",
        "base_candidate": BASE.relative_to(ROOT).as_posix(), "risk_rule": PARAMETERS,
        "setup_rules": [
            {"action": "BLOCK", "conditions": {"entry_weekday": "Monday", "direction": "short"}},
            {"action": "BLOCK", "conditions": {"liquidity_type": "vah_val_proximity", "overnight_direction": "up"}},
        ],
        "selection_rule": "lowest nonnegative risk scale among pre-2025 passing rolling-state plateau; then highest net",
        "state_rule": "sum of last 10 terminal raw R values; open trades excluded", "open_trade_outcome_used": False,
        "selection_period": "2016-2024", "historical_2025_2026_used_for_selection": False,
        "observed_pre2025": risk_stats(selected), "retention_ratio": 1.0,
        "rolling": {"windows": len(rolling_frame), "positive": int(rolling_frame["net_r"].gt(0).sum()), "dd_pass": int(rolling_frame["max_drawdown_r"].le(13).sum())},
        "deterministic": result_hash(selected) == result_hash(apply(source)), "graph_evidence_pass": graph_pass,
        "live_enabled": False, "fresh_forward_required": True,
        "development_result_sha256": result_hash(selected),
        "provenance": build_provenance(
            ROOT,
            [
                (SOURCE, "research_dataset"),
                (EVIDENCE, "trade_evidence"),
                (BASE, "base_candidate"),
                (Path(__file__), "freeze_code"),
                (ROOT / "scripts" / "candidate_artifact.py", "artifact_code"),
                (ROOT / "scripts" / "run_strategy_improvement_loop_v4.py", "strategy_code"),
                (ROOT / "scripts" / "freeze_strategy_improvement_loop_v4.py", "selector_code"),
                (ROOT / "scripts" / "freeze_risk_overlay_v4.py", "metric_code"),
            ],
            dependencies=("pandas",),
        ),
    })
    candidate_path = CANDIDATE_DIR / "nq_spx_locked_pair_conservative_rolling_v1.json"
    write_json_atomic(candidate_path, payload)
    selected.to_csv(REPORT / "selected_trades_pre2025.csv", index=False)
    rolling_frame.to_csv(REPORT / "rolling_robustness.csv", index=False)
    write_json_atomic(REPORT / "manifest.json", {"candidate": candidate_path.relative_to(ROOT).as_posix(), **payload})
    print(json.dumps({"observed_pre2025": payload["observed_pre2025"], "rolling": payload["rolling"], "deterministic": payload["deterministic"], "graph_evidence_pass": graph_pass, "candidate": str(candidate_path)}, indent=2))


if __name__ == "__main__":
    main()
