from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
from backtest.config import SYMBOL_CONFIGS, SymbolConfig
from backtest.data_loader import load_ohlcv
from backtest.strategy import monthly_stats, run_backtest, trades_to_frame


RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "forex_research_grid"
MONTHS = ["2022-12", "2023-06", "2023-09", "2023-10", "2025-02", "2025-03"]
SYMBOLS = ["DUKASCOPY_EURUSD", "DUKASCOPY_GBPUSD"]
TIMEFRAMES = ["3m", "5m"]
REWARD_RS = [2.0, 2.5, 3.0]
ENTRY_MODES = ["start", "midpoint"]
STOP_MODELS = ["cisd_body"]
TRADE_WINDOWS = [
    ("09:30", "10:30"),
    ("09:30", "11:00"),
    ("09:30", "12:00"),
    ("10:00", "12:00"),
]


@dataclass(frozen=True)
class ForexRun:
    slug: str
    symbol: str
    timeframe: str
    reward_r: float
    entry_mode: str
    stop_model: str
    trade_window_start: str
    trade_window_end: str


def main() -> None:
    started = time.perf_counter()
    reset_report_dir()
    loaded = load_data()
    runs = build_runs()

    comparison_rows: list[dict[str, object]] = []
    monthly_rows: list[pd.DataFrame] = []
    all_trades: list[pd.DataFrame] = []

    for index, run in enumerate(runs, start=1):
        print(f"[{index}/{len(runs)}] {run.slug}")
        frame = base_report.select_month_windows(loaded[(run.symbol, run.timeframe)].copy(), MONTHS)
        frame["date"] = frame["time"].dt.date
        config = build_config(run)
        result = run_backtest(frame, config)
        trades = [trade for trade in result.trades if trade.date[:7] in MONTHS]
        trades_df = trades_to_frame(trades)
        monthly_df = monthly_stats(trades)

        out_dir = REPORT_DIR / run.slug
        out_dir.mkdir(parents=True, exist_ok=True)
        trades_df.to_csv(out_dir / "trades.csv", index=False)
        monthly_df.to_csv(out_dir / "monthly_stats.csv", index=False)
        pd.DataFrame([config.__dict__]).to_csv(out_dir / "parameters.csv", index=False)

        comparison_rows.append(comparison_row(run, trades_df, monthly_df, out_dir))
        if not monthly_df.empty:
            tagged_monthly = monthly_df.copy()
            tagged_monthly.insert(0, "run_slug", run.slug)
            tagged_monthly.insert(0, "run_symbol", run.symbol)
            tagged_monthly.insert(0, "run_timeframe", run.timeframe)
            monthly_rows.append(tagged_monthly)
        if not trades_df.empty:
            tagged_trades = trades_df.copy()
            tagged_trades.insert(0, "run_slug", run.slug)
            tagged_trades.insert(0, "run_symbol", run.symbol)
            tagged_trades.insert(0, "run_timeframe", run.timeframe)
            all_trades.append(tagged_trades)

    comparison = pd.DataFrame(comparison_rows).sort_values(
        ["symbol", "timeframe", "net_r", "win_rate"], ascending=[True, True, False, False]
    )
    monthly_detail = pd.concat(monthly_rows, ignore_index=True) if monthly_rows else pd.DataFrame()
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()

    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    trades.to_csv(REPORT_DIR / "all_trades.csv", index=False)
    write_report(comparison, monthly_detail, time.perf_counter() - started)
    print(f"Wrote: {REPORT_DIR}")
    print(f"Runtime seconds: {time.perf_counter() - started:.1f}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_data() -> dict[tuple[str, str], pd.DataFrame]:
    loaded: dict[tuple[str, str], pd.DataFrame] = {}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            loaded[(symbol, timeframe)] = load_ohlcv(base_report.filter_paths(RAW_DIR, symbol, timeframe)).frame
    return loaded


def build_runs() -> list[ForexRun]:
    runs: list[ForexRun] = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            for reward_r in REWARD_RS:
                for entry_mode in ENTRY_MODES:
                    for stop_model in STOP_MODELS:
                        for start, end in TRADE_WINDOWS:
                            slug = "_".join(
                                [
                                    symbol.replace("DUKASCOPY_", "").lower(),
                                    timeframe,
                                    f"{reward_r:g}r".replace(".", "p"),
                                    entry_mode,
                                    stop_model,
                                    start.replace(":", ""),
                                    end.replace(":", ""),
                                ]
                            )
                            runs.append(
                                ForexRun(
                                    slug=slug,
                                    symbol=symbol,
                                    timeframe=timeframe,
                                    reward_r=reward_r,
                                    entry_mode=entry_mode,
                                    stop_model=stop_model,
                                    trade_window_start=start,
                                    trade_window_end=end,
                                )
                            )
    return runs


def build_config(run: ForexRun) -> SymbolConfig:
    config = SYMBOL_CONFIGS[(run.symbol, run.timeframe)]
    return replace(
        config,
        reward_r=run.reward_r,
        max_trades_per_day=1,
        allowed_weekdays="Monday,Tuesday,Wednesday,Thursday,Friday",
        setup_type_filter="all",
        direction_filter="all",
        fvg_entry_mode=run.entry_mode,
        stop_model=run.stop_model,
        stop_management="none",
        session_liquidity_only=False,
        swing_liquidity_mode="all",
        trade_window_start=run.trade_window_start,
        trade_window_end=run.trade_window_end,
        latest_entry_time=None,
    )


def comparison_row(run: ForexRun, trades: pd.DataFrame, monthly: pd.DataFrame, out_dir: Path) -> dict[str, object]:
    result = {
        "run_slug": run.slug,
        "symbol": run.symbol,
        "timeframe": run.timeframe,
        "reward_r": run.reward_r,
        "entry_mode": run.entry_mode,
        "stop_model": run.stop_model,
        "trade_window": f"{run.trade_window_start}-{run.trade_window_end}",
        "months_tested": len(MONTHS),
        "avg_trades_per_month": round(len(trades) / len(MONTHS), 2),
        **stats(trades),
        "positive_months": int((monthly["net_r"] > 0).sum()) if not monthly.empty else 0,
        "negative_months": int((monthly["net_r"] < 0).sum()) if not monthly.empty else 0,
        "out_dir": str(out_dir.relative_to(ROOT)),
    }
    result["score"] = score_row(result)
    return result


def stats(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net_r": 0.0,
            "max_drawdown_r": 0.0,
            "profit_factor": 0.0,
            "net_r_per_dd": "",
        }
    ordered = trades.copy()
    ordered["entry_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    ordered = ordered.sort_values("entry_dt")
    wins = int((ordered["result"] == "win").sum())
    losses = int((ordered["r_multiple"] < 0).sum())
    net_r = float(ordered["r_multiple"].sum())
    equity = ordered["r_multiple"].cumsum()
    drawdown = float((equity - equity.cummax()).min())
    gross_win = float(ordered.loc[ordered["r_multiple"] > 0, "r_multiple"].sum())
    gross_loss = abs(float(ordered.loc[ordered["r_multiple"] < 0, "r_multiple"].sum()))
    return {
        "trades": len(ordered),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(ordered) * 100, 2),
        "net_r": round(net_r, 2),
        "max_drawdown_r": round(drawdown, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else 0.0,
        "net_r_per_dd": round(net_r / abs(drawdown), 2) if drawdown else "",
    }


def score_row(row: dict[str, object]) -> float:
    trades = float(row["trades"])
    net_r = float(row["net_r"])
    win_rate = float(row["win_rate"])
    profit_factor = float(row["profit_factor"])
    drawdown = abs(float(row["max_drawdown_r"]))
    if trades < 6:
        return -999.0
    return round(net_r * 2.0 + win_rate * 0.15 + profit_factor * 5.0 - drawdown * 1.5, 2)


def write_report(comparison: pd.DataFrame, monthly_detail: pd.DataFrame, runtime_seconds: float) -> None:
    viable = comparison[(comparison["trades"] >= 6) & (comparison["net_r"] > 0)].copy()
    top = viable.sort_values(["score", "net_r", "win_rate"], ascending=[False, False, False]).head(20)
    by_symbol = (
        comparison.groupby(["symbol", "timeframe"])
        .agg(
            runs=("run_slug", "count"),
            best_net_r=("net_r", "max"),
            best_win_rate=("win_rate", "max"),
            positive_runs=("net_r", lambda series: int((series > 0).sum())),
            avg_trades=("trades", "mean"),
        )
        .reset_index()
    )

    lines = [
        "# Forex Research Grid",
        "",
        "Scope: first-pass forex research branch, not active candidate selection.",
        f"Months tested: {', '.join(MONTHS)}",
        f"Runtime: {runtime_seconds:.1f} seconds",
        "",
        "Fixed assumptions:",
        "",
        "- Symbols: DUKASCOPY_EURUSD, DUKASCOPY_GBPUSD",
        "- Timeframes: 3m, 5m",
        "- Session: NY only, no Asia trades",
        "- Weekdays: Monday-Friday",
        "- Max trades/day: 1",
        "- Setup types: FVG + IFVG",
        "- Stop model: CISD body",
        "",
        "Tested grid:",
        "",
        "- RR: 2R, 2.5R, 3R",
        "- Entry: FVG/IFVG start, midpoint",
        "- Windows: 09:30-10:30, 09:30-11:00, 09:30-12:00, 10:00-12:00 NY",
        "",
        "## Best Viable Runs",
        "",
        base_report.markdown_table(top) if not top.empty else "No positive viable runs.",
        "",
        "## Symbol/Timeframe Overview",
        "",
        base_report.markdown_table(by_symbol),
        "",
        "## Full Comparison",
        "",
        base_report.markdown_table(comparison),
        "",
        "## Monthly Detail",
        "",
        base_report.markdown_table(monthly_detail) if not monthly_detail.empty else "No trades.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
