from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "selected_trades.csv"
EVIDENCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "audit" / "trade_evidence_index.csv"
REPORT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_065" / "locked_pair_v1"
CANDIDATE_DIR = ROOT / "research_candidates" / "v10_strategy_loop"
BASE_CONFIG = ROOT / "research_candidates" / "v9_strategy_loop" / "nq_spx_local_best_effort_50_iterations_v1.json"
IDENTITY = ["candidate", "entry_time", "direction", "sweep_time", "cisd_time"]


def select(frame: pd.DataFrame) -> pd.DataFrame:
    block_monday_short = frame["entry_weekday"].eq("Monday") & frame["direction"].eq("short")
    block_va_up = frame["liquidity_type"].eq("vah_val_proximity") & frame["overnight_direction"].eq("up")
    return frame[~(block_monday_short | block_va_up)].copy()


def stats(frame: pd.DataFrame) -> dict:
    ordered = frame.assign(_entry_dt=pd.to_datetime(frame["entry_time"], utc=True, format="mixed")).sort_values(["_entry_dt", "candidate"], kind="mergesort")
    pnl = ordered["r_multiple"].astype(float)
    equity = pnl.cumsum()
    dd = equity - equity.cummax().clip(lower=0.0)
    gross_win = pnl[pnl > 0].sum()
    gross_loss = -pnl[pnl < 0].sum()
    return {
        "trades": int(len(frame)), "win_rate": round(float((pnl > 0).mean() * 100), 2),
        "net_r": round(float(pnl.sum()), 2), "max_drawdown_r": round(float(abs(dd.min())), 2),
        "profit_factor": round(float(gross_win / gross_loss), 3),
    }


def stable_hash(frame: pd.DataFrame) -> str:
    columns = IDENTITY + ["exit_time", "result", "r_multiple", "entry_price", "stop_price", "target_price"]
    work = frame[columns].copy().sort_values(IDENTITY, kind="mergesort")
    return sha256(work.to_csv(index=False, lineterminator="\n").encode()).hexdigest()


def main() -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(SOURCE)
    pre = source[source["entry_year"].le(2024)].copy()
    selected = select(pre).sort_values(["entry_time", "candidate"], kind="mergesort")
    repeated = select(pre).sort_values(["entry_time", "candidate"], kind="mergesort")
    deterministic = stable_hash(selected) == stable_hash(repeated)

    rolling = []
    for years in (3, 4, 5):
        for start in range(2016, 2025 - years + 1):
            end = start + years - 1
            base_window = pre[pre["entry_year"].between(start, end)]
            candidate_window = selected[selected["entry_year"].between(start, end)]
            rolling.append({"window_years": years, "start_year": start, "end_year": end, **{f"baseline_{key}": value for key, value in stats(base_window).items()}, **{f"candidate_{key}": value for key, value in stats(candidate_window).items()}})
    rolling_frame = pd.DataFrame(rolling)

    evidence = pd.read_csv(EVIDENCE)
    selected_keys = set(selected[IDENTITY].astype(str).agg("|".join, axis=1))
    evidence_keys = set(evidence[IDENTITY].astype(str).agg("|".join, axis=1))
    graph_evidence_pass = selected_keys.issubset(evidence_keys) and bool(evidence[evidence[IDENTITY].astype(str).agg("|".join, axis=1).isin(selected_keys)]["all_event_bars_found"].all())
    prefix_source = pre[pre["entry_year"].le(2023)]
    prefix_selected = select(prefix_source)
    prefix_pass = set(prefix_selected[IDENTITY].astype(str).agg("|".join, axis=1)) == set(selected[selected["entry_year"].le(2023)][IDENTITY].astype(str).agg("|".join, axis=1))

    rules = [
        {"action": "BLOCK", "conditions": {"entry_weekday": "Monday", "direction": "short"}},
        {"action": "BLOCK", "conditions": {"liquidity_type": "vah_val_proximity", "overnight_direction": "up"}},
    ]
    base = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    payload = {
        "name": "NQ_SPX_LOCKED_PAIR_2016_2024_V1", "status": "LOCAL_LOCKED_PENDING_HISTORICAL_AUDIT",
        "base_candidate": str(BASE_CONFIG), "added_compound_rules": rules,
        "rule_selection_period": "2016-2024", "historical_2025_2026_used_for_selection": False,
        "criteria": {"win_rate_pct": [40, 45], "max_drawdown_r": [9, 13], "net_r_above_pre2025_baseline": 263},
        "observed_pre2025": stats(selected), "retention_ratio": round(len(selected) / len(pre), 6),
        "checks": {"deterministic": deterministic, "prefix_pass": prefix_pass, "graph_evidence_pass": graph_evidence_pass, "rolling_windows": len(rolling_frame)},
        "live_enabled": False, "fresh_forward_required": True,
        "data_sha256": base["data_sha256"], "result_sha256": stable_hash(selected),
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "config_sha256": "",
    }
    payload["config_sha256"] = sha256(json.dumps({k: v for k, v in payload.items() if k != "config_sha256"}, sort_keys=True).encode()).hexdigest()
    candidate_path = CANDIDATE_DIR / "nq_spx_locked_pair_2016_2024_v1.json"
    candidate_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    selected.to_csv(REPORT / "selected_trades_pre2025.csv", index=False)
    rolling_frame.to_csv(REPORT / "rolling_robustness.csv", index=False)
    (REPORT / "manifest.json").write_text(json.dumps({"candidate": str(candidate_path), **payload["checks"], "code_sha256": payload["code_sha256"], "config_sha256": payload["config_sha256"], "data_sha256": payload["data_sha256"], "result_sha256": payload["result_sha256"], "live_enabled": False}, indent=2), encoding="utf-8")
    print(json.dumps({"pre2025": stats(selected), "retention_ratio": payload["retention_ratio"], "rolling_windows": len(rolling_frame), "rolling_net_improved": int((rolling_frame["candidate_net_r"] > rolling_frame["baseline_net_r"]).sum()), "rolling_dd_pass": int(rolling_frame["candidate_max_drawdown_r"].le(13).sum()), "deterministic": deterministic, "prefix_pass": prefix_pass, "graph_evidence_pass": graph_evidence_pass, "candidate": str(candidate_path)}, indent=2))


if __name__ == "__main__":
    main()
