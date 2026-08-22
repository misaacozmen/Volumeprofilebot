from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_frequency_expansion_tests as frequency
import run_main_candidate_filter_tests as filters
from backtest.risk import apply_pair_risk_rule


REPORT_DIR = ROOT / "outputs" / "reports" / "main_candidate_first30_validation"
VARIANTS = ["baseline", "first30_q60", "first30_q70"]


def main() -> None:
    started = time.perf_counter()
    reset_report_dir()
    loaded = filters.load_data()
    thresholds = filters.build_thresholds(loaded)
    legs = filters.build_leg_specs()
    runs = filters.run_leg_variants(legs, loaded, thresholds)

    all_trades = []
    summary_rows = []
    monthly_rows = []
    yearly_rows = []
    drawdown_rows = []

    for system in sorted({leg.system for leg in legs}):
        for variant in VARIANTS:
            nq_variant = variant
            spx_variant = "baseline"
            pair = filters.pair_trades(runs, system, "nq", nq_variant, "spx", spx_variant)
            capped = apply_pair_risk_rule(pair, -1.0)
            tagged = capped.copy()
            if not tagged.empty:
                tagged.insert(0, "validation_variant", variant)
                tagged.insert(0, "validation_system", system)
                all_trades.append(tagged)
            summary_rows.append(summary_row(system, variant, capped))
            monthly_rows.extend(period_rows(system, variant, capped, "month"))
            yearly_rows.extend(period_rows(system, variant, capped, "year"))
            drawdown_rows.append(drawdown_row(system, variant, capped))

    summary = pd.DataFrame(summary_rows)
    monthly = pd.DataFrame(monthly_rows)
    yearly = pd.DataFrame(yearly_rows)
    drawdown = pd.DataFrame(drawdown_rows)
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    deltas = build_deltas(summary)
    worst_months = monthly.sort_values(["net_r", "trades"], ascending=[True, False]).groupby(
        ["system", "variant"], group_keys=False
    ).head(8)

    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    deltas.to_csv(REPORT_DIR / "deltas_vs_baseline.csv", index=False)
    monthly.to_csv(REPORT_DIR / "monthly.csv", index=False)
    yearly.to_csv(REPORT_DIR / "yearly.csv", index=False)
    drawdown.to_csv(REPORT_DIR / "drawdown.csv", index=False)
    worst_months.to_csv(REPORT_DIR / "worst_months.csv", index=False)
    trades.to_csv(REPORT_DIR / "all_trades_after_normalized_cap.csv", index=False)
    write_report(summary, deltas, yearly, worst_months, drawdown, time.perf_counter() - started)
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


def summary_row(system: str, variant: str, trades: pd.DataFrame) -> dict[str, object]:
    stats = frequency.stats(trades)
    daily = trades.groupby("date")["r_multiple"].sum() if not trades.empty else pd.Series(dtype=float)
    return {
        "system": system,
        "variant": variant,
        **stats,
        "avg_trades_per_month": round(len(trades) / frequency.active_month_count(trades), 2) if len(trades) else 0.0,
        "worst_day_r": round(float(daily.min()), 2) if len(daily) else 0.0,
        "positive_months": 0,
        "negative_months": 0,
        "worst_month_r": 0.0,
        "positive_years": 0,
        "negative_years": 0,
        "worst_year_r": 0.0,
    }


def period_rows(system: str, variant: str, trades: pd.DataFrame, period: str) -> list[dict[str, object]]:
    if trades.empty:
        return []
    frame = trades.copy()
    frame["entry_time_dt"] = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    if period == "month":
        frame["period"] = frame["entry_time_dt"].dt.strftime("%Y-%m")
    else:
        frame["period"] = frame["entry_time_dt"].dt.strftime("%Y")
    rows = []
    for value, group in frame.groupby("period"):
        stats = frequency.stats(group)
        rows.append(
            {
                "system": system,
                "variant": variant,
                period: value,
                **stats,
            }
        )
    return rows


