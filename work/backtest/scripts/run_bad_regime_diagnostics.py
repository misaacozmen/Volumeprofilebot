from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
from backtest.data_loader import load_ohlcv


SOURCE_DIR = ROOT / "outputs" / "reports" / "selected_candidates_full_data"
REPORT_DIR = ROOT / "outputs" / "reports" / "bad_regime_diagnostics"


def main() -> None:
    reset_report_dir()
    trades = pd.read_csv(SOURCE_DIR / "all_trades.csv")
    trades = prepare_trades(trades)
    market = load_market_frames(trades)
    context = build_daily_context(market)
    enriched = enrich_trades(trades, context)
    monthly = build_monthly_regime(enriched)
    pair_monthly = build_pair_monthly_regime(enriched)
    comparisons = build_good_bad_comparisons(enriched, monthly, pair_monthly)
    filter_hints = build_filter_hints(enriched)

    enriched.to_csv(REPORT_DIR / "enriched_trades.csv", index=False)
    monthly.to_csv(REPORT_DIR / "candidate_month_regime.csv", index=False)
    pair_monthly.to_csv(REPORT_DIR / "pair_month_regime.csv", index=False)
    comparisons.to_csv(REPORT_DIR / "good_bad_comparison.csv", index=False)
    filter_hints.to_csv(REPORT_DIR / "filter_hints.csv", index=False)
    write_report(monthly, pair_monthly, comparisons, filter_hints)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def prepare_trades(trades: pd.DataFrame) -> pd.DataFrame:
    for column in ["sweep_time", "cisd_time", "fvg_time", "entry_time"]:
        trades[f"{column}_dt"] = pd.to_datetime(trades[column], utc=True, format="mixed").dt.tz_convert("America/New_York")
    trades["month"] = trades["entry_time_dt"].dt.strftime("%Y-%m")
    trades["year"] = trades["entry_time_dt"].dt.year.astype(str)
    trades["entry_minutes"] = trades["entry_time_dt"].dt.hour * 60 + trades["entry_time_dt"].dt.minute
    trades["sweep_minutes"] = trades["sweep_time_dt"].dt.hour * 60 + trades["sweep_time_dt"].dt.minute
    trades["liquidity_type"] = trades["liquidity_context"].map(liquidity_type)
    trades["setup_kind"] = trades["notes"].str.split(";").str[0]
    return trades


def liquidity_type(value: str) -> str:
    text = str(value)
    if text.startswith("swing_"):
        return "swing"
    if text.startswith("london_"):
        return "london"
    if text.startswith("asia_"):
        return "asia"
    if text.startswith("ny_am_"):
        return "ny_am"
    if text.startswith("ny_pm_"):
        return "ny_pm"
    if text.startswith("previous_day_"):
        return "previous_day"
    return "other"


def load_market_frames(trades: pd.DataFrame) -> dict[tuple[str, str], pd.DataFrame]:
    frames = {}
    for symbol, timeframe in trades[["symbol", "timeframe"]].drop_duplicates().itertuples(index=False):
        frames[(symbol, timeframe)] = load_ohlcv(base_report.filter_paths(base_report.RAW_DIR, symbol, timeframe)).frame
    return frames


def build_daily_context(market: dict[tuple[str, str], pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for (symbol, timeframe), frame in market.items():
        for trade_date in sorted(frame["date"].unique()):
            day = pd.Timestamp(trade_date).tz_localize("America/New_York")
            profile_start = day - pd.Timedelta(hours=6)
            profile_end = day + pd.Timedelta(hours=9, minutes=30)
            first30_start = profile_end
            first30_end = day + pd.Timedelta(hours=10)
            profile = frame[(frame["time"] >= profile_start) & (frame["time"] < profile_end)]
            first30 = frame[(frame["time"] >= first30_start) & (frame["time"] < first30_end)]
            if profile.empty or first30.empty:
                continue
            profile_high = float(profile["high"].max())
            profile_low = float(profile["low"].min())
            first_high = float(first30["high"].max())
            first_low = float(first30["low"].min())
            first_open = float(first30.iloc[0]["open"])
            first_close = float(first30.iloc[-1]["close"])
            first_range = first_high - first_low
            first_body = abs(first_close - first_open)
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "date": str(trade_date),
                    "profile_range": profile_high - profile_low,
                    "first30_range": first_range,
                    "first30_body": first_body,
                    "first30_directionality": round(first_body / first_range, 4) if first_range > 0 else 0.0,
                    "first30_return": first_close - first_open,
                    "ny_open": first_open,
                }
            )
    return pd.DataFrame(rows)


