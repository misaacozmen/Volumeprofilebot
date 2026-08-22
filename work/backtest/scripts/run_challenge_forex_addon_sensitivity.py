from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_challenge_speed_sensitivity as speed
import run_corrected_engine_3month_report as base_report
from backtest.risk import apply_pair_risk_rule


CORE_SOURCE = ROOT / "outputs" / "reports" / "manual_rule_v2_validation" / "all_leg_trades.csv"
FOREX_SOURCE = ROOT / "outputs" / "reports" / "forex_filter_tests" / "all_trades.csv"
REPORT_DIR = ROOT / "outputs" / "reports" / "challenge_forex_addon_sensitivity"

FOREX_CANDIDATE = "eurusd_5m_3r_midpoint_0930_1030"
FOREX_FILTER = "exclude_monday"

SYSTEMS = {
    "CHALLENGE_CORE_V2": {"include_forex": False},
    "CHALLENGE_CORE_V2_PLUS_EURUSD_EX_MON": {"include_forex": True},
}


def main() -> None:
    reset_report_dir()
    core = load_core_trades()
    forex = load_forex_addon()

    run_rows = []
    stage_rows = []
    trade_counts = []
    for system_name, spec in SYSTEMS.items():
        trades = build_system_trades(core, forex if spec["include_forex"] else None, system_name)
        trade_counts.append(trade_count_row(system_name, trades))
        for account in speed.ACCOUNT_SCENARIOS:
            if not account.key.startswith(("A1", "A2")):
                continue
            for start_index in range(len(trades)):
                run, stages = speed.simulate_challenge(system_name, account, trades, start_index)
                run_rows.append(run)
                stage_rows.extend(stages)

    runs = pd.DataFrame(run_rows)
    stages = pd.DataFrame(stage_rows)
    summary = speed.build_summary(runs, stages)
    breakdown = speed.build_breakdown(runs)
    recommendation = build_recommendation(summary)
    deltas = build_deltas(summary)
    trade_counts_frame = pd.DataFrame(trade_counts)

    runs.to_csv(REPORT_DIR / "challenge_runs.csv", index=False)
    stages.to_csv(REPORT_DIR / "stage_details.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    breakdown.to_csv(REPORT_DIR / "outcome_breakdown.csv", index=False)
    recommendation.to_csv(REPORT_DIR / "recommendation.csv", index=False)
    deltas.to_csv(REPORT_DIR / "deltas.csv", index=False)
    trade_counts_frame.to_csv(REPORT_DIR / "trade_counts.csv", index=False)
    write_report(recommendation, deltas, trade_counts_frame, summary, breakdown)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_core_trades() -> pd.DataFrame:
    if not CORE_SOURCE.exists():
        raise SystemExit(f"Missing source trades: {CORE_SOURCE}")
    frame = pd.read_csv(CORE_SOURCE)
    selected = frame[(frame["system"] == "phase_selected") & (frame["manual_variant"] == "manual_v2")].copy()
    if selected.empty:
        raise SystemExit("No CHALLENGE_CORE_V2 trades found in manual_rule_v2_validation output.")
    selected["entry_time_dt"] = pd.to_datetime(selected["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    return selected


def load_forex_addon() -> pd.DataFrame:
    if not FOREX_SOURCE.exists():
        raise SystemExit(f"Missing forex trades: {FOREX_SOURCE}")
    frame = pd.read_csv(FOREX_SOURCE)
    selected = frame[(frame["candidate"] == FOREX_CANDIDATE) & (frame["filter"] == FOREX_FILTER)].copy()
    if selected.empty:
        raise SystemExit(f"No forex add-on trades found for {FOREX_CANDIDATE} / {FOREX_FILTER}.")
    selected.insert(0, "manual_variant", "forex_research_filter")
    selected.insert(0, "leg_key", "eurusd")
    selected.insert(0, "label", "EURUSD forex add-on")
    selected.insert(0, "system", "forex_addon")
    selected["entry_time_dt"] = pd.to_datetime(selected["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    return selected


def build_system_trades(core: pd.DataFrame, forex: pd.DataFrame | None, system_name: str) -> pd.DataFrame:
    frames = [core.copy()]
    if forex is not None:
        frames.append(forex.copy())
    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined["group"] = system_name
    capped = apply_pair_risk_rule(combined, -1.0)
    return capped.sort_values("entry_time_dt").reset_index(drop=True)


def trade_count_row(system_name: str, trades: pd.DataFrame) -> dict[str, object]:
    by_label = trades.groupby("label").size().to_dict()
    return {
        "system": system_name,
        "total_trades_after_daily_cap": len(trades),
        "nq_trades": int(by_label.get("NQ phase", 0)),
        "spx_trades": int(by_label.get("SPX phase", 0)),
        "eurusd_trades": int(by_label.get("EURUSD forex add-on", 0)),
        "first_trade_date": str(trades["entry_time_dt"].min().date()) if not trades.empty else "",
        "last_trade_date": str(trades["entry_time_dt"].max().date()) if not trades.empty else "",
    }


def build_recommendation(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in summary.iterrows():
        pass_rate = float(row["pass_rate"])
        phase_fails = int(row["phase1_failed"]) + int(row["phase2_failed"])
        avg_days = float(row["avg_total_trade_days"]) if row["avg_total_trade_days"] != "" else 999.0
        if row["system"] == "CHALLENGE_CORE_V2":
            decision = "baseline"
        elif pass_rate >= 88 and phase_fails <= 15 and avg_days < 28:
            decision = "candidate"
        elif pass_rate >= 85 and phase_fails <= 25:
            decision = "watch"
        else:
            decision = "not_preferred"
        rows.append(
            {
                "system": row["system"],
                "account": row["account"],
                "decision": decision,
                "pass_rate": row["pass_rate"],
                "phase_failures": phase_fails,
                "avg_total_trade_days": row["avg_total_trade_days"],
                "median_total_trade_days": row["median_total_trade_days"],
                "avg_total_calendar_days": row["avg_total_calendar_days"],
                "median_total_calendar_days": row["median_total_calendar_days"],
            }
        )
    return pd.DataFrame(rows).sort_values(["account", "system"])


def build_deltas(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for account, group in summary.groupby("account"):
        baseline = group[group["system"] == "CHALLENGE_CORE_V2"]
        addon = group[group["system"] == "CHALLENGE_CORE_V2_PLUS_EURUSD_EX_MON"]
        if baseline.empty or addon.empty:
            continue
        base = baseline.iloc[0]
        add = addon.iloc[0]
        rows.append(
            {
                "account": account,
                "delta_pass_rate": round(float(add["pass_rate"]) - float(base["pass_rate"]), 2),
                "delta_phase_failures": int(add["phase1_failed"] + add["phase2_failed"]) - int(base["phase1_failed"] + base["phase2_failed"]),
                "delta_avg_trade_days": round(float(add["avg_total_trade_days"]) - float(base["avg_total_trade_days"]), 2),
                "delta_median_trade_days": round(float(add["median_total_trade_days"]) - float(base["median_total_trade_days"]), 2),
                "delta_avg_calendar_days": round(float(add["avg_total_calendar_days"]) - float(base["avg_total_calendar_days"]), 2),
                "delta_median_calendar_days": round(float(add["median_total_calendar_days"]) - float(base["median_total_calendar_days"]), 2),
            }
        )
    return pd.DataFrame(rows)


def write_report(
    recommendation: pd.DataFrame,
    deltas: pd.DataFrame,
    trade_counts: pd.DataFrame,
    summary: pd.DataFrame,
    breakdown: pd.DataFrame,
) -> None:
    lines = [
        "# Challenge Forex Add-On Sensitivity",
        "",
        "Scope: challenge-only rolling phase test.",
        "",
        "Baseline is CHALLENGE_CORE_V2. Add-on test appends the parked forex research line:",
        "",
        f"- {FOREX_CANDIDATE}",
        f"- filter: {FOREX_FILTER}",
        "- pair-level daily -1R cap remains active after merging NQ, SPX, and EURUSD trades.",
        "",
        "Caveat: the EURUSD line is still research-only and was selected in-sample from forex filter tests.",
        "",
        "## Recommendation",
        "",
        base_report.markdown_table(recommendation),
        "",
        "## Delta Versus Baseline",
        "",
        base_report.markdown_table(deltas),
        "",
        "## Trade Counts After Daily Cap",
        "",
        base_report.markdown_table(trade_counts),
        "",
        "## Summary",
        "",
        base_report.markdown_table(summary),
        "",
        "## Outcome Breakdown",
        "",
        base_report.markdown_table(breakdown),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
