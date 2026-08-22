from __future__ import annotations

import statistics
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_final_candidate_fund_account_lifecycle as lifecycle
import run_final_candidate_pre_forward_validation as final_trades


REPORT_DIR = ROOT / "outputs" / "reports" / "phase_to_funded_lifecycle"

PHASE_CANDIDATES = ["phase_selected", "phase_selected_frequency"]
FUNDED_CANDIDATES = ["funded_selected", "funded_selected_frequency"]


def main() -> None:
    reset_report_dir()
    trades = final_trades.load_final_trades()
    by_candidate = {
        candidate: group.sort_values("entry_time_dt").reset_index(drop=True)
        for candidate, group in trades.groupby("final_candidate")
    }
    runs, stages = simulate_all(by_candidate)
    summary = build_summary(runs, stages)
    account_fit = build_account_fit(summary)
    breakdown = build_breakdown(runs)
    initial_start = build_initial_start(runs)
    funded_failures = build_funded_failures(runs)

    runs.to_csv(REPORT_DIR / "lifecycle_runs.csv", index=False)
    stages.to_csv(REPORT_DIR / "stage_details.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    account_fit.to_csv(REPORT_DIR / "account_fit.csv", index=False)
    breakdown.to_csv(REPORT_DIR / "outcome_breakdown.csv", index=False)
    initial_start.to_csv(REPORT_DIR / "initial_start.csv", index=False)
    funded_failures.to_csv(REPORT_DIR / "funded_failures.csv", index=False)
    write_report(account_fit, summary, breakdown, initial_start, funded_failures)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def simulate_all(by_candidate: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    run_rows = []
    stage_rows = []
    for phase_candidate in PHASE_CANDIDATES:
        phase_trades = by_candidate[phase_candidate]
        for funded_candidate in FUNDED_CANDIDATES:
            funded_trades = by_candidate[funded_candidate]
            combo = f"{phase_candidate} -> {funded_candidate}"
            for account in lifecycle.ACCOUNTS:
                for start_index in range(len(phase_trades)):
                    run, stages = simulate_combo(
                        combo=combo,
                        phase_candidate=phase_candidate,
                        funded_candidate=funded_candidate,
                        account=account,
                        phase_trades=phase_trades,
                        funded_trades=funded_trades,
                        start_index=start_index,
                    )
                    run_rows.append(run)
                    stage_rows.extend(stages)
    return pd.DataFrame(run_rows), pd.DataFrame(stage_rows)


def simulate_combo(
    combo: str,
    phase_candidate: str,
    funded_candidate: str,
    account: lifecycle.AccountModel,
    phase_trades: pd.DataFrame,
    funded_trades: pd.DataFrame,
    start_index: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    current_index = start_index
    start_date = lifecycle.trade_date(phase_trades.iloc[start_index])
    stages: list[dict[str, object]] = []
    phase_passed = 0
    phase_1_status = "not_started"
    phase_2_status = "not_applicable" if len(account.targets_pct) == 1 else "not_started"
    outcome = "incomplete_before_funded"
    fail_stage = ""
    fail_reason = ""
    funded_trade_days = 0
    funded_trades_count = 0
    funded_calendar_days = 0
    funded_net_pct = 0.0
    funded_min_equity_pct = 0.0
    funded_end_date = ""

    for phase_number, target_pct in enumerate(account.targets_pct, start=1):
        result = lifecycle.simulate_target_stage(
            trades=phase_trades,
            start_index=current_index,
            account=account,
            stage_name=f"phase_{phase_number}",
            target_pct=target_pct,
        )
        stages.append(stage_row(combo, phase_candidate, funded_candidate, account, start_index, result))
        if phase_number == 1:
            phase_1_status = result["status"]
        elif phase_number == 2:
            phase_2_status = result["status"]

        if result["status"] == "passed":
            phase_passed += 1
            current_index = int(result["next_index"])
            continue
        if result["status"] == "failed":
            outcome = f"failed_{result['stage']}"
            fail_stage = str(result["stage"])
            fail_reason = str(result["reason"])
        else:
            outcome = "incomplete_before_funded"
        break
    else:
        phase_end_time = phase_trades.iloc[current_index - 1]["entry_time_dt"] if current_index > 0 else phase_trades.iloc[start_index]["entry_time_dt"]
        funded_start_index = first_trade_after(funded_trades, phase_end_time)
        if funded_start_index is None:
            outcome = "reached_funded_alive_to_data_end"
            funded_end_date = str(phase_end_time.date())
        else:
            funded = lifecycle.simulate_funded_stage(funded_trades, funded_start_index, account)
            stages.append(stage_row(combo, phase_candidate, funded_candidate, account, start_index, funded))
            funded_trade_days = int(funded["trade_days"])
            funded_trades_count = int(funded["trades"])
            funded_calendar_days = int(funded["calendar_days"])
            funded_net_pct = float(funded["net_pct"])
            funded_min_equity_pct = float(funded["min_equity_pct"])
            funded_end_date = str(funded["end_date"])
            if funded["status"] == "failed":
                outcome = "reached_funded_then_failed"
                fail_stage = "funded"
                fail_reason = str(funded["reason"])
            else:
                outcome = "reached_funded_alive_to_data_end"

    return (
        {
            "combo": combo,
            "phase_candidate": phase_candidate,
            "funded_candidate": funded_candidate,
            "account": account.label,
            "account_key": account.key,
            "risk_per_trade_pct": account.risk_per_trade_pct,
            "drawdown_mode": account.drawdown_mode,
            "start_index": start_index,
            "start_date": start_date,
            "outcome": outcome,
            "phase_1_status": phase_1_status,
            "phase_2_status": phase_2_status,
            "phases_passed": phase_passed,
            "reached_funded": outcome.startswith("reached_funded"),
            "funded_failed": outcome == "reached_funded_then_failed",
            "fail_stage": fail_stage,
            "fail_reason": fail_reason,
            "funded_trade_days": funded_trade_days,
            "funded_trades": funded_trades_count,
            "funded_calendar_days": funded_calendar_days,
            "funded_net_pct": round(funded_net_pct, 2),
            "funded_min_equity_pct": round(funded_min_equity_pct, 2),
            "funded_end_date": funded_end_date,
        },
        stages,
    )


def first_trade_after(trades: pd.DataFrame, timestamp: pd.Timestamp) -> int | None:
    matches = trades.index[trades["entry_time_dt"] > timestamp]
    if len(matches) == 0:
        return None
    return int(matches[0])


def stage_row(
    combo: str,
    phase_candidate: str,
    funded_candidate: str,
    account: lifecycle.AccountModel,
    start_index: int,
    result: dict[str, object],
) -> dict[str, object]:
    return {
        "combo": combo,
        "phase_candidate": phase_candidate,
        "funded_candidate": funded_candidate,
        "account": account.label,
        "account_key": account.key,
        "rolling_start_index": start_index,
        **result,
    }


def build_summary(runs: pd.DataFrame, stages: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (combo, account), group in runs.groupby(["combo", "account"]):
        reached = group[group["reached_funded"]]
        failed = group[group["funded_failed"]]
        alive = group[group["outcome"] == "reached_funded_alive_to_data_end"]
        stage_group = stages[(stages["combo"] == combo) & (stages["account"] == account)]
        phase_1 = stage_group[stage_group["stage"] == "phase_1"]
        phase_2 = stage_group[stage_group["stage"] == "phase_2"]
        funded = stage_group[stage_group["stage"] == "funded"]
        first = group.iloc[0]
        rows.append(
            {
                "combo": combo,
                "phase_candidate": first["phase_candidate"],
                "funded_candidate": first["funded_candidate"],
                "account": account,
                "starts": len(group),
                "phase1_passed": int((group["phase_1_status"] == "passed").sum()),
                "phase1_failed": int((group["outcome"] == "failed_phase_1").sum()),
                "phase2_passed": int((group["phase_2_status"] == "passed").sum()) if not phase_2.empty else "",
                "phase2_failed": int((group["outcome"] == "failed_phase_2").sum()) if not phase_2.empty else "",
                "reached_funded": len(reached),
                "reached_funded_pct": pct(len(reached), len(group)),
                "funded_failed": len(failed),
                "funded_alive_to_data_end": len(alive),
                "funded_fail_pct_of_reached": pct(len(failed), len(reached)),
                "avg_phase1_trade_days_to_pass": avg_pass_days(phase_1),
                "avg_phase2_trade_days_to_pass": avg_pass_days(phase_2),
                "avg_funded_trade_days_until_fail_or_data_end": avg_number(funded["trade_days"]),
                "median_funded_trade_days_until_fail_or_data_end": median_number(funded["trade_days"]),
                "min_funded_trade_days_until_fail": min_number(failed["funded_trade_days"]),
            }
        )
    return pd.DataFrame(rows).sort_values(["combo", "account"])


def build_account_fit(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in summary.iterrows():
        reached = float(row["reached_funded_pct"])
        funded_fail = float(row["funded_fail_pct_of_reached"])
        phase1_failed = int(row["phase1_failed"])
        phase2_failed = int(row["phase2_failed"]) if row["phase2_failed"] != "" else 0
        flags = []
        if reached < 70:
            flags.append("low_funded_reach_rate")
        if phase1_failed:
            flags.append("phase1_failures")
        if phase2_failed:
            flags.append("phase2_failures")
        if funded_fail > 25:
            flags.append("high_funded_failure")
        elif funded_fail > 5:
            flags.append("funded_failure_watch")

        if funded_fail == 0 and reached >= 70 and phase1_failed == 0 and phase2_failed == 0:
            fit = "clean"
        elif funded_fail <= 5 and reached >= 70:
            fit = "usable_watch"
        elif funded_fail <= 25 and reached >= 70:
            fit = "aggressive_watch"
        else:
            fit = "not_preferred"

        rows.append(
            {
                "combo": row["combo"],
                "account": row["account"],
                "fit": fit,
                "flags": ",".join(flags) if flags else "none",
                "reached_funded_pct": reached,
                "funded_fail_pct_of_reached": funded_fail,
                "phase1_failed": phase1_failed,
                "phase2_failed": phase2_failed,
            }
        )
    return pd.DataFrame(rows).sort_values(["combo", "account"])


def build_breakdown(runs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (combo, account), group in runs.groupby(["combo", "account"]):
        for outcome, outcome_group in group.groupby("outcome"):
            rows.append(
                {
                    "combo": combo,
                    "account": account,
                    "outcome": outcome,
                    "count": len(outcome_group),
                    "pct": pct(len(outcome_group), len(group)),
                }
            )
    return pd.DataFrame(rows).sort_values(["combo", "account", "outcome"])


def build_initial_start(runs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (combo, account), group in runs.groupby(["combo", "account"]):
        rows.append(group.sort_values("start_index").iloc[0].to_dict())
    return pd.DataFrame(rows).sort_values(["combo", "account"])


def build_funded_failures(runs: pd.DataFrame) -> pd.DataFrame:
    failures = runs[runs["funded_failed"]].copy()
    if failures.empty:
        return failures
    return failures.sort_values(["funded_trade_days", "funded_calendar_days"]).groupby(
        ["combo", "account"], group_keys=False
    ).head(10)


def avg_pass_days(stages: pd.DataFrame) -> object:
    if stages.empty:
        return ""
    return avg_number(stages[stages["status"] == "passed"]["trade_days"])


def avg_number(values: pd.Series) -> object:
    clean = [float(value) for value in values if pd.notna(value)]
    return round(sum(clean) / len(clean), 2) if clean else ""


def median_number(values: pd.Series) -> object:
    clean = [float(value) for value in values if pd.notna(value)]
    return round(float(statistics.median(clean)), 2) if clean else ""


def min_number(values: pd.Series) -> object:
    clean = [float(value) for value in values if pd.notna(value)]
    return round(min(clean), 2) if clean else ""


def pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100.0, 2) if denominator else 0.0


def write_report(
    account_fit: pd.DataFrame,
    summary: pd.DataFrame,
    breakdown: pd.DataFrame,
    initial_start: pd.DataFrame,
    funded_failures: pd.DataFrame,
) -> None:
    lines = [
        "# Phase-To-Funded Account Lifecycle",
        "",
        "Scope: challenge phases use phase candidates; after the final challenge phase passes, funded stage switches to funded candidates.",
        "",
        "Combinations tested:",
        "",
        "- phase_selected -> funded_selected",
        "- phase_selected -> funded_selected_frequency",
        "- phase_selected_frequency -> funded_selected",
        "- phase_selected_frequency -> funded_selected_frequency",
        "",
        "Account models match the prior lifecycle report: A1/A2 are two-stage static-DD accounts; A3 is one-stage trailing-DD account.",
        "",
        "## Account Fit",
        "",
        base_report.markdown_table(account_fit),
        "",
        "## Lifecycle Summary",
        "",
        base_report.markdown_table(summary),
        "",
        "## Outcome Breakdown",
        "",
        base_report.markdown_table(breakdown),
        "",
        "## Initial Start Result",
        "",
        base_report.markdown_table(initial_start),
        "",
        "## Fastest Funded Failures",
        "",
        base_report.markdown_table(funded_failures) if not funded_failures.empty else "No funded-stage failures.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
