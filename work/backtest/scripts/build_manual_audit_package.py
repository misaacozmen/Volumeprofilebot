from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_final_candidate_pre_forward_validation as final_trades


SOURCE_LIFECYCLE = ROOT / "outputs" / "reports" / "phase_to_funded_lifecycle" / "stage_details.csv"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_audit_package"

AUDIT_COLUMNS = [
    "audit_id",
    "audit_bucket",
    "audit_reason",
    "operating_name",
    "old_candidate_name",
    "label",
    "symbol",
    "timeframe",
    "date",
    "direction",
    "liquidity_context",
    "sweep_time",
    "cisd_time",
    "fvg_time",
    "entry_time",
    "exit_time",
    "result",
    "r_multiple",
    "entry_price",
    "stop_price",
    "target_price",
    "notes",
    "review_status",
    "manual_decision",
    "issue_type",
    "chart_notes",
]


NAME_MAP = {
    "phase_selected": "CHALLENGE_CORE",
    "funded_selected": "FON_CORE",
}


def main() -> None:
    reset_report_dir()
    trades = final_trades.load_final_trades()
    lifecycle = pd.read_csv(SOURCE_LIFECYCLE)
    samples = []
    samples.append(sample_challenge_failures(trades, lifecycle))
    samples.append(sample_fon_core_weak_periods(trades))
    audit = pd.concat(samples, ignore_index=True)
    audit = dedupe_and_format(audit)

    audit.to_csv(REPORT_DIR / "manual_audit_trades.csv", index=False)
    write_report(audit)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def sample_challenge_failures(trades: pd.DataFrame, lifecycle: pd.DataFrame) -> pd.DataFrame:
    challenge = trades[trades["final_candidate"] == "phase_selected"].sort_values("entry_time_dt").reset_index(drop=True)
    failures = lifecycle[
        (lifecycle["combo"] == "phase_selected -> funded_selected")
        & (lifecycle["status"] == "failed")
        & (lifecycle["stage"].isin(["phase_1", "phase_2"]))
        & (lifecycle["account"].str.startswith(("A1", "A2")))
    ].copy()

    frames = []
    selected_failures = failures.sort_values(["account", "stage", "start_index"]).groupby(["account", "stage"]).head(4)
    for _, failure in selected_failures.iterrows():
        start = int(failure["start_index"])
        end = int(failure["end_index"])
        window = challenge.iloc[max(start, end - 3) : end + 1].copy()
        window["audit_bucket"] = "challenge_failure_window"
        window["audit_reason"] = (
            f"{failure['account']} {failure['stage']} failed by {failure['reason']}; "
            f"showing last trades before fail ({failure['start_date']} -> {failure['end_date']})"
        )
        frames.append(window)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def sample_fon_core_weak_periods(trades: pd.DataFrame) -> pd.DataFrame:
    fon = trades[trades["final_candidate"] == "funded_selected"].copy()
    weak_2024 = fon[(fon["year"] == "2024") & (fon["r_multiple"].astype(float) < 0)].copy()
    weak_2024["audit_bucket"] = "fon_core_weak_2024"
    weak_2024["audit_reason"] = "FON_CORE weak 2024 funded-period loss sample."

    weak_2025_h2 = fon[
        (fon["entry_time_dt"] >= pd.Timestamp("2025-07-01", tz="America/New_York"))
        & (fon["entry_time_dt"] < pd.Timestamp("2026-01-01", tz="America/New_York"))
    ].copy()
    weak_2025_h2 = weak_2025_h2[weak_2025_h2["r_multiple"].astype(float) <= 0].copy()
    weak_2025_h2["audit_bucket"] = "fon_core_flat_2025_h2"
    weak_2025_h2["audit_reason"] = "FON_CORE flat/weak 2025-H2 funded-period trade sample."

    worst_months = []
    for month in ["2024-02", "2024-08", "2024-09", "2025-09", "2025-11", "2025-12"]:
        month_rows = fon[fon["month"] == month].copy()
        if month_rows.empty:
            continue
        month_rows["audit_bucket"] = "fon_core_worst_month_context"
        month_rows["audit_reason"] = f"FON_CORE context sample from weak month {month}."
        worst_months.append(month_rows)

    frames = [weak_2024.head(12), weak_2025_h2.head(10)]
    if worst_months:
        frames.append(pd.concat(worst_months, ignore_index=True).head(14))
    return pd.concat(frames, ignore_index=True)


def dedupe_and_format(audit: pd.DataFrame) -> pd.DataFrame:
    if audit.empty:
        return pd.DataFrame(columns=AUDIT_COLUMNS)
    audit = audit.copy()
    audit["operating_name"] = audit["final_candidate"].map(NAME_MAP).fillna(audit["final_candidate"])
    audit["old_candidate_name"] = audit["final_candidate"]
    audit = audit.drop_duplicates(subset=["old_candidate_name", "symbol", "entry_time", "direction"], keep="first")
    audit = audit.sort_values(["audit_bucket", "entry_time"]).reset_index(drop=True)
    audit.insert(0, "audit_id", [f"AUDIT-{index:03d}" for index in range(1, len(audit) + 1)])
    for column in ["review_status", "manual_decision", "issue_type", "chart_notes"]:
        audit[column] = ""
    for column in AUDIT_COLUMNS:
        if column not in audit.columns:
            audit[column] = ""
    return audit[AUDIT_COLUMNS]


def write_report(audit: pd.DataFrame) -> None:
    counts = audit.groupby(["audit_bucket", "operating_name"]).size().reset_index(name="trades")
    lines = [
        "# Manual Audit Package",
        "",
        "Purpose: final manual chart review before forward-test handoff.",
        "",
        "Primary operating plan under review:",
        "",
        "- Challenge: CHALLENGE_CORE",
        "- Funded: FON_CORE",
        "",
        "Review fields are intentionally blank in manual_audit_trades.csv:",
        "",
        "- review_status: PASS / FAIL / WATCH",
        "- manual_decision: TAKE / SKIP",
        "- issue_type: LEVEL / CISD / FVG / ENTRY / EXIT / CONTEXT / OTHER",
        "- chart_notes: free text",
        "",
        "## Sample Counts",
        "",
        base_report.markdown_table(counts),
        "",
        "## Audit Trades",
        "",
        base_report.markdown_table(audit),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
