from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report


SOURCE_DIR = ROOT / "outputs" / "reports" / "regime_filter_tests"
REPORT_DIR = ROOT / "outputs" / "reports" / "filter_risk_profile"
FILTERS = ["baseline", "no_first30_range_high", "no_high_range_or_low_directionality"]
RISK_LEVELS = [0.25, 0.5, 1.0]


def main() -> None:
    reset_report_dir()
    trades = load_filtered_trades()
    equity_rows = []
    risk_rows = []
    month_rows = []
    live_rows = []

    for (filter_name, group), data in trades.groupby(["filter", "group"]):
        ordered = data.sort_values("entry_time_dt").reset_index(drop=True)
        equity_rows.append(equity_summary(filter_name, group, ordered))
        for risk in RISK_LEVELS:
            risk_rows.append(risk_summary(filter_name, group, ordered, risk))
        month_rows.extend(monthly_drawdown(filter_name, group, ordered))
        live_rows.append(live_safety_summary(filter_name, group, ordered))

    equity = pd.DataFrame(equity_rows)
    risk = pd.DataFrame(risk_rows)
    monthly = pd.DataFrame(month_rows)
    live = pd.DataFrame(live_rows)
    equity.to_csv(REPORT_DIR / "equity_summary.csv", index=False)
    risk.to_csv(REPORT_DIR / "risk_percent_summary.csv", index=False)
    monthly.to_csv(REPORT_DIR / "monthly_drawdown.csv", index=False)
    live.to_csv(REPORT_DIR / "live_safety_summary.csv", index=False)
    write_report(equity, risk, monthly, live)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_filtered_trades() -> pd.DataFrame:
    enriched = pd.read_csv(ROOT / "outputs" / "reports" / "bad_regime_diagnostics" / "enriched_trades.csv")
    thresholds = pd.read_csv(SOURCE_DIR / "thresholds.csv")
    rows = []
    for filter_name in FILTERS:
        filtered = apply_filter(enriched, thresholds, filter_name)
        filtered = filtered.copy()
        filtered["filter"] = filter_name
        rows.append(filtered)
    trades = pd.concat(rows, ignore_index=True)
    trades["entry_time_dt"] = pd.to_datetime(trades["entry_time_dt"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    trades["month"] = trades["entry_time_dt"].dt.strftime("%Y-%m")
    return trades


def apply_filter(trades: pd.DataFrame, thresholds: pd.DataFrame, filter_name: str) -> pd.DataFrame:
    if filter_name == "baseline":
        return trades.copy()
    merged = trades.merge(thresholds, on=["group", "candidate_slug", "label"], how="left")
    keep = pd.Series(True, index=merged.index)
    if filter_name in {"no_first30_range_high", "no_high_range_or_low_directionality"}:
        keep &= merged["first30_range"] <= merged["first30_range_q75"]
    if filter_name == "no_high_range_or_low_directionality":
        keep &= merged["first30_directionality"] >= merged["first30_directionality_q25"]
    return merged[keep].drop(columns=["first30_range_q75", "first30_directionality_q25"])


def equity_summary(filter_name: str, group: str, data: pd.DataFrame) -> dict[str, object]:
    equity = data["r_multiple"].cumsum()
    dd = equity - equity.cummax()
    return {
        "filter": filter_name,
        "group": group,
        "trades": len(data),
        "unique_dates": data["date"].nunique(),
        "net_r": round(float(data["r_multiple"].sum()), 2),
        "max_drawdown_r": round(float(dd.min()), 2) if not dd.empty else 0,
        "r_per_trade": round(float(data["r_multiple"].mean()), 3) if len(data) else 0,
        "win_rate": round(float((data["result"] == "win").mean() * 100), 2) if len(data) else 0,
        "max_consecutive_losses": max_consecutive_losses(data),
        "max_consecutive_losing_days": max_consecutive_losing_days(data),
        "worst_month_r": round(float(data.groupby("month")["r_multiple"].sum().min()), 2) if len(data) else 0,
        "best_month_r": round(float(data.groupby("month")["r_multiple"].sum().max()), 2) if len(data) else 0,
    }


def risk_summary(filter_name: str, group: str, data: pd.DataFrame, risk_pct: float) -> dict[str, object]:
    equity = data["r_multiple"].cumsum()
    dd = equity - equity.cummax()
    net_r = float(data["r_multiple"].sum())
    max_dd = float(dd.min()) if not dd.empty else 0
    return {
        "filter": filter_name,
        "group": group,
        "risk_per_trade_pct": risk_pct,
        "net_return_pct": round(net_r * risk_pct, 2),
        "max_drawdown_pct": round(max_dd * risk_pct, 2),
        "worst_month_pct": round(float(data.groupby("month")["r_multiple"].sum().min()) * risk_pct, 2) if len(data) else 0,
        "best_month_pct": round(float(data.groupby("month")["r_multiple"].sum().max()) * risk_pct, 2) if len(data) else 0,
    }


def monthly_drawdown(filter_name: str, group: str, data: pd.DataFrame) -> list[dict[str, object]]:
    rows = []
    for month, month_data in data.groupby("month"):
        ordered = month_data.sort_values("entry_time_dt")
        equity = ordered["r_multiple"].cumsum()
        dd = equity - equity.cummax()
        rows.append(
            {
                "filter": filter_name,
                "group": group,
                "month": month,
                "trades": len(month_data),
                "net_r": round(float(month_data["r_multiple"].sum()), 2),
                "month_max_drawdown_r": round(float(dd.min()), 2) if not dd.empty else 0,
            }
        )
    return rows


def live_safety_summary(filter_name: str, group: str, data: pd.DataFrame) -> dict[str, object]:
    before_10 = data[data["entry_minutes"] < 10 * 60]
    before_10_30 = data[data["entry_minutes"] < 10 * 60 + 30]
    return {
        "filter": filter_name,
        "group": group,
        "trades": len(data),
        "entry_before_1000": len(before_10),
        "entry_before_1000_pct": round(len(before_10) / len(data) * 100, 2) if len(data) else 0,
        "entry_before_1030": len(before_10_30),
        "entry_before_1030_pct": round(len(before_10_30) / len(data) * 100, 2) if len(data) else 0,
        "pre_1000_net_r": round(float(before_10["r_multiple"].sum()), 2),
        "pre_1030_net_r": round(float(before_10_30["r_multiple"].sum()), 2),
    }


def max_consecutive_losses(data: pd.DataFrame) -> int:
    max_run = current = 0
    for value in data.sort_values("entry_time_dt")["r_multiple"]:
        if value < 0:
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    return max_run


def max_consecutive_losing_days(data: pd.DataFrame) -> int:
    daily = data.groupby("date")["r_multiple"].sum().reset_index().sort_values("date")
    max_run = current = 0
    for value in daily["r_multiple"]:
        if value < 0:
            current += 1
            max_run = max(max_run, current)
        elif value > 0:
            current = 0
    return max_run


def write_report(equity: pd.DataFrame, risk: pd.DataFrame, monthly: pd.DataFrame, live: pd.DataFrame) -> None:
    lines = [
        "# Filter Risk Profile",
        "",
        "This report translates R drawdown into account-percent drawdown and checks live-safety caveats.",
        "",
        "## Equity Summary",
        "",
        base_report.markdown_table(equity),
        "",
        "## Risk Percent Summary",
        "",
        base_report.markdown_table(risk),
        "",
        "## Live Safety",
        "",
        "The first30_range filter is only known after 10:00 NY. Any trades entered before 10:00 would be lookahead if the filter is applied as a day-level pre-filter.",
        "",
        base_report.markdown_table(live),
        "",
        "## Worst Monthly Drawdowns",
        "",
        base_report.markdown_table(monthly.sort_values("month_max_drawdown_r").head(20)),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
