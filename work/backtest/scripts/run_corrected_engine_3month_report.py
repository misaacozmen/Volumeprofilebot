from __future__ import annotations

import html
import random
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.config import SYMBOL_CONFIGS, SymbolConfig
from backtest.data_inspector import infer_symbol_timeframe
from backtest.data_loader import load_ohlcv
from backtest.strategy import monthly_stats, run_backtest, summarize_trades, trades_to_frame, weekday_stats


RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "corrected_engine_3month"
SEED = 20260705
SELECTED_MONTH_COUNT = 3


@dataclass(frozen=True)
class Candidate:
    slug: str
    name: str
    symbol: str
    timeframe: str
    reward_r: float
    max_trades_per_day: int
    weekdays: str
    setup_type: str = "all"
    direction: str = "all"
    entry_mode: str = "start"
    stop_model: str = "fvg_opposite_edge"
    session_liquidity_only: bool = False
    swing_liquidity_mode: str = "all"
    trade_window_start: str = "09:30"
    trade_window_end: str = "12:00"


CANDIDATES = [
    Candidate(
        slug="nq_fvg_opposite_edge_3r",
        name="NQ - 3m - Tuesday haric - FVG opposite edge - 3R",
        symbol="DUKASCOPY_USATECHIDXUSD",
        timeframe="3m",
        reward_r=3.0,
        max_trades_per_day=1,
        weekdays="Monday,Wednesday,Thursday,Friday",
    ),
    Candidate(
        slug="spx_fvg_opposite_edge_3r",
        name="SPX - 5m - Tue/Wed/Fri - FVG - opposite edge - 3R",
        symbol="DUKASCOPY_USA500IDXUSD",
        timeframe="5m",
        reward_r=3.0,
        max_trades_per_day=2,
        weekdays="Tuesday,Wednesday,Friday",
        setup_type="fvg",
    ),
    Candidate(
        slug="silver_fvg_opposite_edge_2r",
        name="Silver - 5m - Tue/Wed/Thu - FVG opposite edge - 2R",
        symbol="DUKASCOPY_XAGUSD",
        timeframe="5m",
        reward_r=2.0,
        max_trades_per_day=1,
        weekdays="Tuesday,Wednesday,Thursday",
    ),
    Candidate(
        slug="nq_strong_swing_cisd_body_3r_0930_1030",
        name="NQ - 3m - strong swing - CISD body - 3R - 09:30-10:30",
        symbol="DUKASCOPY_USATECHIDXUSD",
        timeframe="3m",
        reward_r=3.0,
        max_trades_per_day=1,
        weekdays="Monday,Wednesday,Thursday,Friday",
        entry_mode="start",
        stop_model="cisd_body",
        swing_liquidity_mode="strong_only",
        trade_window_end="10:30",
    ),
    Candidate(
        slug="spx_all_liquidity_cisd_body_2p5r_0930_1100",
        name="SPX - 5m - all liquidity - CISD body - 2.5R - 09:30-11:00",
        symbol="DUKASCOPY_USA500IDXUSD",
        timeframe="5m",
        reward_r=2.5,
        max_trades_per_day=2,
        weekdays="Tuesday,Wednesday,Friday",
        setup_type="fvg",
        entry_mode="start",
        stop_model="cisd_body",
        trade_window_end="11:00",
    ),
    Candidate(
        slug="spx_all_liquidity_cisd_body_2r_midpoint_0930_1100",
        name="SPX - 5m - all liquidity - CISD body - 2R midpoint - 09:30-11:00",
        symbol="DUKASCOPY_USA500IDXUSD",
        timeframe="5m",
        reward_r=2.0,
        max_trades_per_day=2,
        weekdays="Tuesday,Wednesday,Friday",
        setup_type="fvg",
        entry_mode="midpoint",
        stop_model="cisd_body",
        trade_window_end="11:00",
    ),
]


