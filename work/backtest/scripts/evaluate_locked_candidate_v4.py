from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from freeze_strategy_improvement_loop_v4 import IDENTITY, SOURCE, stable_hash, stats, select  # noqa: E402

REPORT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_066" / "locked_historical_audit_v1"
CANDIDATE = ROOT / "research_candidates" / "v10_strategy_loop" / "nq_spx_locked_pair_2016_2024_v1.json"
EVIDENCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "audit" / "trade_evidence_index.csv"
SEGMENTS = {
    "learn_2016_2021": (2016, 2021), "validation_2022_2023": (2022, 2023),
    "validation_2024": (2024, 2024), "historical_2025_2026": (2025, 2026),
    "all_2016_2026": (2016, 2026),
}


def main() -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    locked = json.loads(CANDIDATE.read_text(encoding="utf-8"))
    source = pd.read_csv(SOURCE)
    selected = select(source).sort_values(["entry_time", "candidate"], kind="mergesort")
    rows = [{"segment": name, **stats(selected[selected["entry_year"].between(start, end)])} for name, (start, end) in SEGMENTS.items()]
    metrics = pd.DataFrame(rows)
    all_stats = rows[-1]
    baseline_full = stats(source)
    criteria = {
        "win_rate_40_45": 40 <= all_stats["win_rate"] <= 45,
        "net_r_above_baseline": all_stats["net_r"] > baseline_full["net_r"],
        "max_drawdown_9_13": 9 <= all_stats["max_drawdown_r"] <= 13,
        "retention_at_least_70pct": len(selected) / len(source) >= 0.70,
    }
    evidence = pd.read_csv(EVIDENCE)
    selected_keys = set(selected[IDENTITY].astype(str).agg("|".join, axis=1))
    evidence_rows = evidence[evidence[IDENTITY].astype(str).agg("|".join, axis=1).isin(selected_keys)]
    graph_evidence_pass = len(evidence_rows) == len(selected) and bool(evidence_rows["all_event_bars_found"].all())
    result_hash = stable_hash(selected)
    config_unchanged = locked["config_sha256"] == json.loads(CANDIDATE.read_text(encoding="utf-8"))["config_sha256"]
    decision = "ACCEPT_LOCAL_FRESH_FORWARD_CANDIDATE" if all(criteria.values()) and graph_evidence_pass and config_unchanged else "REJECT_CRITERIA_UNMET"
    manifest = {
        "iteration": 66, "decision": decision, "locked_candidate": str(CANDIDATE),
        "baseline_full": baseline_full, "observed_full": all_stats, "criteria": criteria,
        "historical_used_for_rule_change": False, "config_unchanged_after_historical_reveal": config_unchanged,
        "graph_evidence_pass": graph_evidence_pass, "live_enabled": False, "fresh_forward_required": True,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "config_sha256": locked["config_sha256"],
        "data_sha256": locked["data_sha256"], "result_sha256": result_hash,
    }
    selected.to_csv(REPORT / "selected_trades.csv", index=False)
    metrics.to_csv(REPORT / "segment_metrics.csv", index=False)
    (REPORT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(metrics.to_string(index=False))
    print(json.dumps({"baseline_full": baseline_full, "retention_ratio": round(len(selected) / len(source), 6), "criteria": criteria, "graph_evidence_pass": graph_evidence_pass, "decision": decision}, indent=2))


if __name__ == "__main__":
    main()
