from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_final_candidate_pre_forward_validation as final_trades


REPORT_DIR = ROOT / "outputs" / "reports" / "final_candidate_fund_lifecycle"
INITIAL_BALANCE = 5000.0


@dataclass(frozen=True)
class AccountModel:
    key: str
    label: str
    targets_pct: tuple[float, ...]
    max_dd_pct: float
    risk_per_trade_pct: float
    drawdown_mode: str


ACCOUNTS = [
    AccountModel("A1", "A1 5k 7%+5% DD10", (7.0, 5.0), 10.0, 1.0, "static"),
    AccountModel("A2", "A2 5k 6%+5% DD8", (6.0, 5.0), 8.0, 1.0, "static"),
    AccountModel("A3", "A3 25k 6% trail4", (6.0,), 4.0, 0.5, "trailing"),
]


def main() -> None:
    reset_report_dir()
    trades = final_trades.load_final_trades()
    runs, stages = simulate_all(trades)
    summary = build_summary(runs, stages)
    account_fit = build_account_fit(summary)
    failure_breakdown = build_failure_breakdown(runs)
    initial_start = build_initial_start(runs)
    worst_funded_failures = build_worst_funded_failures(runs)

    runs.to_csv(REPORT_DIR / "lifecycle_runs.csv", index=False)
    stages.to_csv(REPORT_DIR / "stage_details.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    account_fit.to_csv(REPORT_DIR / "account_fit.csv", index=False)
    failure_breakdown.to_csv(REPORT_DIR / "failure_breakdown.csv", index=False)
    initial_start.to_csv(REPORT_DIR / "initial_start.csv", index=False)
    worst_funded_failures.to_csv(REPORT_DIR / "funded_failures.csv", index=False)
    write_report(summary, account_fit, failure_breakdown, initial_start, worst_funded_failures)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def simulate_all(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    run_rows = []
    stage_rows = []
    for candidate, candidate_trades in trades.groupby("final_candidate"):
        ordered = candidate_trades.sort_values("entry_time_dt").reset_index(drop=True)
        for account in ACCOUNTS:
            for start_index in range(len(ordered)):
                run, stages = simulate_lifecycle(candidate, account, ordered, start_index)
                run_rows.append(run)
                stage_rows.extend(stages)
    return pd.DataFrame(run_rows), pd.DataFrame(stage_rows)


def simulate_lifecycle(
    candidate: str,
    account: AccountModel,
    trades: pd.DataFrame,
    start_index: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    current_index = start_index
    start_date = trade_date(trades.iloc[start_index])
    stage_rows = []
    phase_passed = 0
    phase_1_status = "not_started"
    phase_2_status = "not_applicable" if len(account.targets_pct) == 1 else "not_started"
    outcome = "incomplete_before_funded"
    fail_stage = ""
    fail_reason = ""
    funded_trade_days = 0
    funded_trades = 0
    funded_calendar_days = 0
    funded_net_pct = 0.0
    funded_min_equity_pct = 0.0
    funded_end_date = ""

    for phase_number, target_pct in enumerate(account.targets_pct, start=1):
        result = simulate_target_stage(
            trades=trades,
            start_index=current_index,
            account=account,
            stage_name=f"phase_{phase_number}",
            target_pct=target_pct,
        )
        stage_rows.append(stage_row(candidate, account, start_index, result))
        if phase_number == 1:
            phase_1_status = result["status"]
        elif phase_number == 2:
            phase_2_status = result["status"]
        if result["status"] == "passed":
            phase_passed += 1
            current_index = int(result["next_index"])
            if current_index >= len(trades):
                outcome = "incomplete_before_funded"
                break
            continue
        if result["status"] == "failed":
            outcome = f"failed_{result['stage']}"
            fail_stage = str(result["stage"])
            fail_reason = str(result["reason"])
        else:
            outcome = "incomplete_before_funded"
        break
    else:
        funded = simulate_funded_stage(trades, current_index, account)
        stage_rows.append(stage_row(candidate, account, start_index, funded))
        funded_trade_days = int(funded["trade_days"])
        funded_trades = int(funded["trades"])
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
            "candidate": candidate,
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
            "funded_trades": funded_trades,
            "funded_calendar_days": funded_calendar_days,
            "funded_net_pct": round(funded_net_pct, 2),
            "funded_min_equity_pct": round(funded_min_equity_pct, 2),
            "funded_end_date": funded_end_date,
        },
        stage_rows,
    )


def simulate_target_stage(
    trades: pd.DataFrame,
    start_index: int,
    account: AccountModel,
    stage_name: str,
    target_pct: float,
) -> dict[str, object]:
    if start_index >= len(trades):
        return empty_stage(stage_name, "incomplete", "no trades left")
    equity_pct = 0.0
    high_water_pct = 0.0
    min_equity_pct = 0.0
    start_date = trade_date(trades.iloc[start_index])
    used_dates: set[str] = set()

    for index in range(start_index, len(trades)):
        row = trades.iloc[index]
        used_dates.add(trade_date(row))
        equity_pct += float(row["r_multiple"]) * account.risk_per_trade_pct
        high_water_pct = max(high_water_pct, equity_pct)
        min_equity_pct = min(min_equity_pct, equity_pct)
        if is_drawdown_failed(equity_pct, high_water_pct, account):
            return stage_result(
                stage_name,
                "failed",
                drawdown_reason(account),
                start_index,
                index,
                start_date,
                trade_date(row),
                used_dates,
                equity_pct,
                min_equity_pct,
                high_water_pct,
                next_index=index + 1,
            )
        if equity_pct >= target_pct:
            return stage_result(
                stage_name,
                "passed",
                "target reached",
                start_index,
                index,
                start_date,
                trade_date(row),
                used_dates,
                equity_pct,
                min_equity_pct,
                high_water_pct,
                next_index=index + 1,
            )

    return stage_result(
        stage_name,
        "incomplete",
        "data ended before target/fail",
        start_index,
        len(trades) - 1,
        start_date,
        trade_date(trades.iloc[-1]),
        used_dates,
        equity_pct,
        min_equity_pct,
        high_water_pct,
        next_index=len(trades),
    )


def simulate_funded_stage(trades: pd.DataFrame, start_index: int, account: AccountModel) -> dict[str, object]:
    if start_index >= len(trades):
        return empty_stage("funded", "alive_to_data_end", "no funded trades available")
    equity_pct = 0.0
    high_water_pct = 0.0
    min_equity_pct = 0.0
    start_date = trade_date(trades.iloc[start_index])
    used_dates: set[str] = set()

    for index in range(start_index, len(trades)):
        row = trades.iloc[index]
        used_dates.add(trade_date(row))
        equity_pct += float(row["r_multiple"]) * account.risk_per_trade_pct
        high_water_pct = max(high_water_pct, equity_pct)
        min_equity_pct = min(min_equity_pct, equity_pct)
        if is_drawdown_failed(equity_pct, high_water_pct, account):
            return stage_result(
                "funded",
                "failed",
                drawdown_reason(account),
                start_index,
                index,
                start_date,
                trade_date(row),
                used_dates,
                equity_pct,
                min_equity_pct,
                high_water_pct,
                next_index=index + 1,
            )

    return stage_result(
        "funded",
        "alive_to_data_end",
        "data ended before funded fail",
        start_index,
        len(trades) - 1,
        start_date,
        trade_date(trades.iloc[-1]),
        used_dates,
        equity_pct,
        min_equity_pct,
        high_water_pct,
        next_index=len(trades),
    )


def is_drawdown_failed(equity_pct: float, high_water_pct: float, account: AccountModel) -> bool:
    if account.drawdown_mode == "static":
        return equity_pct <= -account.max_dd_pct
    return high_water_pct - equity_pct >= account.max_dd_pct


def drawdown_reason(account: AccountModel) -> str:
    return "static max drawdown" if account.drawdown_mode == "static" else "trailing max drawdown"


def stage_result(
    stage: str,
    status: str,
    reason: str,
    start_index: int,
    end_index: int,
    start_date: str,
    end_date: str,
    used_dates: set[str],
    equity_pct: float,
    min_equity_pct: float,
    high_water_pct: float,
    next_index: int,
) -> dict[str, object]:
    return {
        "stage": stage,
        "status": status,
        "reason": reason,
        "start_index": start_index,
        "end_index": end_index,
        "next_index": next_index,
        "start_date": start_date,
        "end_date": end_date,
        "trade_days": len(used_dates),
        "calendar_days": calendar_days(start_date, end_date),
        "trades": max(0, end_index - start_index + 1),
        "net_pct": round(equity_pct, 2),
        "end_balance": round(INITIAL_BALANCE * (1.0 + equity_pct / 100.0), 2),
        "min_equity_pct": round(min_equity_pct, 2),
        "high_water_pct": round(high_water_pct, 2),
    }


def empty_stage(stage: str, status: str, reason: str) -> dict[str, object]:
    return {
        "stage": stage,
        "status": status,
        "reason": reason,
        "start_index": 0,
        "end_index": 0,
        "next_index": 0,
        "start_date": "",
        "end_date": "",
        "trade_days": 0,
        "calendar_days": 0,
        "trades": 0,
        "net_pct": 0.0,
        "end_balance": INITIAL_BALANCE,
        "min_equity_pct": 0.0,
        "high_water_pct": 0.0,
    }


def stage_row(candidate: str, account: AccountModel, start_index: int, result: dict[str, object]) -> dict[str, object]:
    return {
        "candidate": candidate,
        "account": account.label,
        "account_key": account.key,
        "rolling_start_index": start_index,
        **result,
    }


def build_summary(runs: pd.DataFrame, stages: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (candidate, account), group in runs.groupby(["candidate", "account"]):
        reached = group[group["reached_funded"]]
        funded_failed = group[group["funded_failed"]]
        alive = group[group["outcome"] == "reached_funded_alive_to_data_end"]
        stage_group = stages[(stages["candidate"] == candidate) & (stages["account"] == account)]
        phase_1 = stage_group[stage_group["stage"] == "phase_1"]
        phase_2 = stage_group[stage_group["stage"] == "phase_2"]
        funded_stage = stage_group[stage_group["stage"] == "funded"]
        rows.append(
            {
                "candidate": candidate,
                "account": account,
                "starts": len(group),
                "phase1_passed": int((group["phase_1_status"] == "passed").sum()),
                "phase1_failed": int((group["outcome"] == "failed_phase_1").sum()),
                "phase2_passed": int((group["phase_2_status"] == "passed").sum()) if not phase_2.empty else "",
                "phase2_failed": int((group["outcome"] == "failed_phase_2").sum()) if not phase_2.empty else "",
                "reached_funded": len(reached),
                "reached_funded_pct": pct(len(reached), len(group)),
                "funded_failed": len(funded_failed),
                "funded_alive_to_data_end": len(alive),
                "funded_fail_pct_of_reached": pct(len(funded_failed), len(reached)),
                "avg_phase1_trade_days_to_pass": avg_pass_days(phase_1),
                "avg_phase2_trade_days_to_pass": avg_pass_days(phase_2),
                "avg_funded_trade_days_until_fail_or_data_end": avg_number(funded_stage["trade_days"]),
                "median_funded_trade_days_until_fail_or_data_end": median_number(funded_stage["trade_days"]),
                "min_funded_trade_days_until_fail": min_number(funded_failed["funded_trade_days"]),
                "avg_funded_calendar_days_until_fail_or_data_end": avg_number(funded_stage["calendar_days"]),
            }
        )
    return pd.DataFrame(rows).sort_values(["candidate", "account"])


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
        if phase1_failed > 0:
            flags.append("phase1_failures")
        if phase2_failed > 0:
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
                "candidate": row["candidate"],
                "account": row["account"],
                "fit": fit,
                "flags": ",".join(flags) if flags else "none",
                "reached_funded_pct": reached,
                "funded_fail_pct_of_reached": funded_fail,
                "phase1_failed": phase1_failed,
                "phase2_failed": phase2_failed,
            }
        )
    return pd.DataFrame(rows).sort_values(["candidate", "account"])


