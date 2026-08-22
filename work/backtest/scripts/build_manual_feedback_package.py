from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "outputs" / "reports" / "body_fvg_6month_validation"
REGRESSION_DIR = ROOT / "outputs" / "reports" / "manual_regression_harness"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_feedback_package_v1"


REVIEW_COLUMNS = [
    "case_id",
    "review_focus",
    "symbol",
    "timeframe",
    "date",
    "direction",
    "vah",
    "val",
    "liquidity_context",
    "sweep_time",
    "cisd_time",
    "pd_array_time",
    "entry_time",
    "entry_price",
    "stop_price",
    "target_price",
    "setup_notes",
    "manual_decision",
    "manual_direction",
    "manual_liquidity_ok",
    "manual_cisd_ok",
    "manual_pd_array_ok",
    "manual_entry_ok",
    "manual_context_notes",
    "manual_reason",
]


SIMPLE_COLUMNS = [
    "case_id",
    "review_focus",
    "date",
    "symbol",
    "timeframe",
    "engine_direction",
    "vah",
    "val",
    "engine_liquidity",
    "sweep_time_ny",
    "cisd_time_ny",
    "pd_array_time_ny",
    "engine_entry_time_ny",
    "engine_entry_price",
    "engine_stop_price",
    "manual_decision",
    "manual_direction",
    "my_entry_idea",
    "my_pd_array",
    "why_take_or_skip",
    "invalidation_or_cancel",
    "manual_context_notes",
]


ANSWER_COLUMNS = [
    "case_id",
    "source_profile",
    "source_variant",
    "result",
    "r_multiple",
    "exit_time",
    "exit_price",
]