def enrich_trades(trades: pd.DataFrame, context: pd.DataFrame) -> pd.DataFrame:
    enriched = trades.merge(context, on=["symbol", "timeframe", "date"], how="left")
    enriched["va_width"] = enriched["vah"] - enriched["val"]
    enriched["profile_range_to_va_width"] = enriched["profile_range"] / enriched["va_width"].replace(0, pd.NA)
    enriched["open_to_vah"] = enriched["ny_open"] - enriched["vah"]
    enriched["open_to_val"] = enriched["ny_open"] - enriched["val"]
    enriched["entry_after_1030"] = enriched["entry_minutes"] > 10 * 60 + 30
    enriched["sweep_at_open"] = enriched["sweep_minutes"] <= 9 * 60 + 35
    return enriched


def build_monthly_regime(enriched: pd.DataFrame) -> pd.DataFrame:
    metrics = [
        "profile_range",
        "first30_range",
        "first30_directionality",
        "va_width",
        "profile_range_to_va_width",
        "entry_after_1030",
        "sweep_at_open",
    ]
    rows = []
    for keys, group in enriched.groupby(["group", "label", "candidate_slug", "month"]):
        row = dict(zip(["group", "label", "candidate_slug", "month"], keys))
        wins = int((group["result"] == "win").sum())
        row.update(
            {
                "trades": len(group),
                "wins": wins,
                "losses": int((group["r_multiple"] < 0).sum()),
                "win_rate": round(wins / len(group) * 100, 2),
                "net_r": round(float(group["r_multiple"].sum()), 2),
                "long_pct": round(float((group["direction"] == "long").mean() * 100), 2),
                "swing_pct": round(float((group["liquidity_type"] == "swing").mean() * 100), 2),
                "session_pct": round(float((group["liquidity_type"] != "swing").mean() * 100), 2),
            }
        )
        for metric in metrics:
            row[f"avg_{metric}"] = round(float(group[metric].mean()), 4) if group[metric].notna().any() else ""
        rows.append(row)
    return pd.DataFrame(rows)


