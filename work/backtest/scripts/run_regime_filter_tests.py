from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report


SOURCE_DIR = ROOT / "outputs" / "reports" / "bad_regime_diagnostics"
REPORT_DIR = ROOT / "outputs" / "reports" / "regime_filter_tests"


FILTERS = [
    ("baseline", "No filter"),
    ("no_first30_range_high", "Exclude trades where first30_range is above candidate 75th percentile"),
    ("no_first30_directionality_low", "Exclude trades where first30_directionality is below candidate 25th percentile"),
    ("no_high_range_or_low_directionality", "Exclude high first30_range OR low first30_directionality"),
]


def main() -> None:
    reset_report_dir()
    trades = pd.read_csv(SOURCE_DIR / "enriched_trades.csv")
    thresholds = build_thresholds(trades)
    rows = []
    monthly_rows = []
    overlap_rows = []
    pair_rows = []

    for filter_name, description in FILTERS:
        filtered = apply_filter(trades, thresholds, filter_name)
        for keys, group in filtered.groupby(["group", "candidate_slug", "label"]):
            rows.append(build_stats_row(filter_name, description, keys, group, thresholds))
            monthly = monthly_stats(filter_name, group)
            if not monthly.empty:
                monthly_rows.append(monthly)
        pair_rows.extend(build_pair_stats(filter_name, filtered))
        overlap_rows.extend(build_overlap(filter_name, filtered))

    comparison = pd.DataFrame(rows)
    pair_comparison = pd.DataFrame(pair_rows)
    monthly_detail = pd.concat(monthly_rows, ignore_index=True) if monthly_rows else pd.DataFrame()
    overlap = pd.DataFrame(overlap_rows)
    thresholds.to_csv(REPORT_DIR / "thresholds.csv", index=False)
    comparison.to_csv(REPORT_DIR / "candidate_filter_comparison.csv", index=False)
    pair_comparison.to_csv(REPORT_DIR / "pair_filter_comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    overlap.to_csv(REPORT_DIR / "same_day_overlap.csv", index=False)
    write_report(thresholds, comparison, pair_comparison, overlap)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_thresholds(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, group in trades.groupby(["group", "candidate_slug", "label"]):
        rows.append(
            {
                "group": keys[0],
                "candidate_slug": keys[1],
                "label": keys[2],
                "first30_range_q75": float(group["first30_range"].quantile(0.75)),
                "first30_directionality_q25": float(group["first30_directionality"].quantile(0.25)),
            }
        )
    return pd.DataFrame(rows)


def apply_filter(trades: pd.DataFrame, thresholds: pd.DataFrame, filter_name: str) -> pd.DataFrame:
    if filter_name == "baseline":
        return trades.copy()
    merged = trades.merge(thresholds, on=["group", "candidate_slug", "label"], how="left")
    keep = pd.Series(True, index=merged.index)
    if filter_name in {"no_first30_range_high", "no_high_range_or_low_directionality"}:
        keep &= merged["first30_range"] <= merged["first30_range_q75"]
    if filter_name in {"no_first30_directionality_low", "no_high_range_or_low_directionality"}:
        keep &= merged["first30_directionality"] >= merged["first30_directionality_q25"]
    return merged[keep].drop(columns=["first30_range_q75", "first30_directionality_q25"])


def build_stats_row(
    filter_name: str,
    description: str,
    keys: tuple[str, str, str],
    group: pd.DataFrame,
    thresholds: pd.DataFrame,
) -> dict[str, object]:
    group_name, slug, label = keys
    threshold = thresholds[(thresholds["group"] == group_name) & (thresholds["candidate_slug"] == slug)].iloc[0]
    net_r = float(group["r_multiple"].sum())
    max_dd = max_drawdown(group)
    return {
        "filter": filter_name,
        "description": description,
        "group": group_name,
        "candidate_slug": slug,
        "label": label,
        "trades": len(group),
        "wins": int((group["result"] == "win").sum()),
        "losses": int((group["r_multiple"] < 0).sum()),
        "win_rate": round(float((group["result"] == "win").mean() * 100), 2),
        "net_r": round(net_r, 2),
        "max_drawdown_r": round(max_dd, 2),
        "profit_factor": profit_factor(group),
        "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
        "first30_range_q75": round(float(threshold["first30_range_q75"]), 4),
        "first30_directionality_q25": round(float(threshold["first30_directionality_q25"]), 4),
    }


def monthly_stats(filter_name: str, group: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, month_group in group.groupby(["group", "candidate_slug", "label", "month"]):
        rows.append(
            {
                "filter": filter_name,
                "group": keys[0],
                "candidate_slug": keys[1],
                "label": keys[2],
                "month": keys[3],
                "trades": len(month_group),
                "wins": int((month_group["result"] == "win").sum()),
                "losses": int((month_group["r_multiple"] < 0).sum()),
                "win_rate": round(float((month_group["result"] == "win").mean() * 100), 2),
                "net_r": round(float(month_group["r_multiple"].sum()), 2),
            }
        )
    return pd.DataFrame(rows)


def build_pair_stats(filter_name: str, trades: pd.DataFrame) -> list[dict[str, object]]:
    rows = []
    for group_name, group in trades.groupby("group"):
        net_r = float(group["r_multiple"].sum())
        max_dd = max_drawdown(group)
        rows.append(
            {
                "filter": filter_name,
                "group": group_name,
                "trades": len(group),
                "unique_dates": int(group["date"].nunique()),
                "wins": int((group["result"] == "win").sum()),
                "losses": int((group["r_multiple"] < 0).sum()),
                "win_rate": round(float((group["result"] == "win").mean() * 100), 2),
                "net_r": round(net_r, 2),
                "max_drawdown_r": round(max_dd, 2),
                "profit_factor": profit_factor(group),
                "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
            }
        )
    return rows


def build_overlap(filter_name: str, trades: pd.DataFrame) -> list[dict[str, object]]:
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
                    "filter": filter_name,
                    "group": group_name,
                    "date": date,
                    "left_label": labels[0],
                    "left_r": round(left_r, 2),
                    "right_label": labels[1],
                    "right_r": round(right_r, 2),
                    "combined_r": round(left_r + right_r, 2),
                    "both_lost": left_r < 0 and right_r < 0,
                    "both_won": left_r > 0 and right_r > 0,
                }
            )
    return rows


def max_drawdown(group: pd.DataFrame) -> float:
    ordered = group.sort_values("entry_time_dt")
    equity = ordered["r_multiple"].cumsum()
    drawdown = equity - equity.cummax()
    return float(drawdown.min()) if not drawdown.empty else 0.0


def profit_factor(group: pd.DataFrame) -> float:
    gross_win = float(group.loc[group["r_multiple"] > 0, "r_multiple"].sum())
    gross_loss = abs(float(group.loc[group["r_multiple"] < 0, "r_multiple"].sum()))
    return round(gross_win / gross_loss, 2) if gross_loss else 0.0


def write_report(
    thresholds: pd.DataFrame,
    comparison: pd.DataFrame,
    pair_comparison: pd.DataFrame,
    overlap: pd.DataFrame,
) -> None:
    overlap_summary = []
    if not overlap.empty:
        for keys, group in overlap.groupby(["filter", "group"]):
            overlap_summary.append(
                {
                    "filter": keys[0],
                    "group": keys[1],
                    "overlap_days": len(group),
                    "overlap_net_r": round(float(group["combined_r"].sum()), 2),
                    "both_lost": int(group["both_lost"].sum()),
                    "both_won": int(group["both_won"].sum()),
                }
            )
    lines = [
        "# Regime Filter Tests",
        "",
        "Filters use candidate-specific thresholds computed from full-data selected-candidate trades.",
        "",
        "## Thresholds",
        "",
        base_report.markdown_table(thresholds),
        "",
        "## Candidate Filter Comparison",
        "",
        base_report.markdown_table(comparison),
        "",
        "## Pair Filter Comparison",
        "",
        base_report.markdown_table(pair_comparison),
        "",
        "## Same-Day Overlap Summary",
        "",
        base_report.markdown_table(pd.DataFrame(overlap_summary)) if overlap_summary else "No overlap.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
