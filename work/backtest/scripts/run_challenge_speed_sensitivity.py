from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_final_candidate_fund_account_lifecycle as lifecycle
from backtest.risk import apply_pair_risk_rule


SOURCE_TRADES = ROOT / "outputs" / "reports" / "manual_rule_v2_validation" / "all_leg_trades.csv"
REPORT_DIR = ROOT / "outputs" / "reports" / "challenge_speed_sensitivity"

SYSTEMS = {
    "CHALLENGE_CORE_V2": "phase_selected",
    "CHALLENGE_FAST_V2": "phase_selected_frequency",
}

ACCOUNT_SCENARIOS = [
    lifecycle.AccountModel("A1_RISK_1P00", "A1 7%+5% DD10 risk1.00", (7.0, 5.0), 10.0, 1.0, "static"),
    lifecycle.AccountModel("A1_RISK_1P25", "A1 7%+5% DD10 risk1.25", (7.0, 5.0), 10.0, 1.25, "static"),
    lifecycle.AccountModel("A1_RISK_1P50", "A1 7%+5% DD10 risk1.50", (7.0, 5.0), 10.0, 1.5, "static"),
    lifecycle.AccountModel("A2_RISK_1P00", "A2 6%+5% DD8 risk1.00", (6.0, 5.0), 8.0, 1.0, "static"),
    lifecycle.AccountModel("A2_RISK_1P25", "A2 6%+5% DD8 risk1.25", (6.0, 5.0), 8.0, 1.25, "static"),
    lifecycle.AccountModel("A2_RISK_1P50", "A2 6%+5% DD8 risk1.50", (6.0, 5.0), 8.0, 1.5, "static"),
]


