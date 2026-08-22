from __future__ import annotations

import html
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "dukascopy_stop_entry_model_tests"
REWARD_LEVELS = [2.0, 3.0]


@dataclass(frozen=True)
class Candidate:
    slug: str
    name: str
    symbol: str
    timeframe: str
    max_trades_per_day: int
    weekdays: str
    setup_type: str = "all"
    direction: str = "all"


@dataclass(frozen=True)
class Model:
    slug: str
    name: str
    entry_mode: str
    stop_model: str


CANDIDATES = [
    Candidate(
        slug="nq_3m_max1_no_tuesday",
        name="NQ - 3m - Tuesday haric - max 1 trade",
        symbol="DUKASCOPY_USATECHIDXUSD",
        timeframe="3m",
        max_trades_per_day=1,
        weekdays="Monday,Wednesday,Thursday,Friday",
    ),
    Candidate(
        slug="spx_5m_max2_tue_wed_fri_fvg",
        name="SPX - 5m - Tue/Wed/Fri - FVG - max 2 trade",
        symbol="DUKASCOPY_USA500IDXUSD",
        timeframe="5m",
        max_trades_per_day=2,
        weekdays="Tuesday,Wednesday,Friday",
        setup_type="fvg",
    ),
    Candidate(
        slug="silver_5m_max1_tue_wed_thu",
        name="Silver - 5m - Tue/Wed/Thu - max 1 trade",
        symbol="DUKASCOPY_XAGUSD",
        timeframe="5m",
        max_trades_per_day=1,
        weekdays="Tuesday,Wednesday,Thursday",
    ),
]


MODELS = [
    Model("sweep_wick_stop", "Sweep wick stop", "start", "sweep_wick"),
    Model("cisd_body_stop", "CISD body stop", "start", "cisd_body"),
    Model("fvg_opposite_edge_stop", "FVG opposite edge stop", "start", "fvg_opposite_edge"),
    Model("swing_based_stop", "Swing based stop", "start", "swing_based"),
    Model("fvg_body_end_entry", "FVG body-end entry + sweep wick stop", "body_end", "sweep_wick"),
]


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict[str, object]] = []
    monthly_rows: list[pd.DataFrame] = []
    all_trades: list[pd.DataFrame] = []

    for candidate in CANDIDATES:
        for model in MODELS:
            for reward_r in REWARD_LEVELS:
                out_dir = REPORT_DIR / candidate.slug / model.slug / f"rr_{int(reward_r)}"
                run_backtest(candidate, model, reward_r, out_dir)
                summary = pd.read_csv(out_dir / "summary.csv").iloc[0].to_dict()
                trades = pd.read_csv(out_dir / "trades.csv")
                monthly = pd.read_csv(out_dir / "monthly_stats.csv")

                summary_rows.append(build_summary_row(candidate, model, reward_r, out_dir, summary, trades, monthly))

                monthly.insert(0, "reward_r", reward_r)
                monthly.insert(0, "model", model.name)
                monthly.insert(0, "model_slug", model.slug)
                monthly.insert(0, "candidate", candidate.name)
                monthly.insert(0, "candidate_slug", candidate.slug)
                monthly_rows.append(monthly)

                if not trades.empty:
                    trades.insert(0, "reward_r", reward_r)
                    trades.insert(0, "model", model.name)
                    trades.insert(0, "model_slug", model.slug)
                    trades.insert(0, "candidate", candidate.name)
                    trades.insert(0, "candidate_slug", candidate.slug)
                    all_trades.append(trades)

    comparison = pd.DataFrame(summary_rows)
    monthly_detail = pd.concat(monthly_rows, ignore_index=True) if monthly_rows else pd.DataFrame()
    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(REPORT_DIR / "all_trades.csv", index=False)

    write_markdown_report(comparison)
    write_html_report(comparison, monthly_detail)
    print(f"Wrote report folder: {REPORT_DIR}")
    print(f"HTML: {REPORT_DIR / 'report.html'}")
    print(f"Markdown: {REPORT_DIR / 'report.md'}")
    print(f"Monthly detail: {REPORT_DIR / 'monthly_detail.csv'}")