def build_failure_breakdown(runs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (candidate, account), group in runs.groupby(["candidate", "account"]):
        for outcome, outcome_group in group.groupby("outcome"):
            rows.append(
                {
                    "candidate": candidate,
                    "account": account,
                    "outcome": outcome,
                    "count": len(outcome_group),
                    "pct": pct(len(outcome_group), len(group)),
                }
            )
    return pd.DataFrame(rows).sort_values(["candidate", "account", "outcome"])


def build_initial_start(runs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (candidate, account), group in runs.groupby(["candidate", "account"]):
        row = group.sort_values("start_index").iloc[0]
        rows.append(row.to_dict())
    return pd.DataFrame(rows).sort_values(["candidate", "account"])


def build_worst_funded_failures(runs: pd.DataFrame) -> pd.DataFrame:
    failures = runs[runs["funded_failed"]].copy()
    if failures.empty:
        return failures
    return failures.sort_values(["funded_trade_days", "funded_calendar_days"]).groupby(
        ["candidate", "account"], group_keys=False
    ).head(10)


def avg_pass_days(stages: pd.DataFrame) -> object:
    if stages.empty:
        return ""
    passed = stages[stages["status"] == "passed"]
    return avg_number(passed["trade_days"])


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


def trade_date(row: pd.Series) -> str:
    return str(row["entry_time_dt"].date())


def calendar_days(start_date: str, end_date: str) -> int:
    if not start_date or not end_date:
        return 0
    return int((pd.Timestamp(end_date) - pd.Timestamp(start_date)).days) + 1


def write_report(
    summary: pd.DataFrame,
    account_fit: pd.DataFrame,
    failure_breakdown: pd.DataFrame,
    initial_start: pd.DataFrame,
    worst_funded_failures: pd.DataFrame,
) -> None:
    lines = [
        "# Final Candidate Fund Account Lifecycle",
        "",
        "Scope: rolling-start lifecycle simulation for the 4 final combined NQ/SPX candidates, using trade lists after the existing daily -1R pair cap.",
        "",
        "Account models:",
        "",
        "- A1 5k 7%+5% DD10: phase 1 target 7%, phase 2 target 5%, static 10% max DD, 1% risk per R.",
        "- A2 5k 6%+5% DD8: phase 1 target 6%, phase 2 target 5%, static 8% max DD, 1% risk per R.",
        "- A3 25k 6% trail4: one phase target 6%, trailing 4% max DD, 0.5% risk per R.",
        "",
        "After the last challenge phase passes, the simulation enters funded stage with no profit target and continues until the same DD rule fails or data ends.",
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
        base_report.markdown_table(failure_breakdown),
        "",
        "## Initial Start Result",
        "",
        base_report.markdown_table(initial_start),
        "",
        "## Fastest Funded Failures",
        "",
        base_report.markdown_table(worst_funded_failures) if not worst_funded_failures.empty else "No funded-stage failures.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
