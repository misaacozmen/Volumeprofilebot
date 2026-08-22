from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
from backtest.config import SYMBOL_CONFIGS
from backtest.data_loader import load_ohlcv
from backtest.strategy import monthly_stats, run_backtest, summarize_trades, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "selected_candidates_full_data"


@dataclass(frozen=True)
class SelectedCandidate:
    slug: str
    group: str
    label: str
    symbol: str
    timeframe: str
    reward_r: float
    max_trades_per_day: int
    weekdays: str
    setup_type: str
    entry_mode: str
    stop_model: str
    session_liquidity_only: bool
    swing_liquidity_mode: str
    trade_window_start: str
    trade_window_end: str
    risk_per_trade_pct: float
    first30_range_max: float | None = None


CANDIDATES = [
    SelectedCandidate(
        slug="nq_funded_session_midpoint_3r_0930_1030",
        group="funded",
        label="NQ funded",
        symbol="DUKASCOPY_USATECHIDXUSD",
        timeframe="3m",
        reward_r=3.0,
        max_trades_per_day=1,
        weekdays="Monday,Wednesday,Thursday,Friday",
        setup_type="all",
        entry_mode="midpoint",
        stop_model="cisd_body",
        session_liquidity_only=True,
        swing_liquidity_mode="all",
        trade_window_start="09:30",
        trade_window_end="10:30",
        risk_per_trade_pct=0.25,
        first30_range_max=161.45,
    ),
    SelectedCandidate(
        slug="spx_funded_all_start_2p5r_0930_1100",
        group="funded",
        label="SPX funded",
        symbol="DUKASCOPY_USA500IDXUSD",
        timeframe="5m",
        reward_r=2.5,
        max_trades_per_day=2,
        weekdays="Tuesday,Wednesday,Friday",
        setup_type="fvg",
        entry_mode="start",
        stop_model="cisd_body",
        session_liquidity_only=False,
        swing_liquidity_mode="all",
        trade_window_start="09:30",
        trade_window_end="11:00",
        risk_per_trade_pct=0.25,
        first30_range_max=29.69,
    ),
    SelectedCandidate(
        slug="nq_phase_strong_start_3r_0930_1030",
        group="phase",
        label="NQ phase",
        symbol="DUKASCOPY_USATECHIDXUSD",
        timeframe="3m",
        reward_r=3.0,
        max_trades_per_day=1,
        weekdays="Monday,Wednesday,Thursday,Friday",
        setup_type="all",
        entry_mode="start",
        stop_model="cisd_body",
        session_liquidity_only=False,
        swing_liquidity_mode="strong_only",
        trade_window_start="09:30",
        trade_window_end="10:30",
        risk_per_trade_pct=0.50,
        first30_range_max=162.00,
    ),
    SelectedCandidate(
        slug="spx_phase_all_midpoint_2r_0930_1100",
        group="phase",
        label="SPX phase",
        symbol="DUKASCOPY_USA500IDXUSD",
        timeframe="5m",
        reward_r=2.0,
        max_trades_per_day=2,
        weekdays="Tuesday,Wednesday,Friday",
        setup_type="fvg",
        entry_mode="midpoint",
        stop_model="cisd_body",
        session_liquidity_only=False,
        swing_liquidity_mode="all",
        trade_window_start="09:30",
        trade_window_end="11:00",
        risk_per_trade_pct=0.50,
        first30_range_max=30.23,
    ),
]