def run_backtest(candidate: Candidate, model: Model, reward_r: float, out_dir: Path) -> None:
    if (out_dir / "summary.csv").exists() and (out_dir / "trades.csv").exists():
        print(f"Skipping existing {candidate.slug} {model.slug} {reward_r:.0f}R")
        return
    cmd = [
        sys.executable,
        "-m",
        "backtest.cli",
        "run",
        str(RAW_DIR),
        "--symbol",
        candidate.symbol,
        "--timeframe",
        candidate.timeframe,
        "--max-trades-per-day",
        str(candidate.max_trades_per_day),
        "--allowed-weekdays",
        candidate.weekdays,
        "--setup-type",
        candidate.setup_type,
        "--direction",
        candidate.direction,
        "--entry-mode",
        model.entry_mode,
        "--stop-model",
        model.stop_model,
        "--reward-r",
        str(reward_r),
        "--stop-management",
        "none",
        "--output-dir",
        str(out_dir),
    ]
    print(f"Running {candidate.slug} {model.slug} {reward_r:.0f}R")
    subprocess.run(cmd, cwd=ROOT, check=True)


def build_summary_row(
    candidate: Candidate,
    model: Model,
    reward_r: float,
    out_dir: Path,
    summary: dict[str, object],
    trades: pd.DataFrame,
    monthly: pd.DataFrame,
) -> dict[str, object]:
    max_dd = float(summary.get("max_drawdown_r", 0) or 0)
    net_r = float(summary.get("net_r", 0) or 0)
    return {
        "candidate": candidate.name,
        "candidate_slug": candidate.slug,
        "model": model.name,
        "model_slug": model.slug,
        "entry_mode": model.entry_mode,
        "stop_model": model.stop_model,
        "reward_r": reward_r,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "weekdays": candidate.weekdays,
        "setup_type": candidate.setup_type,
        "max_trades_per_day": candidate.max_trades_per_day,
        "trades": int(summary.get("total_trades", 0) or 0),
        "wins": int(summary.get("wins", 0) or 0),
        "losses": int(summary.get("losses", 0) or 0),
        "open_trades": int(summary.get("open_trades", 0) or 0),
        "win_rate": float(summary.get("win_rate", 0) or 0),
        "net_r": net_r,
        "avg_r": float(summary.get("avg_r", 0) or 0),
        "max_drawdown_r": max_dd,
        "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
        "profit_factor": float(summary.get("profit_factor", 0) or 0),
        "best_month": best_period(monthly, "month"),
        "worst_month": worst_period(monthly, "month"),
        "positive_months": int((monthly["net_r"] > 0).sum()) if not monthly.empty else 0,
        "negative_months": int((monthly["net_r"] < 0).sum()) if not monthly.empty else 0,
        "max_loss_streak": max_loss_streak(trades),
        "out_dir": str(out_dir.relative_to(ROOT)),
    }


def best_period(monthly: pd.DataFrame, column: str) -> str:
    if monthly.empty:
        return ""
    row = monthly.loc[monthly["net_r"].idxmax()]
    return f"{row[column]}:{float(row['net_r']):.2f}R"


def worst_period(monthly: pd.DataFrame, column: str) -> str:
    if monthly.empty:
        return ""
    row = monthly.loc[monthly["net_r"].idxmin()]
    return f"{row[column]}:{float(row['net_r']):.2f}R"


def max_loss_streak(trades: pd.DataFrame) -> int:
    max_loss = cur_loss = 0
    if trades.empty:
        return 0
    for result in trades["result"].astype(str):
        if result == "loss":
            cur_loss += 1
        else:
            cur_loss = 0
        max_loss = max(max_loss, cur_loss)
    return max_loss


def best_by_balance(group: pd.DataFrame) -> pd.Series:
    ranked = group.copy()
    ranked["balance_score"] = (
        ranked["net_r"].astype(float)
        + ranked["net_r_per_dd"].replace("", 0).astype(float) * 3
        + ranked["profit_factor"].astype(float) * 5
    )
    return ranked.sort_values(["balance_score", "net_r"], ascending=False).iloc[0]