def main() -> None:
    reset_report_dir()
    months = choose_months()
    rows: list[dict[str, object]] = []
    monthly_rows: list[pd.DataFrame] = []
    all_trades: list[pd.DataFrame] = []

    for candidate in CANDIDATES:
        out_dir = REPORT_DIR / candidate.slug
        out_dir.mkdir(parents=True, exist_ok=True)
        market_data = load_ohlcv(filter_paths(RAW_DIR, candidate.symbol, candidate.timeframe))
        frame = market_data.frame.copy()
        frame = select_month_windows(frame, months)
        frame["date"] = frame["time"].dt.date

        config = build_config(candidate)
        result = run_backtest(frame, config)
        trades = [trade for trade in result.trades if trade.date[:7] in months]
        trades_df = trades_to_frame(trades)
        summary_df = summarize_trades(trades)
        monthly_df = monthly_stats(trades)
        weekday_df = weekday_stats(trades)
        parameters_df = pd.DataFrame([asdict(config)])

        trades_df.to_csv(out_dir / "trades.csv", index=False)
        summary_df.to_csv(out_dir / "summary.csv", index=False)
        monthly_df.to_csv(out_dir / "monthly_stats.csv", index=False)
        weekday_df.to_csv(out_dir / "weekday_stats.csv", index=False)
        parameters_df.to_csv(out_dir / "parameters.csv", index=False)

        summary = summary_df.iloc[0].to_dict()
        row = build_row(candidate, summary, monthly_df, out_dir)
        rows.append(row)

        if not monthly_df.empty:
            monthly_df.insert(0, "candidate", candidate.name)
            monthly_df.insert(0, "candidate_slug", candidate.slug)
            monthly_rows.append(monthly_df)
        if not trades_df.empty:
            trades_df.insert(0, "candidate", candidate.name)
            trades_df.insert(0, "candidate_slug", candidate.slug)
            all_trades.append(trades_df)

    comparison = pd.DataFrame(rows)
    monthly_detail = pd.concat(monthly_rows, ignore_index=True) if monthly_rows else pd.DataFrame()
    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(REPORT_DIR / "all_trades.csv", index=False)

    write_markdown_report(months, comparison, monthly_detail)
    write_html_report(months, comparison, monthly_detail)
    print(f"Selected months: {', '.join(months)}")
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def choose_months() -> list[str]:
    starts: list[str] = []
    for candidate in CANDIDATES:
        data = load_ohlcv(filter_paths(RAW_DIR, candidate.symbol, candidate.timeframe))
        months = set(data.frame["time"].dt.strftime("%Y-%m"))
        starts.append(min(months))
    common_months = set(load_ohlcv(filter_paths(RAW_DIR, CANDIDATES[0].symbol, CANDIDATES[0].timeframe)).frame["time"].dt.strftime("%Y-%m"))
    for candidate in CANDIDATES[1:]:
        data = load_ohlcv(filter_paths(RAW_DIR, candidate.symbol, candidate.timeframe))
        common_months &= set(data.frame["time"].dt.strftime("%Y-%m"))
    common_months = {month for month in common_months if month < "2026-07"}
    rng = random.Random(SEED)
    return sorted(rng.sample(sorted(common_months), SELECTED_MONTH_COUNT))


def month_start(month: str) -> pd.Timestamp:
    return pd.Timestamp(f"{month}-01 00:00", tz="America/New_York")


def month_end(month: str) -> pd.Timestamp:
    return month_start(month) + pd.offsets.MonthBegin(1)


def select_month_windows(frame: pd.DataFrame, months: list[str]) -> pd.DataFrame:
    windows = []
    for month in months:
        start = month_start(month) - pd.Timedelta(days=2)
        end = month_end(month)
        windows.append(frame[(frame["time"] >= start) & (frame["time"] < end)])
    if not windows:
        return frame.iloc[0:0].copy()
    selected = pd.concat(windows, ignore_index=True)
    return selected.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)


def build_config(candidate: Candidate) -> SymbolConfig:
    config = SYMBOL_CONFIGS[(candidate.symbol, candidate.timeframe)]
    return replace(
        config,
        reward_r=candidate.reward_r,
        max_trades_per_day=candidate.max_trades_per_day,
        allowed_weekdays=candidate.weekdays,
        setup_type_filter=candidate.setup_type,
        direction_filter=candidate.direction,
        fvg_entry_mode=candidate.entry_mode,
        stop_model=candidate.stop_model,
        stop_management="none",
        session_liquidity_only=candidate.session_liquidity_only,
        swing_liquidity_mode=candidate.swing_liquidity_mode,
        trade_window_start=candidate.trade_window_start,
        trade_window_end=candidate.trade_window_end,
    )


def filter_paths(path: Path, symbol: str, timeframe: str) -> list[Path]:
    filtered: list[Path] = []
    for candidate in sorted(path.rglob("*.csv")):
        candidate_symbol, candidate_timeframe = infer_symbol_timeframe(candidate)
        if candidate_symbol == symbol and candidate_timeframe == timeframe:
            filtered.append(candidate)
    if not filtered:
        raise SystemExit(f"CSV bulunamadi: {symbol} {timeframe}")
    return filtered


