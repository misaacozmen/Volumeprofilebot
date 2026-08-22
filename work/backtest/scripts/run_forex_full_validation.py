from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_forex_research_grid as grid
from backtest.data_loader import load_ohlcv
from backtest.strategy import monthly_stats, run_backtest, trades_to_frame


RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "forex_full_validation"


@dataclass(frozen=True)
class ValidationRun:
    slug: str
    source_grid_slug: str
    symbol: str
    timeframe: str
    reward_r: float
    entry_mode: str
    stop_model: str
    trade_window_start: str
    trade_window_end: str


RUNS = [
    ValidationRun("gbpusd_5m_2p5r_start_1000_1200", "gbpusd_5m_2p5r_start_cisd_body_1000_1200", "DUKASCOPY_GBPUSD", "5m", 2.5, "start", "cisd_body", "10:00", "12:00"),
    ValidationRun("gbpusd_5m_3r_start_1000_1200", "gbpusd_5m_3r_start_cisd_body_1000_1200", "DUKASCOPY_GBPUSD", "5m", 3.0, "start", "cisd_body", "10:00", "12:00"),
    ValidationRun("gbpusd_5m_2r_start_1000_1200", "gbpusd_5m_2r_start_cisd_body_1000_1200", "DUKASCOPY_GBPUSD", "5m", 2.0, "start", "cisd_body", "10:00", "12:00"),
    ValidationRun("eurusd_5m_3r_midpoint_0930_1100", "eurusd_5m_3r_midpoint_cisd_body_0930_1100", "DUKASCOPY_EURUSD", "5m", 3.0, "midpoint", "cisd_body", "09:30", "11:00"),
    ValidationRun("eurusd_5m_2p5r_midpoint_0930_1100", "eurusd_5m_2p5r_midpoint_cisd_body_0930_1100", "DUKASCOPY_EURUSD", "5m", 2.5, "midpoint", "cisd_body", "09:30", "11:00"),
    ValidationRun("eurusd_5m_2r_midpoint_0930_1100", "eurusd_5m_2r_midpoint_cisd_body_0930_1100", "DUKASCOPY_EURUSD", "5m", 2.0, "midpoint", "cisd_body", "09:30", "11:00"),
    ValidationRun("eurusd_5m_3r_midpoint_0930_1030", "eurusd_5m_3r_midpoint_cisd_body_0930_1030", "DUKASCOPY_EURUSD", "5m", 3.0, "midpoint", "cisd_body", "09:30", "10:30"),
]


def main() -> None:
    started = time.perf_counter()
    reset_report_dir()
    loaded = load_data()
    rows: list[dict[str, object]] = []
    monthly_rows: list[pd.DataFrame] = []
    trade_rows: list[pd.DataFrame] = []

    for index, run in enumerate(RUNS, start=1):
        print(f"[{index}/{len(RUNS)}] {run.slug}")
        frame = loaded[(run.symbol, run.timeframe)].copy()
        config = grid.build_config(to_grid_run(run))
        result = run_backtest(frame, config)
        trades = trades_to_frame(result.trades)
        monthly = monthly_stats(result.trades)
        out_dir = REPORT_DIR / run.slug
        out_dir.mkdir(parents=True, exist_ok=True)
        trades.to_csv(out_dir / "trades.csv", index=False)
        monthly.to_csv(out_dir / "monthly_stats.csv", index=False)
        pd.DataFrame([config.__dict__]).to_csv(out_dir / "parameters.csv", index=False)
        rows.append(comparison_row(run, trades, monthly, out_dir))
        if not monthly.empty:
            tagged_monthly = monthly.copy()
            tagged_monthly.insert(0, "source_grid_slug", run.source_grid_slug)
            tagged_monthly.insert(0, "run_slug", run.slug)
            monthly_rows.append(tagged_monthly)
        if not trades.empty:
            tagged_trades = trades.copy()
            tagged_trades.insert(0, "source_grid_slug", run.source_grid_slug)
            tagged_trades.insert(0, "run_slug", run.slug)
            trade_rows.append(tagged_trades)

    comparison = pd.DataFrame(rows).sort_values(["net_r", "win_rate"], ascending=[False, False])
    monthly_detail = pd.concat(monthly_rows, ignore_index=True) if monthly_rows else pd.DataFrame()
    all_trades = pd.concat(trade_rows, ignore_index=True) if trade_rows else pd.DataFrame()
    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    all_trades.to_csv(REPORT_DIR / "all_trades.csv", index=False)
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
    for run in RUNS:
        key = (run.symbol, run.timeframe)
        if key not in loaded:
            loaded[key] = load_ohlcv(base_report.filter_paths(RAW_DIR, run.symbol, run.timeframe)).frame
    return loaded


def to_grid_run(run: ValidationRun) -> grid.ForexRun:
    return grid.ForexRun(
        slug=run.slug,
        symbol=run.symbol,
        timeframe=run.timeframe,
        reward_r=run.reward_r,
        entry_mode=run.entry_mode,
        stop_model=run.stop_model,
        trade_window_start=run.trade_window_start,
        trade_window_end=run.trade_window_end,
    )


def comparison_row(run: ValidationRun, trades: pd.DataFrame, monthly: pd.DataFrame, out_dir: Path) -> dict[str, object]:
    data = {
        "run_slug": run.slug,
        "source_grid_slug": run.source_grid_slug,
        "symbol": run.symbol,
        "timeframe": run.timeframe,
        "reward_r": run.reward_r,
        "entry_mode": run.entry_mode,
        "stop_model": run.stop_model,
        "trade_window": f"{run.trade_window_start}-{run.trade_window_end}",
        "avg_trades_per_month": avg_trades_per_month(trades),
        **grid.stats(trades),
        "positive_months": int((monthly["net_r"] > 0).sum()) if not monthly.empty else 0,
        "negative_months": int((monthly["net_r"] < 0).sum()) if not monthly.empty else 0,
        "out_dir": str(out_dir.relative_to(ROOT)),
    }
    data["score"] = grid.score_row(data)
    return data


def avg_trades_per_month(trades: pd.DataFrame) -> float:
    if trades.empty:
        return 0.0
    entry_dt = pd.to_datetime(trades["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    entry_months = entry_dt.dt.tz_localize(None).dt.to_period("M")
    months = (entry_months.max() - entry_months.min()).n + 1
    return round(len(trades) / max(1, months), 2)


def write_report(comparison: pd.DataFrame, monthly_detail: pd.DataFrame, runtime_seconds: float) -> None:
    lines = [
        "# Forex Full Validation",
        "",
        "Scope: full available Dukascopy forex data for the best first-pass grid runs.",
        f"Runtime: {runtime_seconds:.1f} seconds",
        "",
        "## Comparison",
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