def build_pair_monthly_regime(enriched: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (group_name, month), group in enriched.groupby(["group", "month"]):
        wins = int((group["result"] == "win").sum())
        rows.append(
            {
                "group": group_name,
                "month": month,
                "trades": len(group),
                "wins": wins,
                "losses": int((group["r_multiple"] < 0).sum()),
                "win_rate": round(wins / len(group) * 100, 2),
                "net_r": round(float(group["r_multiple"].sum()), 2),
                "avg_profile_range": round(float(group["profile_range"].mean()), 4),
                "avg_first30_range": round(float(group["first30_range"].mean()), 4),
                "avg_first30_directionality": round(float(group["first30_directionality"].mean()), 4),
                "avg_va_width": round(float(group["va_width"].mean()), 4),
                "entry_after_1030_pct": round(float(group["entry_after_1030"].mean() * 100), 2),
                "sweep_at_open_pct": round(float(group["sweep_at_open"].mean() * 100), 2),
                "swing_pct": round(float((group["liquidity_type"] == "swing").mean() * 100), 2),
            }
        )
    return pd.DataFrame(rows)


def build_good_bad_comparisons(
    enriched: pd.DataFrame,
    monthly: pd.DataFrame,
    pair_monthly: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for label, month_group in monthly.groupby("label"):
        bad_months = set(month_group[month_group["net_r"] < 0]["month"])
        good_months = set(month_group[month_group["net_r"] >= 4]["month"])
        rows.extend(compare_trade_sets(f"candidate:{label}", enriched[enriched["label"] == label], good_months, bad_months))
    for group, month_group in pair_monthly.groupby("group"):
        bad_months = set(month_group[month_group["net_r"] < 0]["month"])
        good_months = set(month_group[month_group["net_r"] >= 4]["month"])
        rows.extend(compare_trade_sets(f"pair:{group}", enriched[enriched["group"] == group], good_months, bad_months))
    return pd.DataFrame(rows)


def compare_trade_sets(scope: str, trades: pd.DataFrame, good_months: set[str], bad_months: set[str]) -> list[dict[str, object]]:
    rows = []
    for bucket, months in [("good", good_months), ("bad", bad_months)]:
        data = trades[trades["month"].isin(months)]
        if data.empty:
            continue
        rows.append(
            {
                "scope": scope,
                "bucket": bucket,
                "months": len(months),
                "trades": len(data),
                "win_rate": round(float((data["result"] == "win").mean() * 100), 2),
                "net_r": round(float(data["r_multiple"].sum()), 2),
                "avg_profile_range": round(float(data["profile_range"].mean()), 4),
                "avg_first30_range": round(float(data["first30_range"].mean()), 4),
                "avg_first30_directionality": round(float(data["first30_directionality"].mean()), 4),
                "avg_va_width": round(float(data["va_width"].mean()), 4),
                "entry_after_1030_pct": round(float(data["entry_after_1030"].mean() * 100), 2),
                "sweep_at_open_pct": round(float(data["sweep_at_open"].mean() * 100), 2),
                "swing_pct": round(float((data["liquidity_type"] == "swing").mean() * 100), 2),
                "long_pct": round(float((data["direction"] == "long").mean() * 100), 2),
            }
        )
    return rows


def build_filter_hints(enriched: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for label, data in enriched.groupby("label"):
        for metric in ["profile_range", "first30_range", "first30_directionality", "va_width"]:
            clean = data.dropna(subset=[metric])
            if clean.empty:
                continue
            q25, q50, q75 = clean[metric].quantile([0.25, 0.50, 0.75])
            for bucket, subset in [
                (f"{metric}_low", clean[clean[metric] <= q25]),
                (f"{metric}_mid", clean[(clean[metric] > q25) & (clean[metric] <= q75)]),
                (f"{metric}_high", clean[clean[metric] > q75]),
            ]:
                if subset.empty:
                    continue
                rows.append(
                    {
                        "label": label,
                        "bucket": bucket,
                        "trades": len(subset),
                        "win_rate": round(float((subset["result"] == "win").mean() * 100), 2),
                        "net_r": round(float(subset["r_multiple"].sum()), 2),
                        "avg_metric": round(float(subset[metric].mean()), 4),
                    }
                )
    return pd.DataFrame(rows)


def write_report(
    monthly: pd.DataFrame,
    pair_monthly: pd.DataFrame,
    comparisons: pd.DataFrame,
    filter_hints: pd.DataFrame,
) -> None:
    lines = [
        "# Bad Regime Diagnostics",
        "",
        "Good months are defined as net_r >= +4R. Bad months are defined as net_r < 0R.",
        "",
        "## Good vs Bad Comparison",
        "",
        base_report.markdown_table(comparisons),
        "",
        "## Filter Hints By Metric Quartile",
        "",
        base_report.markdown_table(filter_hints),
        "",
        "## Pair Monthly Regime",
        "",
        base_report.markdown_table(pair_monthly),
        "",
        "## Candidate Monthly Regime",
        "",
        base_report.markdown_table(monthly),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
