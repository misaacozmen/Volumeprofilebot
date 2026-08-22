from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_strategy_improvement_loop_v4 import causal_equity_overlay  # noqa: E402

SOURCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv"
GRID = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_067" / "comparison_pre2025.csv"
REPORT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_068" / "locked_risk_overlay_v1"
CANDIDATE_DIR = ROOT / "research_candidates" / "v11_strategy_loop"
BASE = ROOT / "research_candidates" / "v10_strategy_loop" / "nq_spx_locked_pair_2016_2024_v1.json"
PARAMETERS = {"drawdown_threshold_r": 5.0, "drawdown_risk_scale": 0.75, "healthy_risk_scale": 1.20}


def risk_stats(frame: pd.DataFrame) -> dict:
    ordered = frame.sort_values(["entry_dt", "candidate"], kind="mergesort")
    pnl = ordered["strategy_r"].astype(float)
    equity = pnl.cumsum()
    dd = equity - equity.cummax().clip(lower=0.0)
    return {
        "trades": int(len(frame)), "win_rate": round(float((frame["r_multiple"] > 0).mean() * 100), 2),
        "net_r": round(float(pnl.sum()), 3), "max_drawdown_r": round(float(abs(dd.min())), 3),
        "profit_factor": round(float(pnl[pnl > 0].sum() / -pnl[pnl < 0].sum()), 3),
    }


def apply(frame: pd.DataFrame) -> pd.DataFrame:
    return causal_equity_overlay(frame, PARAMETERS["drawdown_threshold_r"], PARAMETERS["drawdown_risk_scale"], PARAMETERS["healthy_risk_scale"])


def result_hash(frame: pd.DataFrame) -> str:
    columns = ["candidate", "entry_time", "exit_time", "direction", "sweep_time", "cisd_time", "risk_scale", "strategy_r"]
    return sha256(frame[columns].sort_values(["entry_time", "candidate"], kind="mergesort").to_csv(index=False, lineterminator="\n").encode()).hexdigest()


def main() -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(SOURCE)
    selected = apply(source)
    repeated = apply(source)
    rolling = []
    for years in (3, 4, 5):
        for start in range(2016, 2025 - years + 1):
            end = start + years - 1
            window = source[source["entry_year"].between(start, end)].copy()
            result = apply(window)
            rolling.append({"window_years": years, "start_year": start, "end_year": end, **risk_stats(result)})
    rolling_frame = pd.DataFrame(rolling)
    grid = pd.read_csv(GRID)
    plateau = grid[(grid["net_r"] > 285) & (grid["max_drawdown_r"] <= 13)].copy()
    base = json.loads(BASE.read_text(encoding="utf-8"))
    payload = {
        "name": "NQ_SPX_LOCKED_PAIR_CAUSAL_RISK_V1", "status": "LOCAL_LOCKED_PENDING_HISTORICAL_AUDIT",
        "base_candidate": str(BASE), "risk_overlay": PARAMETERS,
        "decision_state": "realized scaled equity drawdown from trades with exit_time <= current entry_time",
        "open_trade_outcome_used": False, "selection_period": "2016-2024", "historical_2025_2026_used_for_selection": False,
        "observed_pre2025": risk_stats(selected), "parameter_plateau_pass_count": int(len(plateau)),
        "rolling": {"windows": int(len(rolling_frame)), "dd_pass_count": int(rolling_frame["max_drawdown_r"].le(13).sum()), "positive_net_count": int(rolling_frame["net_r"].gt(0).sum())},
        "deterministic": result_hash(selected) == result_hash(repeated), "live_enabled": False, "fresh_forward_required": True,
        "data_sha256": base["data_sha256"], "result_sha256": result_hash(selected),
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "config_sha256": "",
    }
    payload["config_sha256"] = sha256(json.dumps({k: v for k, v in payload.items() if k != "config_sha256"}, sort_keys=True).encode()).hexdigest()
    candidate_path = CANDIDATE_DIR / "nq_spx_locked_pair_causal_risk_v1.json"
    candidate_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    selected.to_csv(REPORT / "selected_trades_pre2025.csv", index=False)
    rolling_frame.to_csv(REPORT / "rolling_robustness.csv", index=False)
    plateau.to_csv(REPORT / "parameter_plateau.csv", index=False)
    (REPORT / "manifest.json").write_text(json.dumps({"candidate": str(candidate_path), **payload}, indent=2), encoding="utf-8")
    print(json.dumps({"observed_pre2025": payload["observed_pre2025"], "plateau_pass_count": len(plateau), "rolling": payload["rolling"], "deterministic": payload["deterministic"], "candidate": str(candidate_path)}, indent=2))


if __name__ == "__main__":
    main()
