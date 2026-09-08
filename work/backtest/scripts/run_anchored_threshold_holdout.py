from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import time

import pandas as pd
from backtest.market_calendar import signed_market_dates


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_canonical_production_full_history as full_history
import run_engine_path_comparison_2025_feb_mar as comparison
import run_main_candidate_filter_tests as filters
from backtest.engine_pipeline import EngineLeg, run_canonical_pair_pipeline, stable_frame_hash
from backtest.manual_state import ManualStateConfig
from backtest.strategy import compute_first30_range


REPORT_DIR = ROOT / "outputs" / "reports" / "anchored_threshold_holdout_research"
START = pd.Timestamp("2025-01-01")
END = pd.Timestamp("2026-07-02")


def anchored_q60(frame: pd.DataFrame) -> float:
    values = [
        value
        for trade_date in sorted(frame["date"].unique())
        if pd.Timestamp(trade_date) < START
        for value in [compute_first30_range(frame, trade_date)]
        if value is not None
    ]
    return round(float(pd.Series(values).quantile(0.60)), 4)


def metrics(filled: pd.DataFrame) -> dict[str, object]:
    if filled.empty:
        return {"fills": 0, "wins": 0, "win_rate_pct": 0.0, "net_r": 0.0, "max_drawdown_r": 0.0}
    r = pd.to_numeric(filled["r_multiple"], errors="coerce").fillna(0.0)
    equity = r.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0.0)
    wins = int(filled["outcome"].eq("TP").sum())
    return {
        "fills": len(filled),
        "wins": wins,
        "win_rate_pct": round(100 * wins / len(filled), 2),
        "net_r": round(float(r.sum()), 3),
        "max_drawdown_r": round(float(drawdown.min()), 3),
    }


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    loaded = filters.load_data()
    baseline_configs = comparison.build_active_configs(loaded)
    anchored = {
        key: anchored_q60(loaded[source])
        for key, source in comparison.SYMBOLS.items()
    }
    baseline_filled = pd.concat(
        [
            pd.read_csv(ROOT / "outputs" / "reports" / "canonical_production_full_history_research" / "checkpoints" / year / "filled.csv")
            for year in ("2025", "2026")
        ],
        ignore_index=True,
    )
    rows = [
        {
            "variant": "active_frozen_baseline",
            "changed_leg": "none",
            "threshold": "",
            **metrics(baseline_filled),
            "result_hash": "from_full_history_checkpoints",
            "runtime_seconds": 0.0,
        }
    ]
    dates = signed_market_dates(START, END)
    state = ManualStateConfig()
    for changed_leg in ("nq", "spx"):
        configs = dict(baseline_configs)
        configs[changed_leg] = replace(
            configs[changed_leg],
            first30_range_filter="live_safe_max",
            first30_range_max=anchored[changed_leg],
        )
        legs = [
            EngineLeg(key, full_history.period_frame(loaded[source], START, END), configs[key])
            for key, source in comparison.SYMBOLS.items()
        ]
        started = time.perf_counter()
        result = run_canonical_pair_pipeline(legs, dates, state_config=state)
        variant = f"{changed_leg}_pre2025_q60"
        result.decisions.to_csv(REPORT_DIR / f"{variant}_decisions.csv", index=False)
        result.filled_after_pair_cap.to_csv(REPORT_DIR / f"{variant}_filled.csv", index=False)
        row = {
            "variant": variant,
            "changed_leg": changed_leg,
            "threshold": anchored[changed_leg],
            **metrics(result.filled_after_pair_cap),
            "result_hash": stable_frame_hash(result.decisions),
            "runtime_seconds": round(time.perf_counter() - started, 3),
        }
        rows.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)
    summary = pd.DataFrame(rows)
    summary["promotion_ready"] = False
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    (REPORT_DIR / "report.md").write_text(
        "# Anchored threshold 2025+ holdout\n\n"
        "Offline research only. Each variant changes one leg to the q60 threshold calculated solely from pre-2025 data. "
        "No result is promotable without deterministic replay, valid XM session coverage, and causal execution gates.\n\n"
        "```text\n" + summary.to_string(index=False) + "\n```\n",
        encoding="utf-8",
    )
    print(f"Wrote: {REPORT_DIR}", flush=True)


if __name__ == "__main__":
    main()
