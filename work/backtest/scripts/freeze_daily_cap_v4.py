from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_strategy_improvement_loop_v4 import causal_period_loss_cap  # noqa: E402
from freeze_strategy_improvement_loop_v4 import IDENTITY, stats  # noqa: E402

SOURCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv"
EVIDENCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "audit" / "trade_evidence_index.csv"
BASE = ROOT / "research_candidates" / "v10_strategy_loop" / "nq_spx_locked_pair_2016_2024_v1.json"
REPORT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_082" / "locked_daily_cap_v1"
CANDIDATE_DIR = ROOT / "research_candidates" / "v15_strategy_loop"
CAP_R = 1.0


def apply(frame: pd.DataFrame) -> pd.DataFrame:
    return causal_period_loss_cap(frame, CAP_R, "day")


def stable_hash(frame: pd.DataFrame) -> str:
    return sha256(frame[IDENTITY + ["exit_time", "r_multiple"]].sort_values(["entry_time", "candidate"], kind="mergesort").to_csv(index=False, lineterminator="\n").encode()).hexdigest()


def main() -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(SOURCE)
    selected = apply(source)
    rolling = []
    for years in (3, 4, 5):
        for start in range(2016, 2025 - years + 1):
            end = start + years - 1
            window = source[source["entry_year"].between(start, end)].copy()
            result = apply(window)
            rolling.append({"window_years": years, "start_year": start, "end_year": end, **stats(result), "baseline_net_r": stats(window)["net_r"]})
    rolling_frame = pd.DataFrame(rolling)
    evidence = pd.read_csv(EVIDENCE)
    keys = set(selected[IDENTITY].astype(str).agg("|".join, axis=1))
    evidence_rows = evidence[evidence[IDENTITY].astype(str).agg("|".join, axis=1).isin(keys)]
    graph_pass = len(evidence_rows) == len(selected) and bool(evidence_rows["all_event_bars_found"].all())
    base = json.loads(BASE.read_text(encoding="utf-8"))
    payload = {
        "name": "NQ_SPX_LOCKED_PAIR_DAILY_CAP_V1", "status": "LOCAL_LOCKED_PENDING_HISTORICAL_AUDIT",
        "base_candidate": str(BASE), "daily_realized_loss_cap_r": CAP_R, "reset": "trading_day",
        "state_rule": "only trades with exit_time <= current entry_time", "open_trade_outcome_used": False,
        "selection_period": "2016-2024", "historical_2025_2026_used_for_selection": False,
        "observed_pre2025": stats(selected), "retention_ratio": round(len(selected) / len(source), 6),
        "rolling": {"windows": len(rolling_frame), "positive": int(rolling_frame["net_r"].gt(0).sum()), "dd_pass": int(rolling_frame["max_drawdown_r"].le(13).sum()), "net_nonworse": int(rolling_frame["net_r"].ge(rolling_frame["baseline_net_r"]).sum())},
        "deterministic": stable_hash(selected) == stable_hash(apply(source)), "graph_evidence_pass": graph_pass,
        "live_enabled": False, "fresh_forward_required": True, "data_sha256": base["data_sha256"],
        "result_sha256": stable_hash(selected), "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "config_sha256": "",
    }
    payload["config_sha256"] = sha256(json.dumps({k: v for k, v in payload.items() if k != "config_sha256"}, sort_keys=True).encode()).hexdigest()
    candidate_path = CANDIDATE_DIR / "nq_spx_locked_pair_daily_cap_v1.json"
    candidate_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    selected.to_csv(REPORT / "selected_trades_pre2025.csv", index=False)
    rolling_frame.to_csv(REPORT / "rolling_robustness.csv", index=False)
    (REPORT / "manifest.json").write_text(json.dumps({"candidate": str(candidate_path), **payload}, indent=2), encoding="utf-8")
    print(json.dumps({"observed_pre2025": payload["observed_pre2025"], "retention_ratio": payload["retention_ratio"], "rolling": payload["rolling"], "deterministic": payload["deterministic"], "graph_evidence_pass": graph_pass, "candidate": str(candidate_path)}, indent=2))


if __name__ == "__main__":
    main()
