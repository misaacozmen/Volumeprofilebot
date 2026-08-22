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
from backtest.strategy import monthly_stats, run_backtest, summarize_trades, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "phase_candidate_grid_6month"
SEED = 20260705
SELECTED_MONTH_COUNT = 6


@dataclass(frozen=True)
class PhaseRun:
    candidate: base_report.Candidate
    reward_r: float
    entry_mode: str
    liquidity_mode: str
    trade_window: str
    max_trades_per_day: int


INDEX_SLUGS = {"nq_fvg_opposite_edge_3r", "spx_fvg_opposite_edge_3r"}
ENTRY_MODES = ["start", "quarter_25"]
LIQUIDITY_MODES = ["all", "strong_swing"]
TRADE_WINDOWS = ["09:30-11:00", "09:30-12:00"]
REWARD_RS = [2.0, 2.5, 3.0]
MAX_TRADES_BY_SLUG = {
    "nq_fvg_opposite_edge_3r": [1, 2],
    "spx_fvg_opposite_edge_3r": [2],
}


def main() -> None:
    reset_report_dir()
    base_report.SEED = SEED
    base_report.SELECTED_MONTH_COUNT = SELECTED_MONTH_COUNT
    months = base_report.choose_months()
    candidates = [candidate for candidate in base_report.CANDIDATES if candidate.slug in INDEX_SLUGS]
    loaded = {
        (candidate.symbol, candidate.timeframe): load_ohlcv(
            base_report.filter_paths(base_report.RAW_DIR, candidate.symbol, candidate.timeframe)
        ).frame
        for candidate in candidates
    }
    rows: list[dict[str, object]] = []
    monthly_rows: list[pd.DataFrame] = []
    best_trade_frames: list[pd.DataFrame] = []

    for candidate in candidates:
        source = loaded[(candidate.symbol, candidate.timeframe)]
        frame = base_report.select_month_windows(source.copy(), months)
        frame["date"] = frame["time"].dt.date
        for run in build_runs(candidate):
            config = build_config(run)
            trades = [trade for trade in run_backtest(frame, config).trades if trade.date[:7] in months]
            summary = summarize_trades(trades).iloc[0].to_dict()
            monthly = monthly_stats(trades)
            row = build_row(run, summary, monthly)
            rows.append(row)
            if not monthly.empty:
                monthly.insert(0, "run_slug", row["run_slug"])
                monthly.insert(0, "candidate_slug", candidate.slug)
                monthly_rows.append(monthly)

    comparison = pd.DataFrame(rows).sort_values(
        ["candidate_slug", "phase_score", "net_r", "win_rate"],
        ascending=[True, False, False, False],
    ).reset_index(drop=True)
    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    if monthly_rows:
        pd.concat(monthly_rows, ignore_index=True).to_csv(REPORT_DIR / "monthly_detail.csv", index=False)

    for candidate in candidates:
        best = choose_phase_best(comparison[comparison["candidate_slug"] == candidate.slug])
        if best is None:
            continue
        source = loaded[(candidate.symbol, candidate.timeframe)]
        frame = base_report.select_month_windows(source.copy(), months)
        frame["date"] = frame["time"].dt.date
        trades = [trade for trade in run_backtest(frame, build_config_from_row(candidate, best)).trades if trade.date[:7] in months]
        trades_df = trades_to_frame(trades)
        if not trades_df.empty:
            trades_df.insert(0, "run_slug", best["run_slug"])
            trades_df.insert(0, "candidate_slug", candidate.slug)
            best_trade_frames.append(trades_df)

    if best_trade_frames:
        pd.concat(best_trade_frames, ignore_index=True).to_csv(REPORT_DIR / "phase_best_trades.csv", index=False)

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


def build_runs(candidate: base_report.Candidate) -> list[PhaseRun]:
    return [
        PhaseRun(candidate, rr, entry_mode, liquidity_mode, trade_window, max_trades)
        for rr in REWARD_RS
        for entry_mode in ENTRY_MODES
        for liquidity_mode in LIQUIDITY_MODES
        for trade_window in TRADE_WINDOWS
        for max_trades in MAX_TRADES_BY_SLUG[candidate.slug]
    ]


def build_config(run: PhaseRun):
    start, end = run.trade_window.split("-")
    candidate = run.candidate
    config = SYMBOL_CONFIGS[(candidate.symbol, candidate.timeframe)]
    return replace(
        config,
        reward_r=run.reward_r,
        max_trades_per_day=run.max_trades_per_day,
        allowed_weekdays=candidate.weekdays,
        setup_type_filter=candidate.setup_type,
        direction_filter=candidate.direction,
        fvg_entry_mode=run.entry_mode,
        stop_model="cisd_body",
        stop_management="none",
        session_liquidity_only=False,
        swing_liquidity_mode="strong_only" if run.liquidity_mode == "strong_swing" else "all",
        trade_window_start=start,
        trade_window_end=end,
    )


