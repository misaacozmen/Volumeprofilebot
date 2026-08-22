from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_corrected_engine_3month_report as base_report
from backtest.config import SYMBOL_CONFIGS
from backtest.data_loader import load_ohlcv
from backtest.strategy import monthly_stats, run_backtest, summarize_trades


REPORT_DIR = ROOT / "outputs" / "reports" / "index_time_window_followup_6month"
SEED = 20260705
SELECTED_MONTH_COUNT = 6
TRADE_WINDOWS = ["09:30-10:30", "09:30-11:00", "10:00-11:30"]


@dataclass(frozen=True)
class FocusConfig:
    slug: str
    label: str
    reward_r: float
    entry_mode: str
    liquidity_mode: str


FOCUS_CONFIGS = [
    FocusConfig("nq_fvg_opposite_edge_3r", "NQ WR pick", 2.5, "midpoint", "session_only"),
    FocusConfig("nq_fvg_opposite_edge_3r", "NQ net-R pick", 3.0, "start", "strong_swing"),
    FocusConfig("nq_fvg_opposite_edge_3r", "NQ balanced high-R", 3.0, "midpoint", "session_only"),
    FocusConfig("spx_fvg_opposite_edge_3r", "SPX WR pick", 2.0, "midpoint", "all"),
    FocusConfig("spx_fvg_opposite_edge_3r", "SPX net-R pick", 2.5, "start", "all"),
    FocusConfig("spx_fvg_opposite_edge_3r", "SPX high-R pick", 3.0, "quarter_25", "all"),
]


def main() -> None:
    reset_report_dir()
    base_report.SEED = SEED
    base_report.SELECTED_MONTH_COUNT = SELECTED_MONTH_COUNT
    months = base_report.choose_months()
    candidates = {candidate.slug: candidate for candidate in base_report.CANDIDATES}
    needed = {config.slug for config in FOCUS_CONFIGS}
    loaded = {
        slug: load_ohlcv(base_report.filter_paths(base_report.RAW_DIR, candidates[slug].symbol, candidates[slug].timeframe)).frame
        for slug in needed
    }
    rows = []
    monthly_rows = []
    for focus in FOCUS_CONFIGS:
        candidate = candidates[focus.slug]
        frame = base_report.select_month_windows(loaded[focus.slug].copy(), months)
        frame["date"] = frame["time"].dt.date
        for trade_window in TRADE_WINDOWS:
            config = build_config(candidate, focus, trade_window)
            trades = [trade for trade in run_backtest(frame, config).trades if trade.date[:7] in months]
            summary = summarize_trades(trades).iloc[0].to_dict()
            monthly = monthly_stats(trades)
            row = build_row(candidate, focus, trade_window, summary, monthly)
            rows.append(row)
            if not monthly.empty:
                monthly.insert(0, "run_slug", row["run_slug"])
                monthly.insert(0, "candidate_slug", candidate.slug)
                monthly_rows.append(monthly)

    comparison = pd.DataFrame(rows).sort_values(
        ["candidate_slug", "label", "win_rate", "net_r"],
        ascending=[True, True, False, False],
    )
    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    if monthly_rows:
        pd.concat(monthly_rows, ignore_index=True).to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    write_report(months, comparison)
    print(f"Selected months: {', '.join(months)}")
    print(f"Runs: {len(comparison)}")
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_config(candidate: base_report.Candidate, focus: FocusConfig, trade_window: str):
    start, end = trade_window.split("-")
    config = SYMBOL_CONFIGS[(candidate.symbol, candidate.timeframe)]
    return replace(
        config,
        reward_r=focus.reward_r,
        max_trades_per_day=candidate.max_trades_per_day,
        allowed_weekdays=candidate.weekdays,
        setup_type_filter=candidate.setup_type,
        direction_filter=candidate.direction,
        fvg_entry_mode=focus.entry_mode,
        stop_model="cisd_body",
        stop_management="none",
        session_liquidity_only=focus.liquidity_mode == "session_only",
        swing_liquidity_mode="strong_only" if focus.liquidity_mode == "strong_swing" else "all",
        trade_window_start=start,
        trade_window_end=end,
    )


def build_row(candidate: base_report.Candidate, focus: FocusConfig, trade_window: str, summary: dict, monthly: pd.DataFrame) -> dict:
    max_dd = float(summary.get("max_drawdown_r", 0) or 0)
    net_r = float(summary.get("net_r", 0) or 0)
    run_slug = f"{focus.slug}_{focus.label.lower().replace(' ', '_')}_{trade_window.replace(':', '').replace('-', '_')}"
    return {
        "run_slug": run_slug,
        "label": focus.label,
        "candidate_slug": candidate.slug,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "reward_r": focus.reward_r,
        "entry_mode": focus.entry_mode,
        "stop_model": "cisd_body",
        "liquidity_mode": focus.liquidity_mode,
        "trade_window": trade_window,
        "trades": int(summary.get("total_trades", 0) or 0),
        "wins": int(summary.get("wins", 0) or 0),
        "losses": int(summary.get("losses", 0) or 0),
        "win_rate": float(summary.get("win_rate", 0) or 0),
        "net_r": net_r,
        "max_drawdown_r": max_dd,
        "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
        "profit_factor": float(summary.get("profit_factor", 0) or 0),
        "positive_months": int((monthly["net_r"] > 0).sum()) if not monthly.empty else 0,
        "negative_months": int((monthly["net_r"] < 0).sum()) if not monthly.empty else 0,
    }


def write_report(months: list[str], comparison: pd.DataFrame) -> None:
    lines = [
        "# Index Time Window Follow-Up 6-Month Test",
        "",
        f"Random seed: `{SEED}`",
        f"Selected months: {', '.join(months)}",
        "",
        base_report.markdown_table(comparison),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
