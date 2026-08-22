from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_corrected_engine_3month_report as base_report
from backtest.config import SYMBOL_CONFIGS, SymbolConfig
from backtest.data_loader import load_ohlcv
from backtest.strategy import monthly_stats, run_backtest, summarize_trades, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "index_focus_grid_6month"
SEED = 20260705
SELECTED_MONTH_COUNT = 6


@dataclass(frozen=True)
class GridRun:
    candidate: base_report.Candidate
    reward_r: float
    entry_mode: str
    stop_model: str
    liquidity_mode: str
    trade_window: str


INDEX_CANDIDATES = [candidate for candidate in base_report.CANDIDATES if candidate.slug in {
    "nq_fvg_opposite_edge_3r",
    "spx_fvg_opposite_edge_3r",
}]
ENTRY_MODES = ["start", "quarter_25", "midpoint"]
STOP_MODELS = ["cisd_body"]
LIQUIDITY_MODES = ["all", "session_only", "strong_swing"]
TRADE_WINDOWS = ["09:30-11:00"]
REWARD_RS = [2.0, 2.5, 3.0]


def main() -> None:
    reset_report_dir()
    base_report.SEED = SEED
    base_report.SELECTED_MONTH_COUNT = SELECTED_MONTH_COUNT
    months = base_report.choose_months()
    rows: list[dict[str, object]] = []
    monthly_rows: list[pd.DataFrame] = []
    best_trade_frames: list[pd.DataFrame] = []

    loaded = {
        (candidate.symbol, candidate.timeframe): load_ohlcv(
            base_report.filter_paths(base_report.RAW_DIR, candidate.symbol, candidate.timeframe)
        ).frame
        for candidate in INDEX_CANDIDATES
    }

    for candidate in INDEX_CANDIDATES:
        source = loaded[(candidate.symbol, candidate.timeframe)]
        frame = base_report.select_month_windows(source.copy(), months)
        frame["date"] = frame["time"].dt.date
        for grid_run in build_runs(candidate):
            config = build_grid_config(grid_run)
            result = run_backtest(frame, config)
            trades = [trade for trade in result.trades if trade.date[:7] in months]
            summary = summarize_trades(trades).iloc[0].to_dict()
            monthly = monthly_stats(trades)
            row = build_row(grid_run, summary, monthly)
            rows.append(row)
            if not monthly.empty:
                monthly.insert(0, "run_slug", row["run_slug"])
                monthly.insert(0, "candidate_slug", candidate.slug)
                monthly_rows.append(monthly)

    comparison = pd.DataFrame(rows).sort_values(
        ["candidate_slug", "win_rate", "net_r", "max_drawdown_r"],
        ascending=[True, False, False, False],
    ).reset_index(drop=True)
    monthly_detail = pd.concat(monthly_rows, ignore_index=True) if monthly_rows else pd.DataFrame()
    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    write_report(months, comparison)

    for candidate in INDEX_CANDIDATES:
        best = choose_balanced_best(comparison[comparison["candidate_slug"] == candidate.slug])
        if best is None:
            continue
        config = build_config_from_row(candidate, best)
        frame = base_report.select_month_windows(loaded[(candidate.symbol, candidate.timeframe)].copy(), months)
        frame["date"] = frame["time"].dt.date
        trades = [trade for trade in run_backtest(frame, config).trades if trade.date[:7] in months]
        trades_df = trades_to_frame(trades)
        if not trades_df.empty:
            trades_df.insert(0, "run_slug", best["run_slug"])
            trades_df.insert(0, "candidate_slug", candidate.slug)
            best_trade_frames.append(trades_df)

    if best_trade_frames:
        pd.concat(best_trade_frames, ignore_index=True).to_csv(REPORT_DIR / "balanced_best_trades.csv", index=False)

    print(f"Selected months: {', '.join(months)}")
    print(f"Grid runs: {len(comparison)}")
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_runs(candidate: base_report.Candidate) -> list[GridRun]:
    return [
        GridRun(candidate, rr, entry_mode, stop_model, liquidity_mode, trade_window)
        for rr in REWARD_RS
        for entry_mode in ENTRY_MODES
        for stop_model in STOP_MODELS
        for liquidity_mode in LIQUIDITY_MODES
        for trade_window in TRADE_WINDOWS
    ]


def build_grid_config(grid_run: GridRun) -> SymbolConfig:
    candidate = grid_run.candidate
    start, end = grid_run.trade_window.split("-")
    config = SYMBOL_CONFIGS[(candidate.symbol, candidate.timeframe)]
    return replace(
        config,
        reward_r=grid_run.reward_r,
        max_trades_per_day=candidate.max_trades_per_day,
        allowed_weekdays=candidate.weekdays,
        setup_type_filter=candidate.setup_type,
        direction_filter=candidate.direction,
        fvg_entry_mode=grid_run.entry_mode,
        stop_model=grid_run.stop_model,
        stop_management="none",
        session_liquidity_only=grid_run.liquidity_mode == "session_only",
        swing_liquidity_mode="strong_only" if grid_run.liquidity_mode == "strong_swing" else "all",
        trade_window_start=start,
        trade_window_end=end,
    )