def main() -> None:
    reset_report_dir()
    loaded = load_data()
    comparison_rows: list[dict[str, object]] = []
    all_monthly: list[pd.DataFrame] = []
    all_trades: list[pd.DataFrame] = []
    candidate_frames: dict[str, pd.DataFrame] = {}

    for candidate in CANDIDATES:
        frame = loaded[(candidate.symbol, candidate.timeframe)].copy()
        config = build_config(candidate)
        result = run_backtest(frame, config)
        trades_df = trades_to_frame(result.trades)
        summary_df = summarize_trades(result.trades)
        monthly_df = monthly_stats(result.trades)
        out_dir = REPORT_DIR / candidate.slug
        out_dir.mkdir(parents=True, exist_ok=True)

        trades_df.to_csv(out_dir / "trades.csv", index=False)
        summary_df.to_csv(out_dir / "summary.csv", index=False)
        monthly_df.to_csv(out_dir / "monthly_stats.csv", index=False)

        comparison_rows.append(build_comparison_row(candidate, summary_df.iloc[0].to_dict(), monthly_df))
        if not monthly_df.empty:
            monthly_tagged = monthly_df.copy()
            monthly_tagged.insert(0, "label", candidate.label)
            monthly_tagged.insert(0, "candidate_slug", candidate.slug)
            all_monthly.append(monthly_tagged)
        if not trades_df.empty:
            tagged = trades_df.copy()
            tagged.insert(0, "group", candidate.group)
            tagged.insert(0, "label", candidate.label)
            tagged.insert(0, "candidate_slug", candidate.slug)
            all_trades.append(tagged)
        candidate_frames[candidate.slug] = trades_df

    comparison = pd.DataFrame(comparison_rows)
    monthly_detail = pd.concat(all_monthly, ignore_index=True) if all_monthly else pd.DataFrame()
    trades_all = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    pair_daily = build_pair_daily(candidate_frames)
    overlap = build_overlap_report(candidate_frames)

    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    trades_all.to_csv(REPORT_DIR / "all_trades.csv", index=False)
    pair_daily.to_csv(REPORT_DIR / "pair_daily.csv", index=False)
    overlap.to_csv(REPORT_DIR / "same_day_overlap.csv", index=False)
    write_report(comparison, monthly_detail, pair_daily, overlap)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_data() -> dict[tuple[str, str], pd.DataFrame]:
    data: dict[tuple[str, str], pd.DataFrame] = {}
    for candidate in CANDIDATES:
        key = (candidate.symbol, candidate.timeframe)
        if key in data:
            continue
        market_data = load_ohlcv(base_report.filter_paths(base_report.RAW_DIR, candidate.symbol, candidate.timeframe))
        data[key] = market_data.frame.copy()
    return data


def build_config(candidate: SelectedCandidate):
    config = SYMBOL_CONFIGS[(candidate.symbol, candidate.timeframe)]
    return replace(
        config,
        reward_r=candidate.reward_r,
        max_trades_per_day=candidate.max_trades_per_day,
        allowed_weekdays=candidate.weekdays,
        setup_type_filter=candidate.setup_type,
        direction_filter="all",
        fvg_entry_mode=candidate.entry_mode,
        stop_model=candidate.stop_model,
        stop_management="none",
        session_liquidity_only=candidate.session_liquidity_only,
        swing_liquidity_mode=candidate.swing_liquidity_mode,
        trade_window_start=candidate.trade_window_start,
        trade_window_end=candidate.trade_window_end,
        first30_range_filter="live_safe_max",
        first30_range_max=candidate.first30_range_max,
    )


def build_comparison_row(candidate: SelectedCandidate, summary: dict[str, object], monthly: pd.DataFrame) -> dict[str, object]:
    net_r = float(summary.get("net_r", 0) or 0)
    max_dd = float(summary.get("max_drawdown_r", 0) or 0)
    trades = int(summary.get("total_trades", 0) or 0)
    return {
        "group": candidate.group,
        "label": candidate.label,
        "candidate_slug": candidate.slug,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "reward_r": candidate.reward_r,
        "entry_mode": candidate.entry_mode,
        "stop_model": candidate.stop_model,
        "liquidity": "session_only" if candidate.session_liquidity_only else candidate.swing_liquidity_mode,
        "trade_window": f"{candidate.trade_window_start}-{candidate.trade_window_end}",
        "max_trades_per_day": candidate.max_trades_per_day,
        "risk_per_trade_pct": candidate.risk_per_trade_pct,
        "first30_range_filter": "live_safe_max",
        "first30_range_max": candidate.first30_range_max,
        "trades": trades,
        "avg_trades_per_month": round(trades / max(len(monthly), 1), 2),
        "wins": int(summary.get("wins", 0) or 0),
        "losses": int(summary.get("losses", 0) or 0),
        "win_rate": float(summary.get("win_rate", 0) or 0),
        "net_r": net_r,
        "net_return_pct": round(net_r * candidate.risk_per_trade_pct, 2),
        "max_drawdown_r": max_dd,
        "max_drawdown_pct": round(max_dd * candidate.risk_per_trade_pct, 2),
        "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
        "profit_factor": float(summary.get("profit_factor", 0) or 0),
        "positive_months": int((monthly["net_r"] > 0).sum()) if not monthly.empty else 0,
        "negative_months": int((monthly["net_r"] < 0).sum()) if not monthly.empty else 0,
    }


