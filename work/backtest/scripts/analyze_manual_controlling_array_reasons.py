from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REASONS_FILE = ROOT / "calibration_examples" / "manual_controlling_array_reasons_v1.csv"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_controlling_array_reason_analysis"

MACHINE_MEASURABILITY = {
    "array_type": "available",
    "htf": "available",
    "direction": "available",
    "array_relation": "available once proposed trade direction exists",
    "opening_reaction": "partially measurable from first touch and reaction displacement",
    "va_alignment": "measurable from zone distance to VAH/VAL",
    "body_state_role": "available from HTF state timeline",
    "structure_alignment": "partially measurable; semantic premarket/LTF structure link missing",
    "liquidity_context": "partially measurable; optional-versus-required meaning missing",
    "displacement_context": "partially measurable from post-touch candles",
    "conversion_context": "partially measurable from state transition",
    "competing_array_ignored": "available as multiple-candidate condition",
    "ignored_reason": "manual-only until a semantic relevance model is defined",
}


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    reasons = pd.read_csv(REASONS_FILE, keep_default_na=False)
    category_summary = build_category_summary(reasons)
    decision_summary = count_values(reasons, "decision_role")
    relation_summary = count_values(reasons, "array_relation")
    measurability = pd.DataFrame([
        {"field": field, "machine_status": status}
        for field, status in MACHINE_MEASURABILITY.items()
    ])
    unknowns = build_unknown_summary(reasons)

    reasons.to_csv(REPORT_DIR / "reason_cases.csv", index=False)
    category_summary.to_csv(REPORT_DIR / "category_summary.csv", index=False)
    decision_summary.to_csv(REPORT_DIR / "decision_role_summary.csv", index=False)
    relation_summary.to_csv(REPORT_DIR / "array_relation_summary.csv", index=False)
    measurability.to_csv(REPORT_DIR / "machine_measurability.csv", index=False)
    unknowns.to_csv(REPORT_DIR / "unknown_summary.csv", index=False)
    write_report(category_summary, decision_summary, relation_summary, measurability, unknowns)
    print(f"Wrote: {REPORT_DIR}")


def build_category_summary(frame: pd.DataFrame) -> pd.DataFrame:
    fields = [
        "opening_reaction",
        "va_alignment",
        "body_state_role",
        "structure_alignment",
        "liquidity_context",
        "conversion_context",
        "competing_array_ignored",
    ]
    rows: list[dict[str, object]] = []
    for field in fields:
        known = frame[~frame[field].isin(["", "unknown"])]
        rows.append({
            "field": field,
            "known_cases": len(known),
            "unknown_cases": len(frame) - len(known),
            "coverage_pct": round(len(known) / len(frame) * 100, 2),
            "distinct_known_values": known[field].nunique(),
        })
    return pd.DataFrame(rows)


def count_values(frame: pd.DataFrame, field: str) -> pd.DataFrame:
    result = frame.groupby(field, dropna=False).size().reset_index(name="cases")
    result.insert(0, "field", field)
    return result.sort_values("cases", ascending=False)


def build_unknown_summary(frame: pd.DataFrame) -> pd.DataFrame:
    fields = ["competing_array_ignored", "ignored_reason", "conversion_context"]
    rows: list[dict[str, object]] = []
    for field in fields:
        missing = frame[frame[field].isin(["", "unknown"])]
        rows.append({
            "field": field,
            "unknown_cases": len(missing),
            "case_ids": ",".join(missing["case_id"]),
        })
    return pd.DataFrame(rows)


def markdown_table(frame: pd.DataFrame) -> str:
    columns = list(frame.columns)
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(str(row[column]).replace("|", "/") for column in columns) + " |")
    return "\n".join(lines)


def write_report(*tables: pd.DataFrame) -> None:
    category, decision, relation, measurability, unknowns = tables
    lines = [
        "# Manual Controlling-Array Reason Analysis",
        "",
        "Only reasons explicitly supported by the existing manual comments/screenshots are labelled. Missing reasons remain unknown.",
        "",
        "## Coverage",
        "",
        markdown_table(category),
        "",
        "## Decision roles",
        "",
        markdown_table(decision),
        "",
        "## Array relation",
        "",
        markdown_table(relation),
        "",
        "## Machine measurability",
        "",
        markdown_table(measurability),
        "",
        "## Unknowns",
        "",
        markdown_table(unknowns),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