def build_config_from_row(candidate: base_report.Candidate, row: pd.Series) -> SymbolConfig:
    start, end = str(row["trade_window"]).split("-")
    config = SYMBOL_CONFIGS[(candidate.symbol, candidate.timeframe)]
    liquidity_mode = str(row["liquidity_mode"])
    return replace(
        config,
        reward_r=float(row["reward_r"]),
        max_trades_per_day=candidate.max_trades_per_day,
        allowed_weekdays=candidate.weekdays,
        setup_type_filter=candidate.setup_type,
        direction_filter=candidate.direction,
        fvg_entry_mode=str(row["entry_mode"]),
        stop_model=str(row["stop_model"]),
        stop_management="none",
        session_liquidity_only=liquidity_mode == "session_only",
        swing_liquidity_mode="strong_only" if liquidity_mode == "strong_swing" else "all",
        trade_window_start=start,
        trade_window_end=end,
    )


def build_row(grid_run: GridRun, summary: dict[str, object], monthly: pd.DataFrame) -> dict[str, object]:
    candidate = grid_run.candidate
    max_dd = float(summary.get("max_drawdown_r", 0) or 0)
    net_r = float(summary.get("net_r", 0) or 0)
    trades = int(summary.get("total_trades", 0) or 0)
    run_slug = (
        f"{candidate.slug}_rr{format_rr(grid_run.reward_r)}_{grid_run.entry_mode}_"
        f"{grid_run.stop_model}_{grid_run.liquidity_mode}_{grid_run.trade_window.replace(':', '').replace('-', '_')}"
    )
    return {
        "run_slug": run_slug,
        "candidate": candidate.name,
        "candidate_slug": candidate.slug,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "reward_r": grid_run.reward_r,
        "entry_mode": grid_run.entry_mode,
        "stop_model": grid_run.stop_model,
        "liquidity_mode": grid_run.liquidity_mode,
        "trade_window": grid_run.trade_window,
        "trades": trades,
        "wins": int(summary.get("wins", 0) or 0),
        "losses": int(summary.get("losses", 0) or 0),
        "win_rate": float(summary.get("win_rate", 0) or 0),
        "net_r": net_r,
        "max_drawdown_r": max_dd,
        "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
        "profit_factor": float(summary.get("profit_factor", 0) or 0),
        "positive_months": int((monthly["net_r"] > 0).sum()) if not monthly.empty else 0,
        "negative_months": int((monthly["net_r"] < 0).sum()) if not monthly.empty else 0,
        "avg_trades_per_month": round(trades / SELECTED_MONTH_COUNT, 2),
    }


def choose_balanced_best(frame: pd.DataFrame) -> pd.Series | None:
    if frame.empty:
        return None
    viable = frame[(frame["trades"] >= 8) & (frame["net_r"] > 0)].copy()
    if viable.empty:
        viable = frame[frame["trades"] >= 8].copy()
    if viable.empty:
        viable = frame.copy()
    viable["score"] = viable["win_rate"] + viable["net_r"] * 2 + viable["profit_factor"] * 5 - viable["max_drawdown_r"].abs()
    return viable.sort_values(["score", "win_rate", "net_r"], ascending=[False, False, False]).iloc[0]


def format_rr(value: float) -> str:
    return str(value).rstrip("0").rstrip(".").replace(".", "p")


def write_report(months: list[str], comparison: pd.DataFrame) -> None:
    lines = [
        "# Index Focus Grid 6-Month Test",
        "",
        f"Random seed: `{SEED}`",
        f"Selected months: {', '.join(months)}",
        "",
        "Grid:",
        "",
        f"- Candidates: {', '.join(candidate.slug for candidate in INDEX_CANDIDATES)}",
        f"- RR: {', '.join(str(item) for item in REWARD_RS)}",
        f"- Entry modes: {', '.join(ENTRY_MODES)}",
        f"- Stop models: {', '.join(STOP_MODELS)}",
        f"- Liquidity modes: {', '.join(LIQUIDITY_MODES)}",
        f"- Trade windows: {', '.join(TRADE_WINDOWS)}",
        "",
    ]
    for candidate in INDEX_CANDIDATES:
        subset = comparison[comparison["candidate_slug"] == candidate.slug]
        lines.extend(
            [
                f"## {candidate.name}",
                "",
                "Top by win rate:",
                "",
                base_report.markdown_table(subset.head(12)),
                "",
                "Top positive net R with >=8 trades:",
                "",
                base_report.markdown_table(
                    subset[(subset["net_r"] > 0) & (subset["trades"] >= 8)]
                    .sort_values(["win_rate", "net_r"], ascending=[False, False])
                    .head(12)
                ),
                "",
                "Balanced pick:",
                "",
                base_report.markdown_table(pd.DataFrame([choose_balanced_best(subset)])),
                "",
            ]
        )
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
