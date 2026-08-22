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
import run_forex_research_grid as forex_grid
from backtest.data_loader import load_ohlcv
from backtest.strategy import compute_first30_range, monthly_stats, run_backtest, trades_to_frame


RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "forex_filter_tests"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


@dataclass(frozen=True)
class Candidate:
    slug: str
    symbol: str
    timeframe: str
    reward_r: float
    entry_mode: str
    stop_model: str
    trade_window_start: str
    trade_window_end: str


@dataclass(frozen=True)
class EngineVariant:
    filter_name: str
    description: str
    setup_type: str = "all"
    direction: str = "all"
    first30_quantile: float | None = None


CANDIDATES = [
    Candidate("eurusd_5m_3r_midpoint_0930_1030", "DUKASCOPY_EURUSD", "5m", 3.0, "midpoint", "cisd_body", "09:30", "10:30"),
    Candidate("eurusd_5m_3r_midpoint_0930_1100", "DUKASCOPY_EURUSD", "5m", 3.0, "midpoint", "cisd_body", "09:30", "11:00"),
    Candidate("gbpusd_5m_3r_start_1000_1200", "DUKASCOPY_GBPUSD", "5m", 3.0, "start", "cisd_body", "10:00", "12:00"),
]

ENGINE_VARIANTS = [
    EngineVariant("baseline", "No filter"),
    EngineVariant("setup_fvg_only", "Only FVG setups", setup_type="fvg"),
    EngineVariant("setup_ifvg_only", "Only IFVG setups", setup_type="ifvg"),
    EngineVariant("long_only", "Long direction only", direction="long"),
    EngineVariant("short_only", "Short direction only", direction="short"),
    EngineVariant("first30_range_q60", "Live-safe: after 10:00 block days above first30 range 60th percentile", first30_quantile=0.60),
    EngineVariant("first30_range_q70", "Live-safe: after 10:00 block days above first30 range 70th percentile", first30_quantile=0.70),
]


def main() -> None:
    started = time.perf_counter()
    reset_report_dir()
    loaded = load_data()
    rows: list[dict[str, object]] = []
    monthly_rows: list[pd.DataFrame] = []
    trade_rows: list[pd.DataFrame] = []
    threshold_rows: list[dict[str, object]] = []

    for candidate in CANDIDATES:
        frame = loaded[(candidate.symbol, candidate.timeframe)]
        thresholds = first30_thresholds(frame)
        threshold_rows.append({"candidate": candidate.slug, **{f"first30_q{int(q * 100)}": round(v, 8) for q, v in thresholds.items()}})

        baseline_trades = pd.DataFrame()
        for variant in ENGINE_VARIANTS:
            print(f"{candidate.slug} / {variant.filter_name}")
            trades, monthly = run_engine_variant(candidate, variant, frame, thresholds)
            if variant.filter_name == "baseline":
                baseline_trades = trades.copy()
            rows.append(result_row(candidate, variant.filter_name, variant.description, "engine", trades, monthly, thresholds, variant.first30_quantile))
            monthly_rows.append(tag_monthly(candidate, variant.filter_name, "engine", monthly))
            trade_rows.append(tag_trades(candidate, variant.filter_name, "engine", trades))

        for weekday in WEEKDAYS:
            filter_name = f"exclude_{weekday.lower()}"
            filtered = exclude_weekday(baseline_trades, weekday)
            monthly = monthly_from_frame(filtered)
            rows.append(
                result_row(
                    candidate,
                    filter_name,
                    f"Post-filter: exclude {weekday}",
                    "post_trade_weekday",
                    filtered,
                    monthly,
                    thresholds,
                    None,
                )
            )
            monthly_rows.append(tag_monthly(candidate, filter_name, "post_trade_weekday", monthly))
            trade_rows.append(tag_trades(candidate, filter_name, "post_trade_weekday", filtered))

    comparison = pd.DataFrame(rows).sort_values(["candidate", "score", "net_r"], ascending=[True, False, False])
    monthly_detail = pd.concat([frame for frame in monthly_rows if not frame.empty], ignore_index=True) if monthly_rows else pd.DataFrame()
    all_trades = pd.concat([frame for frame in trade_rows if not frame.empty], ignore_index=True) if trade_rows else pd.DataFrame()
    thresholds = pd.DataFrame(threshold_rows)

    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    all_trades.to_csv(REPORT_DIR / "all_trades.csv", index=False)
    thresholds.to_csv(REPORT_DIR / "first30_thresholds.csv", index=False)
    write_report(comparison, thresholds, time.perf_counter() - started)
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
    for candidate in CANDIDATES:
        key = (candidate.symbol, candidate.timeframe)
        if key not in loaded:
            loaded[key] = load_ohlcv(base_report.filter_paths(RAW_DIR, candidate.symbol, candidate.timeframe)).frame
    return loaded


def first30_thresholds(frame: pd.DataFrame) -> dict[float, float]:
    values = []
    for trade_date in sorted(frame["date"].unique()):
        value = compute_first30_range(frame, trade_date)
        if value is not None:
            values.append(value)
    series = pd.Series(values)
    return {0.60: float(series.quantile(0.60)), 0.70: float(series.quantile(0.70))}


