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
REPORT_DIR = ROOT / "outputs" / "reports" / "selected_candidate_loss_analysis"


def main() -> None:
    reset_report_dir()
    trades = pd.read_csv(SOURCE_DIR / "all_trades.csv")
    trades = prepare_trades(trades)
    context = build_context(trades)
    enriched = enrich(trades, context)
    losses = enriched[enriched["r_multiple"] < 0].copy()

    loss_summary = build_loss_summary(losses)
    loss_buckets = build_loss_buckets(losses)
    factor_compare = build_factor_compare(enriched)
    reason_summary = build_reason_summary(losses)
    overlap_loss = build_overlap_loss_summary(enriched)

    enriched.to_csv(REPORT_DIR / "enriched_trades.csv", index=False)
    losses.to_csv(REPORT_DIR / "sl_trades.csv", index=False)
    loss_summary.to_csv(REPORT_DIR / "loss_timing_summary.csv", index=False)
    loss_buckets.to_csv(REPORT_DIR / "loss_timing_buckets.csv", index=False)
    factor_compare.to_csv(REPORT_DIR / "win_loss_factor_compare.csv", index=False)
    reason_summary.to_csv(REPORT_DIR / "loss_reason_summary.csv", index=False)
    overlap_loss.to_csv(REPORT_DIR / "overlap_loss_summary.csv", index=False)
    write_report(loss_summary, loss_buckets, factor_compare, reason_summary, overlap_loss)
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
    for column in ["entry_time", "exit_time", "sweep_time", "cisd_time", "fvg_time"]:
        trades[f"{column}_dt"] = pd.to_datetime(trades[column], utc=True, format="mixed").dt.tz_convert("America/New_York")
    trades["entry_minutes"] = trades["entry_time_dt"].dt.hour * 60 + trades["entry_time_dt"].dt.minute
    trades["exit_minutes_after_entry"] = (trades["exit_time_dt"] - trades["entry_time_dt"]).dt.total_seconds() / 60
    trades["timeframe_minutes"] = trades["timeframe"].map(lambda value: int(str(value).rstrip("m")))
    trades["exit_candles_after_entry"] = trades["exit_minutes_after_entry"] / trades["timeframe_minutes"]
    trades["month"] = trades["entry_time_dt"].dt.strftime("%Y-%m")
    trades["liquidity_type"] = trades["liquidity_context"].map(liquidity_type)
    trades["risk_points"] = (trades["entry_price"] - trades["stop_price"]).abs()
    trades["entry_hour"] = trades["entry_time_dt"].dt.strftime("%H:%M")
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


def build_context(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for symbol, timeframe in trades[["symbol", "timeframe"]].drop_duplicates().itertuples(index=False):
        frame = load_ohlcv(base_report.filter_paths(base_report.RAW_DIR, symbol, timeframe)).frame
        needed_dates = set(trades[(trades["symbol"] == symbol) & (trades["timeframe"] == timeframe)]["date"])
        for trade_date in sorted(needed_dates):
            day = pd.Timestamp(trade_date).tz_localize("America/New_York")
            first30 = frame[(frame["time"] >= day + pd.Timedelta(hours=9, minutes=30)) & (frame["time"] < day + pd.Timedelta(hours=10))]
            profile = frame[(frame["time"] >= day - pd.Timedelta(hours=6)) & (frame["time"] < day + pd.Timedelta(hours=9, minutes=30))]
            if first30.empty or profile.empty:
                continue
            first_range = float(first30["high"].max() - first30["low"].min())
            first_body = abs(float(first30.iloc[-1]["close"] - first30.iloc[0]["open"]))
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "date": trade_date,
                    "first30_range": first_range,
                    "first30_directionality": first_body / first_range if first_range > 0 else 0,
                    "profile_range": float(profile["high"].max() - profile["low"].min()),
                }
            )
    return pd.DataFrame(rows)


def enrich(trades: pd.DataFrame, context: pd.DataFrame) -> pd.DataFrame:
    enriched = trades.merge(context, on=["symbol", "timeframe", "date"], how="left")
    enriched["va_width"] = enriched["vah"] - enriched["val"]
    enriched["is_fast_stop"] = enriched["exit_candles_after_entry"] <= 2
    enriched["is_same_bar_stop"] = enriched["result"] == "loss_same_bar"
    enriched["is_after_1000"] = enriched["entry_minutes"] >= 10 * 60
    enriched["is_after_1030"] = enriched["entry_minutes"] >= 10 * 60 + 30
    return enriched


