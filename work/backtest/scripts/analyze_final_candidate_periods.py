from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
from backtest.risk import apply_pair_risk_rule


SOURCE_DIR = ROOT / "outputs" / "reports" / "followup_combination_tests"
REPORT_DIR = ROOT / "outputs" / "reports" / "final_candidate_period_analysis"

SYSTEM_SPECS = [
    {
        "system": "funded_selected",
        "risk_rule": "daily_loss_cap_minus_1r",
        "legs": [
            ("NQ funded", "trades_funded_nq_baseline.csv"),
            ("SPX funded", "trades_spx_funded_latest_entry_1030.csv"),
        ],
    },
    {
        "system": "phase_selected",
        "risk_rule": "daily_loss_cap_minus_1r",
        "legs": [
            ("NQ phase", "trades_nq_phase_min_touches_3.csv"),
            ("SPX phase", "trades_spx_phase_latest_entry_1030.csv"),
        ],
    },
]


def main() -> None:
    reset_report_dir()
    all_raw = load_system_trades()
    all_capped = apply_system_risk(all_raw)

    candidate_monthly = period_stats(all_raw, ["system", "leg", "month"])
    candidate_yearly = period_stats(all_raw, ["system", "leg", "year"])
    pair_monthly = period_stats(all_capped, ["system", "month"])
    pair_yearly = period_stats(all_capped, ["system", "year"])
    rankings = build_rankings(candidate_monthly, candidate_yearly, pair_monthly, pair_yearly)
    drawdown = build_drawdown_tables(all_capped)

    all_raw.to_csv(REPORT_DIR / "all_trades_raw.csv", index=False)
    all_capped.to_csv(REPORT_DIR / "all_trades_after_daily_cap.csv", index=False)
    candidate_monthly.to_csv(REPORT_DIR / "candidate_monthly.csv", index=False)
    candidate_yearly.to_csv(REPORT_DIR / "candidate_yearly.csv", index=False)
    pair_monthly.to_csv(REPORT_DIR / "pair_monthly_after_daily_cap.csv", index=False)
    pair_yearly.to_csv(REPORT_DIR / "pair_yearly_after_daily_cap.csv", index=False)
    rankings.to_csv(REPORT_DIR / "rankings.csv", index=False)
    drawdown.to_csv(REPORT_DIR / "drawdown_by_system.csv", index=False)
    write_report(candidate_monthly, candidate_yearly, pair_monthly, pair_yearly, rankings, drawdown)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_system_trades() -> pd.DataFrame:
    frames = []
    for spec in SYSTEM_SPECS:
        for leg, filename in spec["legs"]:
            path = SOURCE_DIR / filename
            if not path.exists():
                raise SystemExit(f"Missing source trades: {path}")
            frame = pd.read_csv(path)
            frame = frame.copy()
            frame["system"] = spec["system"]
            frame["risk_rule"] = spec["risk_rule"]
            frame["leg"] = leg
            frame["entry_dt"] = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
            frame["month"] = frame["entry_dt"].dt.strftime("%Y-%m")
            frame["year"] = frame["entry_dt"].dt.year.astype(str)
            frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def apply_system_risk(trades: pd.DataFrame) -> pd.DataFrame:
    capped_frames = []
    for system, group in trades.groupby("system"):
        capped = group.copy()
        capped["group"] = system
        capped["label"] = capped["leg"]
        capped["entry_time_dt"] = capped["entry_dt"]
        capped = apply_pair_risk_rule(capped, -1.0)
        capped["system"] = system
        capped["month"] = capped["entry_dt"].dt.strftime("%Y-%m")
        capped["year"] = capped["entry_dt"].dt.year.astype(str)
        capped_frames.append(capped)
    return pd.concat(capped_frames, ignore_index=True)


