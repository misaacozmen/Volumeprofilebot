from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
GRID = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_067" / "comparison_pre2025.csv"
REPORT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_070" / "calmar_selection_v1"
BASE = ROOT / "research_candidates" / "v10_strategy_loop" / "nq_spx_locked_pair_2016_2024_v1.json"
CANDIDATE_DIR = ROOT / "research_candidates" / "v12_strategy_loop"


def main() -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    grid = pd.read_csv(GRID)
    eligible = grid[(grid["net_r"] > 285) & (grid["max_drawdown_r"].between(9, 13))].copy()
    eligible["calmar_proxy"] = eligible["net_r"] / eligible["max_drawdown_r"]
    eligible = eligible.sort_values(["calmar_proxy", "net_r"], ascending=[False, False])
    chosen = eligible.iloc[0]
    parameters = {"drawdown_threshold_r": 3.0, "drawdown_risk_scale": 0.75, "healthy_risk_scale": 1.30}
    base = json.loads(BASE.read_text(encoding="utf-8"))
    payload = {
        "name": "NQ_SPX_LOCKED_PAIR_CALMAR_RISK_V1", "status": "LOCAL_SELECTED_PRE2025_PENDING_ROLLING",
        "base_candidate": str(BASE), "risk_overlay": parameters,
        "selection_rule": "highest pre-2025 net_r/max_drawdown among net>285 and 9<=DD<=13",
        "selected_variant": str(chosen["variant"]), "observed_pre2025": {"trades": int(chosen["trades"]), "win_rate": float(chosen["win_rate"]), "net_r": float(chosen["net_r"]), "max_drawdown_r": float(chosen["max_drawdown_r"]), "profit_factor": float(chosen["profit_factor"]), "calmar_proxy": float(chosen["calmar_proxy"])},
        "eligible_plateau_count": int(len(eligible)), "historical_2025_2026_used_for_ranking": False,
        "open_trade_outcome_used": False, "live_enabled": False, "fresh_forward_required": True,
        "data_sha256": base["data_sha256"], "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "config_sha256": "",
    }
    payload["config_sha256"] = sha256(json.dumps({k: v for k, v in payload.items() if k != "config_sha256"}, sort_keys=True).encode()).hexdigest()
    candidate_path = CANDIDATE_DIR / "nq_spx_locked_pair_calmar_risk_v1.json"
    candidate_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    eligible.to_csv(REPORT / "eligible_pre2025.csv", index=False)
    (REPORT / "manifest.json").write_text(json.dumps({"candidate": str(candidate_path), **payload}, indent=2), encoding="utf-8")
    print(json.dumps({"selected": payload["selected_variant"], "observed_pre2025": payload["observed_pre2025"], "eligible_plateau_count": len(eligible), "candidate": str(candidate_path)}, indent=2))


if __name__ == "__main__":
    main()