def build_loss_summary(losses: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, group in losses.groupby(["group", "label", "candidate_slug", "symbol", "timeframe"]):
        rows.append(
            {
                "group": keys[0],
                "label": keys[1],
                "candidate_slug": keys[2],
                "symbol": keys[3],
                "timeframe": keys[4],
                "sl_trades": len(group),
                "avg_minutes_to_sl": round(float(group["exit_minutes_after_entry"].mean()), 2),
                "median_minutes_to_sl": round(float(group["exit_minutes_after_entry"].median()), 2),
                "avg_candles_to_sl": round(float(group["exit_candles_after_entry"].mean()), 2),
                "median_candles_to_sl": round(float(group["exit_candles_after_entry"].median()), 2),
                "same_bar_losses": int(group["is_same_bar_stop"].sum()),
                "fast_0_2_candle_losses": int(group["is_fast_stop"].sum()),
                "fast_0_2_candle_pct": round(float(group["is_fast_stop"].mean() * 100), 2),
                "avg_risk_points": round(float(group["risk_points"].mean()), 4),
            }
        )
    return pd.DataFrame(rows)


def build_loss_buckets(losses: pd.DataFrame) -> pd.DataFrame:
    rows = []
    bins = [
        ("same_bar", losses["is_same_bar_stop"]),
        ("0_2_candles", losses["exit_candles_after_entry"] <= 2),
        ("2_5_candles", (losses["exit_candles_after_entry"] > 2) & (losses["exit_candles_after_entry"] <= 5)),
        ("5_10_candles", (losses["exit_candles_after_entry"] > 5) & (losses["exit_candles_after_entry"] <= 10)),
        ("10plus_candles", losses["exit_candles_after_entry"] > 10),
    ]
    for label, group in losses.groupby("label"):
        total = len(group)
        for bucket, mask in bins:
            count = int(mask.loc[group.index].sum())
            rows.append({"label": label, "bucket": bucket, "count": count, "pct": round(count / total * 100, 2) if total else 0})
    return pd.DataFrame(rows)


def build_factor_compare(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for label, group in trades.groupby("label"):
        for side, data in [("win", group[group["r_multiple"] > 0]), ("loss", group[group["r_multiple"] < 0])]:
            rows.append(
                {
                    "label": label,
                    "side": side,
                    "trades": len(data),
                    "avg_first30_range": round(float(data["first30_range"].mean()), 4) if len(data) else "",
                    "avg_first30_directionality": round(float(data["first30_directionality"].mean()), 4) if len(data) else "",
                    "avg_profile_range": round(float(data["profile_range"].mean()), 4) if len(data) else "",
                    "avg_va_width": round(float(data["va_width"].mean()), 4) if len(data) else "",
                    "swing_pct": round(float((data["liquidity_type"] == "swing").mean() * 100), 2) if len(data) else "",
                    "long_pct": round(float((data["direction"] == "long").mean() * 100), 2) if len(data) else "",
                    "after_1000_pct": round(float(data["is_after_1000"].mean() * 100), 2) if len(data) else "",
                }
            )
    return pd.DataFrame(rows)


def build_reason_summary(losses: pd.DataFrame) -> pd.DataFrame:
    rows = []
    reason_masks = {
        "fast_stop_0_2_candles": losses["is_fast_stop"],
        "same_bar_stop": losses["is_same_bar_stop"],
        "after_1000_entry": losses["is_after_1000"],
        "after_1030_entry": losses["is_after_1030"],
        "swing_liquidity": losses["liquidity_type"] == "swing",
        "session_liquidity": losses["liquidity_type"] != "swing",
        "long": losses["direction"] == "long",
        "short": losses["direction"] == "short",
    }
    for label, group in losses.groupby("label"):
        total = len(group)
        for reason, mask in reason_masks.items():
            count = int(mask.loc[group.index].sum())
            rows.append({"label": label, "reason": reason, "count": count, "pct_of_sl": round(count / total * 100, 2) if total else 0})
    return pd.DataFrame(rows)


def build_overlap_loss_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for group_name, group in trades.groupby("group"):
        labels = sorted(group["label"].unique())
        if len(labels) < 2:
            continue
        left = group[group["label"] == labels[0]]
        right = group[group["label"] == labels[1]]
        overlap_dates = sorted(set(left["date"]) & set(right["date"]))
        for date in overlap_dates:
            left_r = float(left[left["date"] == date]["r_multiple"].sum())
            right_r = float(right[right["date"] == date]["r_multiple"].sum())
            rows.append(
                {
                    "group": group_name,
                    "date": date,
                    "left_label": labels[0],
                    "left_r": round(left_r, 2),
                    "right_label": labels[1],
                    "right_r": round(right_r, 2),
                    "combined_r": round(left_r + right_r, 2),
                    "both_lost": left_r < 0 and right_r < 0,
                }
            )
    return pd.DataFrame(rows)


def write_report(
    loss_summary: pd.DataFrame,
    loss_buckets: pd.DataFrame,
    factor_compare: pd.DataFrame,
    reason_summary: pd.DataFrame,
    overlap_loss: pd.DataFrame,
) -> None:
    overlap_summary = []
    if not overlap_loss.empty:
        for group, data in overlap_loss.groupby("group"):
            overlap_summary.append(
                {
                    "group": group,
                    "overlap_days": len(data),
                    "both_lost": int(data["both_lost"].sum()),
                    "overlap_net_r": round(float(data["combined_r"].sum()), 2),
                }
            )
    lines = [
        "# Selected Candidate SL Analysis",
        "",
        "Source: selected_candidates_full_data/all_trades.csv after motor-level live-safe filter.",
        "",
        "## SL Timing Summary",
        "",
        base_report.markdown_table(loss_summary),
        "",
        "## SL Timing Buckets",
        "",
        base_report.markdown_table(loss_buckets),
        "",
        "## Win/Loss Factor Compare",
        "",
        base_report.markdown_table(factor_compare),
        "",
        "## SL Reason Tags",
        "",
        base_report.markdown_table(reason_summary),
        "",
        "## Same-Day Overlap Loss Summary",
        "",
        base_report.markdown_table(pd.DataFrame(overlap_summary)) if overlap_summary else "No overlap.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