def main() -> None:
    reset_report_dir()
    trades = pd.read_csv(SOURCE_DIR / "all_trades.csv")
    regression = pd.read_csv(REGRESSION_DIR / "actual_trades.csv")

    cases = build_cases(trades, regression)
    review = cases[REVIEW_COLUMNS].copy()
    simple_review = build_simple_review(cases)
    answer = cases[ANSWER_COLUMNS].copy()

    review.to_csv(REPORT_DIR / "manual_review_cases_blind.csv", index=False)
    simple_review.to_csv(REPORT_DIR / "manual_review_cases_simple_tr.csv", index=False, sep=";", encoding="utf-8-sig")
    answer.to_csv(REPORT_DIR / "answer_key_do_not_review_first.csv", index=False)
    write_instructions(simple_review)
    print(f"Wrote: {REPORT_DIR}")
    print(f"Cases: {len(review)}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_cases(trades: pd.DataFrame, regression: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    body = trades[
        (trades["system"] == "phase_selected")
        & (trades["leg_key"] == "spx")
        & (trades["variant"] == "spx_body_fvg_midpoint")
    ].copy()
    body = body.sort_values(["date", "entry_time"]).reset_index(drop=True)
    for order, (_, row) in enumerate(body.iterrows(), start=1):
        rows.append(case_row(f"BFVG-{order:03d}", "body_fvg_quality", row, "body_fvg_6month", "spx_body_fvg_midpoint"))

    ote = trades[
        (trades["system"] == "phase_selected")
        & (trades["leg_key"] == "spx")
        & (trades["variant"] == "spx_body_fvg_ote705")
    ].copy()
    ote = ote.sort_values(["date", "entry_time"]).reset_index(drop=True)
    ote_sample = stratified_sample(ote, max_rows=12)
    for order, (_, row) in enumerate(ote_sample.iterrows(), start=1):
        rows.append(case_row(f"OTE-{order:03d}", "body_fvg_entry_model", row, "body_fvg_6month", "spx_body_fvg_ote705"))

    baseline = trades[
        (trades["system"] == "phase_selected")
        & (trades["leg_key"] == "spx")
        & (trades["variant"] == "baseline")
    ].copy()
    baseline = baseline.sort_values(["date", "entry_time"]).head(12).reset_index(drop=True)
    for order, (_, row) in enumerate(baseline.iterrows(), start=1):
        rows.append(case_row(f"CTRL-{order:03d}", "standard_fvg_control", row, "body_fvg_6month", "baseline"))

    followup = regression[
        regression["profile"] == "challenge_core_v3_spx_body_fvg_start_followup"
    ].copy()
    followup["entry_time_dt"] = pd.to_datetime(followup["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    followup["day_order"] = followup.groupby("date")["entry_time_dt"].rank(method="first").astype(int)
    followup = followup[(followup["day_order"] >= 2) | (followup["date"].isin(["2023-10-13", "2023-10-17"]))].copy()
    followup = followup.sort_values(["date", "entry_time"]).reset_index(drop=True)
    for order, (_, row) in enumerate(followup.iterrows(), start=1):
        rows.append(case_row(f"CONT-{order:03d}", "same_day_or_continuation", row, "manual_regression", row["profile"]))

    frame = pd.DataFrame(rows)
    frame = frame.drop_duplicates(
        subset=["symbol", "timeframe", "date", "direction", "sweep_time", "cisd_time", "pd_array_time", "entry_time", "entry_price"]
    )
    return frame.sort_values(["review_focus", "date", "entry_time"]).reset_index(drop=True)


def stratified_sample(frame: pd.DataFrame, max_rows: int) -> pd.DataFrame:
    if len(frame) <= max_rows:
        return frame
    winners = frame[frame["result"] == "win"].copy()
    losers = frame[frame["result"] != "win"].copy()
    half = max_rows // 2
    parts = [
        winners.sort_values(["date", "entry_time"]).head(half),
        losers.sort_values(["date", "entry_time"]).head(max_rows - half),
    ]
    return pd.concat(parts, ignore_index=True).sort_values(["date", "entry_time"]).reset_index(drop=True)


def case_row(case_id: str, focus: str, row: pd.Series, source_profile: str, source_variant: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "review_focus": focus,
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "date": row["date"],
        "direction": row["direction"],
        "vah": row["vah"],
        "val": row["val"],
        "liquidity_context": row["liquidity_context"],
        "sweep_time": row["sweep_time"],
        "cisd_time": row["cisd_time"],
        "pd_array_time": row["fvg_time"],
        "entry_time": row["entry_time"],
        "entry_price": row["entry_price"],
        "stop_price": row["stop_price"],
        "target_price": row["target_price"],
        "setup_notes": row["notes"],
        "manual_decision": "",
        "manual_direction": "",
        "manual_liquidity_ok": "",
        "manual_cisd_ok": "",
        "manual_pd_array_ok": "",
        "manual_entry_ok": "",
        "manual_context_notes": "",
        "manual_reason": "",
        "source_profile": source_profile,
        "source_variant": source_variant,
        "result": row.get("result", ""),
        "r_multiple": row.get("r_multiple", ""),
        "exit_time": row.get("exit_time", ""),
        "exit_price": row.get("exit_price", ""),
    }


def build_simple_review(cases: pd.DataFrame) -> pd.DataFrame:
    review = pd.DataFrame(
        {
            "case_id": cases["case_id"],
            "review_focus": cases["review_focus"],
            "date": cases["date"],
            "symbol": cases["symbol"],
            "timeframe": cases["timeframe"],
            "engine_direction": cases["direction"],
            "vah": cases["vah"],
            "val": cases["val"],
            "engine_liquidity": cases["liquidity_context"],
            "sweep_time_ny": cases["sweep_time"].map(format_ny_time),
            "cisd_time_ny": cases["cisd_time"].map(format_ny_time),
            "pd_array_time_ny": cases["pd_array_time"].map(format_ny_time),
            "engine_entry_time_ny": cases["entry_time"].map(format_ny_time),
            "engine_entry_price": cases["entry_price"],
            "engine_stop_price": cases["stop_price"],
            "manual_decision": "",
            "manual_direction": "",
            "my_entry_idea": "",
            "my_pd_array": "",
            "why_take_or_skip": "",
            "invalidation_or_cancel": "",
            "manual_context_notes": "",
        }
    )
    return review[SIMPLE_COLUMNS]


def format_ny_time(value: object) -> str:
    if not value:
        return ""
    timestamp = str(value)
    return timestamp[:16].replace("T", " ") + " NY"


def write_instructions(review: pd.DataFrame) -> None:
    focus_counts = review["review_focus"].value_counts().rename_axis("review_focus").reset_index(name="cases")
    lines = [
        "# Manual Feedback Package V1",
        "",
        "Excel review file:",
        "",
        "```text",
        "outputs/reports/manual_feedback_package_v1/manual_feedback_review_simple.xlsx",
        "```",
        "",
        "Turkish Excel CSV review file:",
        "",
        "```text",
        "outputs/reports/manual_feedback_package_v1/manual_review_cases_simple_tr.csv",
        "```",
        "",
        "Answer key, do not use before chart review:",
        "",
        "```text",
        "outputs/reports/manual_feedback_package_v1/answer_key_do_not_review_first.csv",
        "```",
        "",
        "## How To Review",
        "",
        "For each row, inspect the chart around the listed sweep/CISD/PD-array/entry times.",
        "Fill only the manual columns in the Excel file or `manual_review_cases_simple_tr.csv`.",
        "",
        "Use these values:",
        "",
        "- `manual_decision`: TAKE, SKIP, WATCH",
        "- `manual_direction`: long, short, blank if SKIP",
        "- `my_entry_idea`: own entry level or zone if you would take it",
        "- `my_pd_array`: body FVG, FVG, IFVG, OTE, none, or your own description",
        "- `why_take_or_skip`: your one or two sentence decision",
        "- `invalidation_or_cancel`: what would invalidate or cancel the setup",
        "- `manual_context_notes`: optional broader chart context",
        "",
        "Do not look at the answer key until the manual fields are filled.",
        "",
        "## Case Mix",
        "",
        markdown_table(focus_counts),
        "",
        "## What We Need To Learn",
        "",
        "1. Which body-FVG setups you would actually take.",
        "2. Which body-FVG setups are technically present but low quality.",
        "3. Whether compact body-FVGs are better because they are cleaner, or if that was just sample noise.",
        "4. Whether same-day second continuation needs a separate rule or should mostly be skipped.",
        "",
        "A useful first pass is about 40-55 examples. This package contains enough for the first quality-gate pass.",
        "If the labels split cleanly, we can build the next filter from this set. If not, we add a second package.",
        "",
    ]
    (REPORT_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "No rows."
    columns = list(frame.columns)
    rows = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for _, row in frame.iterrows():
        rows.append("| " + " | ".join(str(row[column]) for column in columns) + " |")
    return "\n".join(rows)


if __name__ == "__main__":
    main()