def build_row(
    candidate: Candidate,
    summary: dict[str, object],
    monthly: pd.DataFrame,
    out_dir: Path,
) -> dict[str, object]:
    max_dd = float(summary.get("max_drawdown_r", 0) or 0)
    net_r = float(summary.get("net_r", 0) or 0)
    return {
        "candidate": candidate.name,
        "candidate_slug": candidate.slug,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "reward_r": candidate.reward_r,
        "entry_mode": candidate.entry_mode,
        "stop_model": candidate.stop_model,
        "session_liquidity_only": candidate.session_liquidity_only,
        "swing_liquidity_mode": candidate.swing_liquidity_mode,
        "trade_window": f"{candidate.trade_window_start}-{candidate.trade_window_end}",
        "min_fvg_points": SYMBOL_CONFIGS[(candidate.symbol, candidate.timeframe)].min_fvg_points,
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
        "out_dir": str(out_dir.relative_to(ROOT)),
    }


def write_markdown_report(months: list[str], comparison: pd.DataFrame, monthly_detail: pd.DataFrame) -> None:
    title = f"Corrected Engine {SELECTED_MONTH_COUNT}-Month Candidate Test"
    lines = [
        f"# {title}",
        "",
        f"Random seed: `{SEED}`",
        f"Selected months: {', '.join(months)}",
        "",
        "Engine corrections active:",
        "",
        "- Pre-NY already-taken session liquidity is filtered out.",
        "- Previous NY AM high/low is included.",
        "- Named session liquidity is prioritized over generic swing liquidity.",
        "- Entry fill on the FVG/IFVG formation candle is disabled.",
        "- Symbol-specific minimum FVG size is enabled.",
        "- CISD requires a post-sweep reaction reference candle.",
        "- CISD is invalidated if structure breaks before FVG/IFVG confirmation.",
        "- Same-direction follow-up trades can continue without a fresh sweep; opposite direction still requires a fresh sweep.",
        "",
        "## Summary",
        "",
        markdown_table(comparison),
        "",
        "## Monthly Detail",
        "",
        markdown_table(monthly_detail) if not monthly_detail.empty else "No trades.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_html_report(months: list[str], comparison: pd.DataFrame, monthly_detail: pd.DataFrame) -> None:
    title = f"Corrected Engine {SELECTED_MONTH_COUNT}-Month Candidate Test"
    html_doc = f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    body {{ margin: 0; background: #f7f8fa; color: #17202a; font: 14px/1.5 "Segoe UI", Arial, sans-serif; }}
    header {{ background: #172033; color: white; padding: 26px 34px; }}
    main {{ max-width: 1240px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin-top: 28px; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #dbe2ea; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #dbe2ea; text-align: right; white-space: nowrap; }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; white-space: normal; }}
    th {{ background: #f1f4f8; color: #5f6b7a; font-size: 12px; text-transform: uppercase; }}
    .note {{ background: white; border: 1px solid #dbe2ea; border-radius: 8px; padding: 14px 16px; }}
  </style>
</head>
<body>
  <header>
    <h1>{esc(title)}</h1>
    <p>Selected months: {esc(', '.join(months))} | Random seed: {SEED}</p>
  </header>
  <main>
    <section class="note">
      <b>Corrections active:</b> pre-NY taken-liquidity filter, previous NY AM levels,
      session liquidity priority, no same-candle setup fill, minimum FVG size,
      post-sweep CISD reference, CISD invalidation, same-direction continuation rule.
    </section>
    <h2>Summary</h2>
    {html_table(comparison)}
    <h2>Monthly Detail</h2>
    {html_table(monthly_detail) if not monthly_detail.empty else '<p>No trades.</p>'}
  </main>
</body>
</html>"""
    (REPORT_DIR / "report.html").write_text(html_doc, encoding="utf-8")


def markdown_table(frame: pd.DataFrame) -> str:
    headers = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(markdown_cell(row[column]) for column in frame.columns) + " |")
    return "\n".join(lines)


def markdown_cell(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        value = f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value).replace("|", "\\|")


def html_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, row in frame.iterrows():
        rows.append("<tr>" + "".join(f"<td>{esc(format_value(row[col]))}</td>" for col in frame.columns) + "</tr>")
    header = "".join(f"<th>{esc(col)}</th>" for col in frame.columns)
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def format_value(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def esc(value: object) -> str:
    return html.escape(str(value))


if __name__ == "__main__":
    main()