def main() -> None:
    reset_report_dir()
    source = load_source_trades()
    run_rows = []
    stage_rows = []
    for operating_name, source_system in SYSTEMS.items():
        trades = pair_trades(source, source_system)
        for account in ACCOUNT_SCENARIOS:
            if not account.key.startswith("A1") and "A2" not in account.key:
                continue
            if account.key.startswith("A1") != account.label.startswith("A1"):
                continue
            for start_index in range(len(trades)):
                run, stages = simulate_challenge(operating_name, account, trades, start_index)
                run_rows.append(run)
                stage_rows.extend(stages)

    runs = pd.DataFrame(run_rows)
    stages = pd.DataFrame(stage_rows)
    summary = build_summary(runs, stages)
    breakdown = build_breakdown(runs)
    recommendation = build_recommendation(summary)

    runs.to_csv(REPORT_DIR / "challenge_runs.csv", index=False)
    stages.to_csv(REPORT_DIR / "stage_details.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    breakdown.to_csv(REPORT_DIR / "outcome_breakdown.csv", index=False)
    recommendation.to_csv(REPORT_DIR / "recommendation.csv", index=False)
    write_report(recommendation, summary, breakdown)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_source_trades() -> pd.DataFrame:
    if not SOURCE_TRADES.exists():
        raise SystemExit(f"Missing source trades: {SOURCE_TRADES}")
    frame = pd.read_csv(SOURCE_TRADES)
    frame["entry_time_dt"] = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    return frame


def pair_trades(source: pd.DataFrame, system: str) -> pd.DataFrame:
    selected = source[(source["system"] == system) & (source["manual_variant"] == "manual_v2")].copy()
    selected["group"] = system
    capped = apply_pair_risk_rule(selected, -1.0)
    return capped.sort_values("entry_time_dt").reset_index(drop=True)


def simulate_challenge(
    operating_name: str,
    account: lifecycle.AccountModel,
    trades: pd.DataFrame,
    start_index: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    current_index = start_index
    stages = []
    phase_1_status = "not_started"
    phase_2_status = "not_started"
    outcome = "incomplete"
    fail_stage = ""
    fail_reason = ""

    for phase_number, target_pct in enumerate(account.targets_pct, start=1):
        result = lifecycle.simulate_target_stage(
            trades=trades,
            start_index=current_index,
            account=account,
            stage_name=f"phase_{phase_number}",
            target_pct=target_pct,
        )
        stages.append(stage_row(operating_name, account, start_index, result))
        if phase_number == 1:
            phase_1_status = result["status"]
        else:
            phase_2_status = result["status"]
        if result["status"] == "passed":
            current_index = int(result["next_index"])
            if current_index >= len(trades) and phase_number < len(account.targets_pct):
                outcome = "incomplete"
                break
            continue
        if result["status"] == "failed":
            outcome = f"failed_{result['stage']}"
            fail_stage = str(result["stage"])
            fail_reason = str(result["reason"])
        else:
            outcome = "incomplete"
        break
    else:
        outcome = "passed_all_phases"

    return (
        {
            "system": operating_name,
            "account": account.label,
            "account_key": account.key,
            "risk_per_trade_pct": account.risk_per_trade_pct,
            "start_index": start_index,
            "start_date": str(trades.iloc[start_index]["entry_time_dt"].date()),
            "outcome": outcome,
            "phase_1_status": phase_1_status,
            "phase_2_status": phase_2_status,
            "passed_all_phases": outcome == "passed_all_phases",
            "fail_stage": fail_stage,
            "fail_reason": fail_reason,
        },
        stages,
    )


def stage_row(
    operating_name: str,
    account: lifecycle.AccountModel,
    start_index: int,
    result: dict[str, object],
) -> dict[str, object]:
    return {
        "system": operating_name,
        "account": account.label,
        "account_key": account.key,
        "rolling_start_index": start_index,
        **result,
    }


def build_summary(runs: pd.DataFrame, stages: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (system, account), group in runs.groupby(["system", "account"]):
        stage_group = stages[(stages["system"] == system) & (stages["account"] == account)]
        phase_1 = stage_group[stage_group["stage"] == "phase_1"]
        phase_2 = stage_group[stage_group["stage"] == "phase_2"]
        passed = group[group["passed_all_phases"]]
        total_pass = total_duration_rows(stage_group)
        rows.append(
            {
                "system": system,
                "account": account,
                "starts": len(group),
                "passed_all_phases": len(passed),
                "pass_rate": pct(len(passed), len(group)),
                "phase1_failed": int((group["outcome"] == "failed_phase_1").sum()),
                "phase2_failed": int((group["outcome"] == "failed_phase_2").sum()),
                "incomplete": int((group["outcome"] == "incomplete").sum()),
                "avg_phase1_trade_days": avg_pass_days(phase_1),
                "avg_phase2_trade_days": avg_pass_days(phase_2),
                "avg_total_trade_days": avg_number(total_pass["trade_days"]),
                "median_total_trade_days": median_number(total_pass["trade_days"]),
                "avg_total_trades": avg_number(total_pass["trades"]),
                "median_total_trades": median_number(total_pass["trades"]),
                "avg_total_calendar_days": avg_number(total_pass["calendar_days"]),
                "median_total_calendar_days": median_number(total_pass["calendar_days"]),
            }
        )
    return pd.DataFrame(rows).sort_values(["system", "account"])


def total_duration_rows(stages: pd.DataFrame) -> pd.DataFrame:
    passed = stages[stages["status"] == "passed"]
    pivot = passed.pivot_table(
        index="rolling_start_index",
        columns="stage",
        values=["trade_days", "trades", "calendar_days"],
        aggfunc="first",
    )
    if ("trade_days", "phase_1") not in pivot.columns or ("trade_days", "phase_2") not in pivot.columns:
        return pd.DataFrame(columns=["trade_days", "trades", "calendar_days"])
    both = pivot.dropna()
    return pd.DataFrame(
        {
            "trade_days": both[("trade_days", "phase_1")] + both[("trade_days", "phase_2")],
            "trades": both[("trades", "phase_1")] + both[("trades", "phase_2")],
            "calendar_days": both[("calendar_days", "phase_1")] + both[("calendar_days", "phase_2")],
        }
    )


def build_breakdown(runs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (system, account), group in runs.groupby(["system", "account"]):
        for outcome, outcome_group in group.groupby("outcome"):
            rows.append(
                {
                    "system": system,
                    "account": account,
                    "outcome": outcome,
                    "count": len(outcome_group),
                    "pct": pct(len(outcome_group), len(group)),
                }
            )
    return pd.DataFrame(rows).sort_values(["system", "account", "outcome"])


def build_recommendation(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in summary.iterrows():
        pass_rate = float(row["pass_rate"])
        avg_days = float(row["avg_total_trade_days"]) if row["avg_total_trade_days"] != "" else 999.0
        phase_fails = int(row["phase1_failed"]) + int(row["phase2_failed"])
        if pass_rate >= 85 and phase_fails <= 25 and avg_days <= 24:
            decision = "candidate"
        elif pass_rate >= 80 and avg_days <= 24:
            decision = "aggressive_candidate"
        elif pass_rate >= 85:
            decision = "stable_but_slow"
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
    return pd.DataFrame(rows).sort_values(["system", "account"])


def avg_pass_days(stages: pd.DataFrame) -> object:
    if stages.empty:
        return ""
    return avg_number(stages[stages["status"] == "passed"]["trade_days"])


def avg_number(values: pd.Series) -> object:
    clean = [float(value) for value in values if pd.notna(value)]
    return round(sum(clean) / len(clean), 2) if clean else ""


def median_number(values: pd.Series) -> object:
    clean = [float(value) for value in values if pd.notna(value)]
    return round(float(pd.Series(clean).median()), 2) if clean else ""


def pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100.0, 2) if denominator else 0.0


def write_report(recommendation: pd.DataFrame, summary: pd.DataFrame, breakdown: pd.DataFrame) -> None:
    lines = [
        "# Challenge Speed Sensitivity",
        "",
        "Scope: challenge-only speed test. Funded stage is intentionally excluded.",
        "",
        "Goal: reduce phase pass duration without materially increasing phase failures.",
        "",
        "## Recommendation",
        "",
        base_report.markdown_table(recommendation),
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
