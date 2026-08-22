from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report


SOURCE_DIR = ROOT / "outputs" / "reports" / "selected_candidates_full_data"
REPORT_DIR = ROOT / "outputs" / "reports" / "selected_candidates_period_analysis"


def main() -> None:
    reset_report_dir()
    trades = pd.read_csv(SOURCE_DIR / "all_trades.csv")
    trades["entry_dt"] = pd.to_datetime(trades["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    trades["month"] = trades["entry_dt"].dt.strftime("%Y-%m")
    trades["year"] = trades["entry_dt"].dt.year.astype(str)

    candidate_monthly = period_stats(trades, ["group", "candidate_slug", "label", "month"])
    candidate_yearly = period_stats(trades, ["group", "candidate_slug", "label", "year"])
    pair_monthly = pair_period_stats(trades, "month")
    pair_yearly = pair_period_stats(trades, "year")
    rankings = build_rankings(candidate_monthly, candidate_yearly, pair_monthly, pair_yearly)

    candidate_monthly.to_csv(REPORT_DIR / "candidate_monthly.csv", index=False)
    candidate_yearly.to_csv(REPORT_DIR / "candidate_yearly.csv", index=False)
    pair_monthly.to_csv(REPORT_DIR / "pair_monthly.csv", index=False)
    pair_yearly.to_csv(REPORT_DIR / "pair_yearly.csv", index=False)
    rankings.to_csv(REPORT_DIR / "rankings.csv", index=False)
    write_report(candidate_monthly, candidate_yearly, pair_monthly, pair_yearly, rankings)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


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
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def pair_period_stats(trades: pd.DataFrame, period_col: str) -> pd.DataFrame:
    rows = []
    for (group_name, period), group in trades.groupby(["group", period_col]):
        wins = int((group["result"] == "win").sum())
        losses = int((group["r_multiple"] < 0).sum())
        net_r = float(group["r_multiple"].sum())
        active_symbols = ",".join(sorted(group["label"].unique()))
        rows.append(
            {
                "group": group_name,
                period_col: period,
                "active_symbols": active_symbols,
                "trades": int(len(group)),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / len(group) * 100, 2) if len(group) else 0.0,
                "net_r": round(net_r, 2),
                "avg_r": round(net_r / len(group), 2) if len(group) else 0.0,
                "profit_factor": profit_factor(group),
            }
        )
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
    for label, frame, name_col, period_col in [
        ("candidate_month", candidate_monthly, "label", "month"),
        ("candidate_year", candidate_yearly, "label", "year"),
        ("pair_month", pair_monthly, "group", "month"),
        ("pair_year", pair_yearly, "group", "year"),
    ]:
        for name, group in frame.groupby(name_col):
            best = group.sort_values(["net_r", "win_rate"], ascending=[False, False]).head(5)
            worst = group.sort_values(["net_r", "win_rate"], ascending=[True, True]).head(5)
            for rank, (_, row) in enumerate(best.iterrows(), start=1):
                rows.append(ranking_row(label, name, "best", rank, row, period_col))
            for rank, (_, row) in enumerate(worst.iterrows(), start=1):
                rows.append(ranking_row(label, name, "worst", rank, row, period_col))
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


def write_report(
    candidate_monthly: pd.DataFrame,
    candidate_yearly: pd.DataFrame,
    pair_monthly: pd.DataFrame,
    pair_yearly: pd.DataFrame,
    rankings: pd.DataFrame,
) -> None:
    lines = [
        "# Selected Candidates Period Analysis",
        "",
        "Source: selected_candidates_full_data/all_trades.csv",
        "",
        "## Pair Yearly",
        "",
        base_report.markdown_table(pair_yearly),
        "",
        "## Candidate Yearly",
        "",
        base_report.markdown_table(candidate_yearly),
        "",
        "## Best/Worst Pair Months",
        "",
        base_report.markdown_table(rankings[rankings["scope"] == "pair_month"]),
        "",
        "## Best/Worst Candidate Months",
        "",
        base_report.markdown_table(rankings[rankings["scope"] == "candidate_month"]),
        "",
        "## Pair Monthly Detail",
        "",
        base_report.markdown_table(pair_monthly),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
