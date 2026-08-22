from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_frequency_expansion_tests as frequency


SOURCE_FILE = ROOT / "outputs" / "reports" / "main_candidate_first30_validation" / "all_trades_after_normalized_cap.csv"
REPORT_DIR = ROOT / "outputs" / "reports" / "final_candidate_pre_forward_validation"

FINAL_SELECTIONS = {
    "funded_selected": "first30_q70",
    "phase_selected": "first30_q60",
    "funded_selected_frequency": "first30_q70",
    "phase_selected_frequency": "first30_q70",
}

PERIOD_SPLITS = [
    ("2022_2023", "2022-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025_2026_to_data_end", "2025-01-01", "2026-12-31"),
    ("last_24_calendar_months", "2024-07-01", "2026-12-31"),
    ("last_18_calendar_months", "2025-01-01", "2026-12-31"),
    ("last_12_calendar_months", "2025-07-01", "2026-12-31"),
]


def main() -> None:
    reset_report_dir()
    trades = load_final_trades()
    summary = build_summary(trades)
    yearly = build_period_table(trades, "year")
    half_year = build_half_year_table(trades)
    split_summary = build_split_summary(trades)
    rolling = build_rolling_windows(trades, [3, 6, 12])
    stress = build_stress_summary(summary, split_summary, rolling)
    decision = build_decision_table(summary, split_summary, stress)

    trades.to_csv(REPORT_DIR / "all_final_trades_after_daily_cap.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    yearly.to_csv(REPORT_DIR / "yearly.csv", index=False)
    half_year.to_csv(REPORT_DIR / "half_year.csv", index=False)
    split_summary.to_csv(REPORT_DIR / "period_splits.csv", index=False)
    rolling.to_csv(REPORT_DIR / "rolling_windows.csv", index=False)
    stress.to_csv(REPORT_DIR / "stress_summary.csv", index=False)
    decision.to_csv(REPORT_DIR / "forward_readiness.csv", index=False)
    write_report(summary, yearly, half_year, split_summary, stress, decision)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_final_trades() -> pd.DataFrame:
    if not SOURCE_FILE.exists():
        raise SystemExit(f"Missing source file: {SOURCE_FILE}")
    source = pd.read_csv(SOURCE_FILE)
    frames = []
    for system, variant in FINAL_SELECTIONS.items():
        selected = source[(source["validation_system"] == system) & (source["validation_variant"] == variant)].copy()
        if selected.empty:
            raise SystemExit(f"Missing final selection trades: {system} / {variant}")
        selected["final_candidate"] = system
        selected["final_variant"] = variant
        selected["entry_time_dt"] = pd.to_datetime(selected["entry_time"], utc=True, format="mixed").dt.tz_convert(
            "America/New_York"
        )
        selected["entry_date"] = selected["entry_time_dt"].dt.date.astype(str)
        selected["month"] = selected["entry_time_dt"].dt.strftime("%Y-%m")
        selected["month_period"] = selected["entry_time_dt"].dt.tz_localize(None).dt.to_period("M")
        selected["year"] = selected["entry_time_dt"].dt.strftime("%Y")
        selected["half_year"] = selected["year"] + "-H" + (((selected["entry_time_dt"].dt.month - 1) // 6) + 1).astype(str)
        frames.append(selected)
    return pd.concat(frames, ignore_index=True).sort_values(["final_candidate", "entry_time_dt"])


def build_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for candidate, group in trades.groupby("final_candidate"):
        row = {"candidate": candidate, "variant": FINAL_SELECTIONS[candidate], **stats_with_periods(group)}
        rows.append(row)
    return pd.DataFrame(rows).sort_values("candidate")


def build_period_table(trades: pd.DataFrame, period_col: str) -> pd.DataFrame:
    rows = []
    for (candidate, period), group in trades.groupby(["final_candidate", period_col]):
        rows.append({"candidate": candidate, period_col: period, **frequency.stats(group)})
    return pd.DataFrame(rows).sort_values(["candidate", period_col])


def build_half_year_table(trades: pd.DataFrame) -> pd.DataFrame:
    return build_period_table(trades, "half_year")


def build_split_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for candidate, group in trades.groupby("final_candidate"):
        for split_name, start, end in PERIOD_SPLITS:
            start_ts = pd.Timestamp(start, tz="America/New_York")
            end_ts = pd.Timestamp(end, tz="America/New_York") + pd.Timedelta(days=1)
            selected = group[(group["entry_time_dt"] >= start_ts) & (group["entry_time_dt"] < end_ts)]
            rows.append({"candidate": candidate, "period_split": split_name, **stats_with_periods(selected)})
    return pd.DataFrame(rows).sort_values(["candidate", "period_split"])


def build_rolling_windows(trades: pd.DataFrame, windows: list[int]) -> pd.DataFrame:
    rows = []
    for candidate, group in trades.groupby("final_candidate"):
        months = pd.period_range(group["month_period"].min(), group["month_period"].max(), freq="M")
        for window in windows:
            for end_index in range(window - 1, len(months)):
                window_months = months[end_index - window + 1 : end_index + 1]
                selected = group[group["month_period"].isin(window_months)]
                if selected.empty:
                    continue
                rows.append(
                    {
                        "candidate": candidate,
                        "window_months": window,
                        "start_month": str(window_months[0]),
                        "end_month": str(window_months[-1]),
                        **frequency.stats(selected),
                    }
                )
    return pd.DataFrame(rows).sort_values(["candidate", "window_months", "start_month"])


def build_stress_summary(summary: pd.DataFrame, split_summary: pd.DataFrame, rolling: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for candidate in sorted(FINAL_SELECTIONS):
        candidate_splits = split_summary[split_summary["candidate"] == candidate]
        candidate_rolling = rolling[rolling["candidate"] == candidate]
        row = {
            "candidate": candidate,
            "negative_years": int(summary.loc[summary["candidate"] == candidate, "negative_years"].iloc[0]),
            "negative_months": int(summary.loc[summary["candidate"] == candidate, "negative_months"].iloc[0]),
            "worst_month_r": float(summary.loc[summary["candidate"] == candidate, "worst_month_r"].iloc[0]),
            "worst_split": "",
            "worst_split_net_r": 0.0,
            "worst_3m_net_r": 0.0,
            "worst_6m_net_r": 0.0,
            "worst_12m_net_r": 0.0,
            "recent_12m_net_r": split_net(candidate_splits, "last_12_calendar_months"),
            "recent_18m_net_r": split_net(candidate_splits, "last_18_calendar_months"),
        }
        if not candidate_splits.empty:
            worst_split = candidate_splits.sort_values(["net_r", "trades"], ascending=[True, False]).iloc[0]
            row["worst_split"] = worst_split["period_split"]
            row["worst_split_net_r"] = float(worst_split["net_r"])
        for window in [3, 6, 12]:
            window_rows = candidate_rolling[candidate_rolling["window_months"] == window]
            if not window_rows.empty:
                row[f"worst_{window}m_net_r"] = float(window_rows["net_r"].min())
        rows.append(row)
    return pd.DataFrame(rows).sort_values("candidate")


def build_decision_table(summary: pd.DataFrame, split_summary: pd.DataFrame, stress: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in stress.iterrows():
        candidate = row["candidate"]
        overall = summary[summary["candidate"] == candidate].iloc[0]
        recent_12 = split_summary[
            (split_summary["candidate"] == candidate) & (split_summary["period_split"] == "last_12_calendar_months")
        ].iloc[0]
        flags = []
        if int(row["negative_years"]) > 0:
            flags.append("negative_year")
        if float(recent_12["net_r"]) <= 0:
            flags.append("recent_12m_not_positive")
        if float(row["worst_6m_net_r"]) <= -8.0:
            flags.append("rolling_6m_drawdown_cluster")
        if float(overall["profit_factor"]) < 1.45:
            flags.append("low_overall_pf")
        if float(recent_12["profit_factor"]) < 1.15:
            flags.append("low_recent_12m_pf")

        if not flags:
            status = "pass"
        elif len(flags) <= 2 and "negative_year" not in flags and "recent_12m_not_positive" not in flags:
            status = "watch"
        else:
            status = "fail_or_needs_more_filtering"

        rows.append(
            {
                "candidate": candidate,
                "status": status,
                "flags": ",".join(flags) if flags else "none",
                "overall_net_r": overall["net_r"],
                "overall_pf": overall["profit_factor"],
                "recent_12m_net_r": recent_12["net_r"],
                "recent_12m_pf": recent_12["profit_factor"],
                "worst_6m_net_r": row["worst_6m_net_r"],
                "worst_month_r": row["worst_month_r"],
            }
        )
    return pd.DataFrame(rows).sort_values("candidate")


def stats_with_periods(trades: pd.DataFrame) -> dict[str, object]:
    base = frequency.stats(trades)
    if trades.empty:
        base.update(
            {
                "avg_trades_per_month": 0.0,
                "positive_months": 0,
                "negative_months": 0,
                "worst_month_r": 0.0,
                "positive_years": 0,
                "negative_years": 0,
                "worst_year_r": 0.0,
            }
        )
        return base
    monthly = trades.groupby("month")["r_multiple"].sum()
    yearly = trades.groupby("year")["r_multiple"].sum()
    base.update(
        {
            "avg_trades_per_month": round(len(trades) / frequency.active_month_count(trades), 2),
            "positive_months": int((monthly > 0).sum()),
            "negative_months": int((monthly < 0).sum()),
            "worst_month_r": round(float(monthly.min()), 2),
            "positive_years": int((yearly > 0).sum()),
            "negative_years": int((yearly < 0).sum()),
            "worst_year_r": round(float(yearly.min()), 2),
        }
    )
    return base


def split_net(frame: pd.DataFrame, split_name: str) -> float:
    selected = frame[frame["period_split"] == split_name]
    if selected.empty:
        return 0.0
    return float(selected.iloc[0]["net_r"])


def write_report(
    summary: pd.DataFrame,
    yearly: pd.DataFrame,
    half_year: pd.DataFrame,
    split_summary: pd.DataFrame,
    stress: pd.DataFrame,
    decision: pd.DataFrame,
) -> None:
    lines = [
        "# Final Candidate Pre-Forward Validation",
        "",
        "Scope: frozen final NQ/SPX combined candidates after daily -1R cap.",
        "Important: this is temporal robustness validation, not true out-of-sample validation, because first30 thresholds were selected from historical data.",
        "",
        "## Forward Readiness",
        "",
        base_report.markdown_table(decision),
        "",
        "## Summary",
        "",
        base_report.markdown_table(summary),
        "",
        "## Stress Summary",
        "",
        base_report.markdown_table(stress),
        "",
        "## Period Splits",
        "",
        base_report.markdown_table(split_summary),
        "",
        "## Yearly",
        "",
        base_report.markdown_table(yearly),
        "",
        "## Half-Year",
        "",
        base_report.markdown_table(half_year),
        "",
        "## Notes",
        "",
        "- pass: no negative years, recent 12 months positive, worst rolling 6 months above -8R, overall PF >= 1.45, recent 12m PF >= 1.15.",
        "- watch: minor quality warnings but no negative year or recent-12-month failure.",
        "- fail_or_needs_more_filtering: failed a major temporal stability check.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