def drawdown_row(system: str, variant: str, trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {
            "system": system,
            "variant": variant,
            "max_drawdown_r": 0.0,
            "max_consecutive_losing_trades": 0,
            "max_consecutive_losing_days": 0,
            "worst_month_r": 0.0,
            "worst_year_r": 0.0,
        }
    ordered = trades.copy()
    ordered["entry_time_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    ordered = ordered.sort_values("entry_time_dt")
    ordered["month"] = ordered["entry_time_dt"].dt.strftime("%Y-%m")
    ordered["year"] = ordered["entry_time_dt"].dt.strftime("%Y")
    equity = ordered["r_multiple"].cumsum()
    drawdown = equity - equity.cummax()
    daily = ordered.groupby("date")["r_multiple"].sum().reset_index().sort_values("date")
    return {
        "system": system,
        "variant": variant,
        "max_drawdown_r": round(float(drawdown.min()), 2),
        "max_consecutive_losing_trades": max_consecutive_negative(ordered["r_multiple"]),
        "max_consecutive_losing_days": max_consecutive_negative(daily["r_multiple"]),
        "worst_month_r": round(float(ordered.groupby("month")["r_multiple"].sum().min()), 2),
        "worst_year_r": round(float(ordered.groupby("year")["r_multiple"].sum().min()), 2),
    }


def max_consecutive_negative(values: pd.Series) -> int:
    best = current = 0
    for value in values:
        if float(value) < 0:
            current += 1
            best = max(best, current)
        elif float(value) > 0:
            current = 0
    return best


def build_deltas(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for system, group in summary.groupby("system"):
        baseline = group[group["variant"] == "baseline"].iloc[0]
        for _, row in group[group["variant"] != "baseline"].iterrows():
            rows.append(
                {
                    "system": system,
                    "variant": row["variant"],
                    "delta_trades": int(row["trades"] - baseline["trades"]),
                    "delta_avg_trades_per_month": round(float(row["avg_trades_per_month"]) - float(baseline["avg_trades_per_month"]), 2),
                    "delta_win_rate": round(float(row["win_rate"]) - float(baseline["win_rate"]), 2),
                    "delta_net_r": round(float(row["net_r"]) - float(baseline["net_r"]), 2),
                    "delta_max_drawdown_r": round(float(row["max_drawdown_r"]) - float(baseline["max_drawdown_r"]), 2),
                    "delta_profit_factor": round(float(row["profit_factor"]) - float(baseline["profit_factor"]), 2),
                    "delta_net_r_per_dd": round(float(row["net_r_per_dd"]) - float(baseline["net_r_per_dd"]), 2),
                }
            )
    return pd.DataFrame(rows)


def enrich_summary(summary: pd.DataFrame, monthly: pd.DataFrame, yearly: pd.DataFrame, drawdown: pd.DataFrame) -> pd.DataFrame:
    enriched = summary.copy()
    for index, row in enriched.iterrows():
        month_rows = monthly[(monthly["system"] == row["system"]) & (monthly["variant"] == row["variant"])]
        year_rows = yearly[(yearly["system"] == row["system"]) & (yearly["variant"] == row["variant"])]
        dd = drawdown[(drawdown["system"] == row["system"]) & (drawdown["variant"] == row["variant"])].iloc[0]
        enriched.loc[index, "positive_months"] = int((month_rows["net_r"] > 0).sum())
        enriched.loc[index, "negative_months"] = int((month_rows["net_r"] < 0).sum())
        enriched.loc[index, "worst_month_r"] = round(float(month_rows["net_r"].min()), 2)
        enriched.loc[index, "positive_years"] = int((year_rows["net_r"] > 0).sum())
        enriched.loc[index, "negative_years"] = int((year_rows["net_r"] < 0).sum())
        enriched.loc[index, "worst_year_r"] = round(float(year_rows["net_r"].min()), 2)
        enriched.loc[index, "max_consecutive_losing_trades"] = int(dd["max_consecutive_losing_trades"])
        enriched.loc[index, "max_consecutive_losing_days"] = int(dd["max_consecutive_losing_days"])
    return enriched


def write_report(
    summary: pd.DataFrame,
    deltas: pd.DataFrame,
    yearly: pd.DataFrame,
    worst_months: pd.DataFrame,
    drawdown: pd.DataFrame,
    runtime_seconds: float,
) -> None:
    monthly = pd.read_csv(REPORT_DIR / "monthly.csv") if (REPORT_DIR / "monthly.csv").exists() else pd.DataFrame()
    enriched_summary = enrich_summary(summary, monthly, yearly, drawdown)
    enriched_summary.to_csv(REPORT_DIR / "summary_enriched.csv", index=False)
    lines = [
        "# Main Candidate First30 Validation",
        "",
        "Scope: NQ first30_q60/q70 validation on the four active NQ/SPX systems.",
        "Pair cap is applied by the shared deterministic helper: sort by group, date, entry time, symbol if present else label, source order; stop after daily cumulative R <= -1R.",
        f"Runtime: {runtime_seconds:.1f} seconds",
        "",
        "## Summary",
        "",
        base_report.markdown_table(enriched_summary),
        "",
        "## Deltas Vs Baseline",
        "",
        base_report.markdown_table(deltas),
        "",
        "## Yearly",
        "",
        base_report.markdown_table(yearly),
        "",
        "## Drawdown",
        "",
        base_report.markdown_table(drawdown),
        "",
        "## Worst Months",
        "",
        base_report.markdown_table(worst_months),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