def run_engine_variant(
    candidate: Candidate,
    variant: EngineVariant,
    frame: pd.DataFrame,
    thresholds: dict[float, float],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    config = forex_grid.build_config(
        forex_grid.ForexRun(
            slug=candidate.slug,
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            reward_r=candidate.reward_r,
            entry_mode=candidate.entry_mode,
            stop_model=candidate.stop_model,
            trade_window_start=candidate.trade_window_start,
            trade_window_end=candidate.trade_window_end,
        )
    )
    config = replace(config, setup_type_filter=variant.setup_type, direction_filter=variant.direction)
    if variant.first30_quantile is not None:
        config = replace(
            config,
            first30_range_filter="live_safe_max",
            first30_range_max=thresholds[variant.first30_quantile],
        )
    result = run_backtest(frame.copy(), config)
    return trades_to_frame(result.trades), monthly_stats(result.trades)


def exclude_weekday(trades: pd.DataFrame, weekday: str) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    filtered = trades.copy()
    entry_dt = pd.to_datetime(filtered["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    return filtered[entry_dt.dt.day_name() != weekday].reset_index(drop=True)


def monthly_from_frame(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["month", "total_trades", "tp", "sl", "open_trades", "win_rate", "net_r", "mtm_net_r"])
    frame = trades.copy()
    entry_dt = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    frame["month"] = entry_dt.dt.strftime("%Y-%m")
    rows = []
    for month, group in frame.groupby("month"):
        wins = int((group["result"] == "win").sum())
        losses = int((group["r_multiple"] < 0).sum())
        open_trades = int((group["result"] == "open").sum())
        net_r = float(group["r_multiple"].sum())
        rows.append(
            {
                "month": month,
                "total_trades": len(group),
                "tp": wins,
                "sl": losses,
                "open_trades": open_trades,
                "win_rate": round(wins / len(group) * 100, 2),
                "net_r": round(net_r, 2),
                "mtm_net_r": round(net_r, 2),
            }
        )
    return pd.DataFrame(rows)


def result_row(
    candidate: Candidate,
    filter_name: str,
    description: str,
    filter_mode: str,
    trades: pd.DataFrame,
    monthly: pd.DataFrame,
    thresholds: dict[float, float],
    first30_quantile: float | None,
) -> dict[str, object]:
    row = {
        "candidate": candidate.slug,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "reward_r": candidate.reward_r,
        "entry_mode": candidate.entry_mode,
        "trade_window": f"{candidate.trade_window_start}-{candidate.trade_window_end}",
        "filter": filter_name,
        "filter_mode": filter_mode,
        "description": description,
        "first30_range_max": round(thresholds[first30_quantile], 8) if first30_quantile is not None else "",
        "avg_trades_per_month": avg_trades_per_month(trades),
        **forex_grid.stats(trades),
        "positive_months": int((monthly["net_r"] > 0).sum()) if not monthly.empty else 0,
        "negative_months": int((monthly["net_r"] < 0).sum()) if not monthly.empty else 0,
        "worst_month_r": round(float(monthly["net_r"].min()), 2) if not monthly.empty else 0,
    }
    row["score"] = forex_grid.score_row(row)
    return row


def avg_trades_per_month(trades: pd.DataFrame) -> float:
    if trades.empty:
        return 0.0
    entry_dt = pd.to_datetime(trades["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    months = entry_dt.dt.tz_localize(None).dt.to_period("M")
    month_count = (months.max() - months.min()).n + 1
    return round(len(trades) / max(1, month_count), 2)


def tag_monthly(candidate: Candidate, filter_name: str, filter_mode: str, monthly: pd.DataFrame) -> pd.DataFrame:
    if monthly.empty:
        return monthly
    tagged = monthly.copy()
    tagged.insert(0, "filter_mode", filter_mode)
    tagged.insert(0, "filter", filter_name)
    tagged.insert(0, "candidate", candidate.slug)
    return tagged


def tag_trades(candidate: Candidate, filter_name: str, filter_mode: str, trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    tagged = trades.copy()
    tagged.insert(0, "filter_mode", filter_mode)
    tagged.insert(0, "filter", filter_name)
    tagged.insert(0, "candidate", candidate.slug)
    return tagged


def write_report(comparison: pd.DataFrame, thresholds: pd.DataFrame, runtime_seconds: float) -> None:
    best = comparison.groupby("candidate", group_keys=False).apply(
        lambda group: group.sort_values(["score", "net_r"], ascending=[False, False]).head(6)
    )
    lines = [
        "# Forex Filter Tests",
        "",
        "Scope: diagnostic filter tests for the strongest EURUSD/GBPUSD 5m research runs.",
        f"Runtime: {runtime_seconds:.1f} seconds",
        "",
        "Caveat: first30 thresholds are selected from the available history, so they are diagnostic until validated out-of-sample.",
        "Weekday exclusions are post-trade filters, equivalent to disabling that full weekday for these max-1-trade/day runs.",
        "",
        "## First30 Thresholds",
        "",
        base_report.markdown_table(thresholds),
        "",
        "## Best Per Candidate",
        "",
        base_report.markdown_table(best),
        "",
        "## Full Comparison",
        "",
        base_report.markdown_table(comparison),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