def build_pair_daily(candidate_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    slug_to_group_label = {candidate.slug: (candidate.group, candidate.label) for candidate in CANDIDATES}
    for slug, frame in candidate_frames.items():
        if frame.empty:
            continue
        group, label = slug_to_group_label[slug]
        daily = frame.groupby("date").agg(
            trades=("r_multiple", "count"),
            net_r=("r_multiple", "sum"),
            wins=("result", lambda values: int((values == "win").sum())),
            losses=("r_multiple", lambda values: int((values < 0).sum())),
        ).reset_index()
        daily.insert(0, "label", label)
        daily.insert(0, "group", group)
        daily.insert(0, "candidate_slug", slug)
        rows.append(daily)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def build_overlap_report(candidate_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    pairs = {
        "funded": ("nq_funded_session_midpoint_3r_0930_1030", "spx_funded_all_start_2p5r_0930_1100"),
        "phase": ("nq_phase_strong_start_3r_0930_1030", "spx_phase_all_midpoint_2r_0930_1100"),
    }
    for group, (nq_slug, spx_slug) in pairs.items():
        nq = candidate_frames[nq_slug]
        spx = candidate_frames[spx_slug]
        if nq.empty or spx.empty:
            continue
        overlap_dates = sorted(set(nq["date"]) & set(spx["date"]))
        for date in overlap_dates:
            nq_day = nq[nq["date"] == date]
            spx_day = spx[spx["date"] == date]
            nq_r = float(nq_day["r_multiple"].sum())
            spx_r = float(spx_day["r_multiple"].sum())
            rows.append(
                {
                    "group": group,
                    "date": date,
                    "nq_trades": len(nq_day),
                    "nq_r": round(nq_r, 2),
                    "spx_trades": len(spx_day),
                    "spx_r": round(spx_r, 2),
                    "combined_r": round(nq_r + spx_r, 2),
                    "both_won": nq_r > 0 and spx_r > 0,
                    "both_lost": nq_r < 0 and spx_r < 0,
                }
            )
    return pd.DataFrame(rows)


def write_report(
    comparison: pd.DataFrame,
    monthly_detail: pd.DataFrame,
    pair_daily: pd.DataFrame,
    overlap: pd.DataFrame,
) -> None:
    lines = [
        "# Selected Candidates Full-Data Report",
        "",
        "Scope: full available Dukascopy data loaded from raw CSV files.",
        "",
        "## Candidate Summary",
        "",
        base_report.markdown_table(comparison),
        "",
        "## Pair Daily Summary",
        "",
    ]
    for group in ["funded", "phase"]:
        group_daily = pair_daily[pair_daily["group"] == group]
        if group_daily.empty:
            continue
        totals = group_daily.groupby("date")["net_r"].sum().reset_index(name="combined_r")
        lines.extend(
            [
                f"### {group.title()} Pair",
                "",
                f"Unique trade dates: {len(totals)}",
                f"Net R: {totals['combined_r'].sum():.2f}",
                f"Worst day: {totals.loc[totals['combined_r'].idxmin(), 'date']} / {totals['combined_r'].min():.2f}R",
                f"Best day: {totals.loc[totals['combined_r'].idxmax(), 'date']} / {totals['combined_r'].max():.2f}R",
                "",
            ]
        )
    lines.extend(
        [
            "## Same-Day Overlap",
            "",
            overlap_summary(overlap),
            "",
            base_report.markdown_table(overlap) if not overlap.empty else "No overlap.",
            "",
            "## Monthly Detail",
            "",
            base_report.markdown_table(monthly_detail) if not monthly_detail.empty else "No trades.",
            "",
        ]
    )
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def overlap_summary(overlap: pd.DataFrame) -> str:
    if overlap.empty:
        return "No same-day overlap."
    lines = []
    for group, data in overlap.groupby("group"):
        lines.append(
            f"{group}: {len(data)} overlap days, combined overlap net {data['combined_r'].sum():.2f}R, "
            f"both lost {int(data['both_lost'].sum())}, both won {int(data['both_won'].sum())}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