def best_rows_by_candidate(comparison: pd.DataFrame) -> pd.DataFrame:
    selected = comparison.groupby("candidate", group_keys=False).apply(best_by_balance)
    if "candidate" not in selected.columns:
        selected = selected.reset_index()
    return selected.reset_index(drop=True)


def write_markdown_report(comparison: pd.DataFrame) -> None:
    selected = best_rows_by_candidate(comparison)
    top_columns = [
        "candidate",
        "model",
        "reward_r",
        "trades",
        "win_rate",
        "net_r",
        "max_drawdown_r",
        "net_r_per_dd",
        "profit_factor",
        "best_month",
        "worst_month",
    ]
    grid_columns = [
        "candidate",
        "model",
        "reward_r",
        "trades",
        "win_rate",
        "net_r",
        "avg_r",
        "max_drawdown_r",
        "net_r_per_dd",
        "profit_factor",
        "positive_months",
        "negative_months",
        "max_loss_streak",
    ]
    lines = [
        "# Dukascopy Stop / Entry Model Report",
        "",
        "Data range: 2022-01-02 18:00 NY -> 2026-07-02 23:55/23:57 NY",
        "RR grid: 2R and 3R only. Stop management: none.",
        "",
        "Monthly detail with trade count, TP, SL, and monthly WR is written to `monthly_detail.csv`.",
        "",
        "## Best Balance Per Candidate",
        "",
        markdown_table(selected[top_columns]),
        "",
        "## Full Model Grid",
        "",
        markdown_table(comparison[grid_columns]),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_html_report(comparison: pd.DataFrame, monthly_detail: pd.DataFrame) -> None:
    selected = best_rows_by_candidate(comparison)
    cards = "\n".join(summary_card(row) for _, row in selected.iterrows())
    tables = "\n".join(candidate_table(name, group) for name, group in comparison.groupby("candidate"))
    monthly_tables = "\n".join(monthly_detail_table(key, group) for key, group in monthly_detail.groupby(["candidate", "model", "reward_r"]))
    html_doc = f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dukascopy Stop / Entry Model Report</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #fff;
      --ink: #17202a;
      --muted: #667085;
      --line: #d9e0e8;
      --good: #117a55;
      --bad: #b42318;
      --accent: #2457c5;
      --soft: #edf3ff;
    }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.5 "Segoe UI", Arial, sans-serif; }}
    header {{ background: #162033; color: white; padding: 28px 36px; }}
    header h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    header p {{ margin: 0; color: #d7deea; }}
    main {{ max-width: 1320px; margin: 0 auto; padding: 28px 24px 48px; }}
    h2 {{ margin: 30px 0 12px; font-size: 20px; }}
    h3 {{ margin: 0 0 10px; font-size: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }}
    .card, .section, details {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; box-shadow: 0 1px 2px rgba(16,24,40,.04); }}
    .muted {{ color: var(--muted); }}
    .metric-row {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 12px; }}
    .metric {{ background: #f8fafc; border: 1px solid var(--line); border-radius: 6px; padding: 9px; }}
    .metric b {{ display: block; font-size: 18px; }}
    .tag {{ display: inline-block; padding: 3px 8px; border-radius: 999px; background: var(--soft); color: var(--accent); font-weight: 600; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); margin-bottom: 14px; }}
    th, td {{ padding: 8px 9px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; white-space: normal; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; background: #f8fafc; }}
    summary {{ cursor: pointer; font-weight: 700; }}
    .pos {{ color: var(--good); font-weight: 700; }}
    .neg {{ color: var(--bad); font-weight: 700; }}
    @media (max-width: 900px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .metric-row {{ grid-template-columns: repeat(2, 1fr); }}
      main {{ padding: 20px 12px 36px; }}
      header {{ padding: 22px 18px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Dukascopy Stop / Entry Model Report</h1>
    <p>RR: 2R / 3R | 3 candidates | 5 stop-entry models | Stop management: none</p>
  </header>
  <main>
    <h2>Best Balance Per Candidate</h2>
    <div class="grid">{cards}</div>
    <h2>Full Model Grid</h2>
    {tables}
    <h2>Monthly Detail</h2>
    {monthly_tables}
  </main>
</body>
</html>"""
    (REPORT_DIR / "report.html").write_text(html_doc, encoding="utf-8")


def summary_card(row: pd.Series) -> str:
    return f"""
    <section class="card">
      <h3>{esc(row['candidate'])}</h3>
      <span class="tag">{esc(row['model'])} / {float(row['reward_r']):.0f}R</span>
      <div class="metric-row">
        <div class="metric"><span class="muted">Net R</span><b class="{klass(row['net_r'])}">{float(row['net_r']):.1f}</b></div>
        <div class="metric"><span class="muted">DD</span><b>{float(row['max_drawdown_r']):.1f}</b></div>
        <div class="metric"><span class="muted">R/DD</span><b>{row['net_r_per_dd']}</b></div>
        <div class="metric"><span class="muted">PF</span><b>{float(row['profit_factor']):.2f}</b></div>
      </div>
      <p class="muted">Trades: {int(row['trades'])} | WR: {float(row['win_rate']):.2f}% | Max loss streak: {int(row['max_loss_streak'])}</p>
    </section>"""


def candidate_table(name: str, group: pd.DataFrame) -> str:
    rows = []
    for _, row in group.sort_values(["model", "reward_r"]).iterrows():
        rows.append(
            "<tr>"
            f"<td>{esc(row['model'])}</td>"
            f"<td>{float(row['reward_r']):.0f}R</td>"
            f"<td>{int(row['trades'])}</td>"
            f"<td>{float(row['win_rate']):.2f}%</td>"
            f"<td class='{klass(row['net_r'])}'>{float(row['net_r']):.1f}</td>"
            f"<td>{float(row['avg_r']):.2f}</td>"
            f"<td>{float(row['max_drawdown_r']):.1f}</td>"
            f"<td>{row['net_r_per_dd']}</td>"
            f"<td>{float(row['profit_factor']):.2f}</td>"
            f"<td>{int(row['positive_months'])}/{int(row['negative_months'])}</td>"
            f"<td>{int(row['max_loss_streak'])}</td>"
            "</tr>"
        )
    return f"""
    <section class="section">
      <h3>{esc(name)}</h3>
      <table>
        <thead><tr><th>Model</th><th>RR</th><th>Trades</th><th>WR</th><th>Net R</th><th>Avg R</th><th>DD</th><th>R/DD</th><th>PF</th><th>+/- Months</th><th>Loss Streak</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>"""


def monthly_detail_table(key: tuple[object, object, object], group: pd.DataFrame) -> str:
    candidate, model, reward_r = key
    rows = []
    for _, row in group.sort_values("month").iterrows():
        rows.append(
            "<tr>"
            f"<td>{esc(row['month'])}</td>"
            f"<td>{int(row['total_trades'])}</td>"
            f"<td>{int(row['tp'])}</td>"
            f"<td>{int(row['sl'])}</td>"
            f"<td>{float(row['win_rate']):.2f}%</td>"
            f"<td class='{klass(row['net_r'])}'>{float(row['net_r']):.1f}</td>"
            "</tr>"
        )
    return f"""
    <details>
      <summary>{esc(candidate)} | {esc(model)} | {float(reward_r):.0f}R</summary>
      <table>
        <thead><tr><th>Month</th><th>Trades</th><th>TP</th><th>SL</th><th>WR</th><th>Net R</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </details>"""


def markdown_table(frame: pd.DataFrame) -> str:
    headers = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for _, row in frame.iterrows():
        values = [markdown_cell(row[column]) for column in frame.columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def markdown_cell(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        value = f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value).replace("|", "\\|")


def klass(value: object) -> str:
    return "pos" if float(value) >= 0 else "neg"


def esc(value: object) -> str:
    return html.escape(str(value))


if __name__ == "__main__":
    main()