def period_stats(frame: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows = []
    for keys, group in frame.groupby(group_cols):
        if not isinstance(keys, tuple):
            keys = (keys,)
        wins = int((group["result"] == "win").sum())
        losses = int((group["r_multiple"] < 0).sum())
        net_r = float(group["r_multiple"].sum())
        row = {column: value for column, value in zip(group_cols, keys)}
        row.update(
            {
                "trades": int(len(group)),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / len(group) * 100, 2) if len(group) else 0.0,
                "net_r": round(net_r, 2),
                "avg_r": round(net_r / len(group), 2) if len(group) else 0.0,
                "profit_factor": profit_factor(group),
                "worst_trade_r": round(float(group["r_multiple"].min()), 2) if len(group) else 0.0,
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def profit_factor(group: pd.DataFrame) -> float:
    gross_win = float(group.loc[group["r_multiple"] > 0, "r_multiple"].sum())
    gross_loss = abs(float(group.loc[group["r_multiple"] < 0, "r_multiple"].sum()))
    return round(gross_win / gross_loss, 2) if gross_loss else 0.0


def build_rankings(
    candidate_monthly: pd.DataFrame,
    candidate_yearly: pd.DataFrame,
    pair_monthly: pd.DataFrame,
    pair_yearly: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for scope, frame, name_cols, period_col in [
        ("candidate_month", candidate_monthly, ["system", "leg"], "month"),
        ("candidate_year", candidate_yearly, ["system", "leg"], "year"),
        ("pair_month", pair_monthly, ["system"], "month"),
        ("pair_year", pair_yearly, ["system"], "year"),
    ]:
        for keys, group in frame.groupby(name_cols):
            if not isinstance(keys, tuple):
                keys = (keys,)
            name = " / ".join(str(item) for item in keys)
            best = group.sort_values(["net_r", "win_rate"], ascending=[False, False]).head(5)
            worst = group.sort_values(["net_r", "win_rate"], ascending=[True, True]).head(5)
            for rank, (_, row) in enumerate(best.iterrows(), start=1):
                rows.append(ranking_row(scope, name, "best", rank, row, period_col))
            for rank, (_, row) in enumerate(worst.iterrows(), start=1):
                rows.append(ranking_row(scope, name, "worst", rank, row, period_col))
    return pd.DataFrame(rows)


def ranking_row(scope: str, name: str, side: str, rank: int, row: pd.Series, period_col: str) -> dict[str, object]:
    return {
        "scope": scope,
        "name": name,
        "side": side,
        "rank": rank,
        "period": row[period_col],
        "trades": row["trades"],
        "wins": row["wins"],
        "losses": row["losses"],
        "win_rate": row["win_rate"],
        "net_r": row["net_r"],
        "profit_factor": row["profit_factor"],
    }


def build_drawdown_tables(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for system, group in trades.groupby("system"):
        ordered = group.sort_values("entry_dt")
        equity = ordered["r_multiple"].cumsum()
        drawdown = equity - equity.cummax()
        daily = ordered.groupby("date")["r_multiple"].sum()
        rows.append(
            {
                "system": system,
                "trades": len(ordered),
                "unique_dates": int(ordered["date"].nunique()),
                "net_r": round(float(ordered["r_multiple"].sum()), 2),
                "max_drawdown_r": round(float(drawdown.min()), 2) if len(drawdown) else 0.0,
                "worst_day_r": round(float(daily.min()), 2) if len(daily) else 0.0,
                "best_day_r": round(float(daily.max()), 2) if len(daily) else 0.0,
                "negative_months": int((ordered.groupby("month")["r_multiple"].sum() < 0).sum()),
                "positive_months": int((ordered.groupby("month")["r_multiple"].sum() > 0).sum()),
                "negative_years": int((ordered.groupby("year")["r_multiple"].sum() < 0).sum()),
                "positive_years": int((ordered.groupby("year")["r_multiple"].sum() > 0).sum()),
            }
        )
    return pd.DataFrame(rows)


def write_report(
    candidate_monthly: pd.DataFrame,
    candidate_yearly: pd.DataFrame,
    pair_monthly: pd.DataFrame,
    pair_yearly: pd.DataFrame,
    rankings: pd.DataFrame,
    drawdown: pd.DataFrame,
) -> None:
    lines = [
        "# Final Candidate Period Analysis",
        "",
        "Systems:",
        "",
        "- funded_selected: NQ funded unchanged + SPX funded latest_entry_time=10:30 + pair daily loss cap -1R",
        "- phase_selected: NQ phase strong_swing_min_touches=3 + SPX phase latest_entry_time=10:30 + pair daily loss cap -1R",
        "",
        "## System Summary After Daily Cap",
        "",
        base_report.markdown_table(drawdown),
        "",
        "## Pair Yearly After Daily Cap",
        "",
        base_report.markdown_table(pair_yearly),
        "",
        "## Pair Worst/Best Months After Daily Cap",
        "",
        base_report.markdown_table(rankings[rankings["scope"] == "pair_month"]),
        "",
        "## Candidate Yearly Before Pair Cap",
        "",
        base_report.markdown_table(candidate_yearly),
        "",
        "## Candidate Worst/Best Months Before Pair Cap",
        "",
        base_report.markdown_table(rankings[rankings["scope"] == "candidate_month"]),
        "",
        "## Pair Monthly Detail After Daily Cap",
        "",
        base_report.markdown_table(pair_monthly),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
