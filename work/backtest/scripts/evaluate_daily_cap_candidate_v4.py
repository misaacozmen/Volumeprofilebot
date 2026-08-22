from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from freeze_strategy_improvement_loop_v4 import select, stats  # noqa: E402
from freeze_daily_cap_v4 import CAP_R, apply, stable_hash  # noqa: E402

SOURCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "selected_trades.csv"
CANDIDATE = ROOT / "research_candidates" / "v15_strategy_loop" / "nq_spx_locked_pair_daily_cap_v1.json"
REPORT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_083" / "daily_cap_historical_audit_v1"
SEGMENTS = {"learn_2016_2021": (2016, 2021), "validation_2022_2023": (2022, 2023), "validation_2024": (2024, 2024), "historical_2025_2026": (2025, 2026), "all_2016_2026": (2016, 2026)}


def main() -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    locked_bytes = CANDIDATE.read_bytes()
    locked = json.loads(locked_bytes)
    source = pd.read_csv(SOURCE)
    selected = apply(select(source))
    rows = [{"segment": name, **stats(selected[selected["entry_year"].between(start, end)])} for name, (start, end) in SEGMENTS.items()]
    observed = rows[-1]
    criteria = {"win_rate_40_45": 40 <= observed["win_rate"] <= 45, "net_r_above_292_baseline": observed["net_r"] > 292, "max_drawdown_9_13": 9 <= observed["max_drawdown_r"] <= 13, "retention_at_least_70pct": len(selected) / len(source) >= 0.70}
    unchanged = locked_bytes == CANDIDATE.read_bytes()
    decision = "ACCEPT_LOCAL_FRESH_FORWARD_CANDIDATE" if all(criteria.values()) and unchanged else "REJECT_CRITERIA_UNMET"
    manifest = {"iteration": 83, "decision": decision, "candidate": str(CANDIDATE), "daily_cap_r": CAP_R, "criteria": criteria, "observed": observed, "retention_ratio": round(len(selected) / len(source), 6), "historical_used_for_rule_change": False, "config_unchanged": unchanged, "live_enabled": False, "fresh_forward_required": True, "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "config_sha256": locked["config_sha256"], "data_sha256": locked["data_sha256"], "result_sha256": stable_hash(selected)}
    selected.to_csv(REPORT / "selected_trades.csv", index=False)
    pd.DataFrame(rows).to_csv(REPORT / "segment_metrics.csv", index=False)
    (REPORT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(rows).to_string(index=False))
    print(json.dumps({"criteria": criteria, "retention_ratio": manifest["retention_ratio"], "decision": decision}, indent=2))


if __name__ == "__main__":
    main()