def build_config_from_row(candidate: base_report.Candidate, row: pd.Series):
    run = PhaseRun(
        candidate=candidate,
        reward_r=float(row["reward_r"]),
        entry_mode=str(row["entry_mode"]),
        liquidity_mode=str(row["liquidity_mode"]),
        trade_window=str(row["trade_window"]),
        max_trades_per_day=int(row["max_trades_per_day"]),
    )
    return build_config(run)


def build_row(run: PhaseRun, summary: dict[str, object], monthly: pd.DataFrame) -> dict[str, object]:
    candidate = run.candidate
    trades = int(summary.get("total_trades", 0) or 0)
    wins = int(summary.get("wins", 0) or 0)
    net_r = float(summary.get("net_r", 0) or 0)
    max_dd = float(summary.get("max_drawdown_r", 0) or 0)
    win_rate = float(summary.get("win_rate", 0) or 0)
    profit_factor = float(summary.get("profit_factor", 0) or 0)
    avg_trades = round(trades / SELECTED_MONTH_COUNT, 2)
    run_slug = (
        f"{candidate.slug}_phase_rr{format_rr(run.reward_r)}_{run.entry_mode}_"
        f"{run.liquidity_mode}_{run.trade_window.replace(':', '').replace('-', '_')}_max{run.max_trades_per_day}"
    )
    return {
        "run_slug": run_slug,
        "candidate": candidate.name,
        "candidate_slug": candidate.slug,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "reward_r": run.reward_r,
        "entry_mode": run.entry_mode,
        "stop_model": "cisd_body",
        "liquidity_mode": run.liquidity_mode,
        "trade_window": run.trade_window,
        "max_trades_per_day": run.max_trades_per_day,
        "trades": trades,
        "avg_trades_per_month": avg_trades,
        "wins": wins,
        "losses": int(summary.get("losses", 0) or 0),
        "win_rate": win_rate,
        "net_r": net_r,
        "max_drawdown_r": max_dd,
        "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
        "profit_factor": profit_factor,
        "positive_months": int((monthly["net_r"] > 0).sum()) if not monthly.empty else 0,
        "negative_months": int((monthly["net_r"] < 0).sum()) if not monthly.empty else 0,
        "phase_score": phase_score(trades, avg_trades, win_rate, net_r, max_dd, profit_factor),
    }


def phase_score(trades: int, avg_trades: float, win_rate: float, net_r: float, max_dd: float, profit_factor: float) -> float:
    if trades < 30 or net_r <= 0:
        return -9999 + trades
    trade_bonus = min(avg_trades, 9.0) * 2
    return net_r * 2 + win_rate + profit_factor * 5 + trade_bonus - abs(max_dd) * 1.5


def choose_phase_best(frame: pd.DataFrame) -> pd.Series | None:
    if frame.empty:
        return None
    viable = frame[(frame["trades"] >= 30) & (frame["net_r"] > 0)].copy()
    if viable.empty:
        viable = frame.copy()
    return viable.sort_values(["phase_score", "net_r", "win_rate"], ascending=[False, False, False]).iloc[0]


def format_rr(value: float) -> str:
    return str(value).rstrip("0").rstrip(".").replace(".", "p")


def write_report(months: list[str], comparison: pd.DataFrame) -> None:
    lines = [
        "# Phase Candidate Grid 6-Month Test",
        "",
        f"Random seed: `{SEED}`",
        f"Selected months: {', '.join(months)}",
        "",
        "Goal: find higher-frequency NQ/SPX candidates for phase passing, separate from lower-frequency funded-account candidates.",
        "",
        "Grid:",
        "",
        f"- RR: {', '.join(str(item) for item in REWARD_RS)}",
        f"- Entry modes: {', '.join(ENTRY_MODES)}",
        "- Stop model: cisd_body",
        f"- Liquidity modes: {', '.join(LIQUIDITY_MODES)}",
        f"- Trade windows: {', '.join(TRADE_WINDOWS)}",
        "- Max trades/day: NQ 1 and 2; SPX 2",
        "",
    ]
    for candidate_slug, subset in comparison.groupby("candidate_slug", sort=False):
        lines.extend(
            [
                f"## {candidate_slug}",
                "",
                "Top phase-score rows:",
                "",
                base_report.markdown_table(subset.head(12)),
                "",
                "Rows with >=48 trades in 6 months:",
                "",
                base_report.markdown_table(
                    subset[subset["trades"] >= 48]
                    .sort_values(["phase_score", "net_r", "win_rate"], ascending=[False, False, False])
                    .head(12)
                ),
                "",
            ]
        )
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
