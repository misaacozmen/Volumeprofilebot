from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "outputs" / "reports" / "fund_account_playbook"


NAME_MAP = [
    {
        "old_name": "phase_selected",
        "new_name": "CHALLENGE_CORE",
        "role": "Challenge/phase passing",
        "profile": "Primary challenge system",
        "notes": "Use during phase 1/phase 2. Best default challenge candidate from the lifecycle reports.",
    },
    {
        "old_name": "phase_selected_frequency",
        "new_name": "CHALLENGE_FAST",
        "role": "Challenge/phase passing",
        "profile": "Higher-frequency challenge system",
        "notes": "Usable on A1/A2, but not default because phase failures increase.",
    },
    {
        "old_name": "funded_selected",
        "new_name": "FON_CORE",
        "role": "Funded account trading",
        "profile": "Primary funded system",
        "notes": "Best funded-stage stability. Clean in the standalone lifecycle report across A1/A2/A3.",
    },
    {
        "old_name": "funded_selected_frequency",
        "new_name": "FON_FAST",
        "role": "Funded account trading",
        "profile": "Higher-frequency funded system",
        "notes": "Good on A1/A2 when switched to after challenge. Avoid on A3 trailing 4%.",
    },
]


RECOMMENDATIONS = [
    {
        "account": "A1 5k 7%+5% DD10",
        "primary_plan": "CHALLENGE_CORE_V2 -> FON_CORE",
        "optional_plan": "CHALLENGE_CORE_V2 -> FON_FAST",
        "risk_level": "preferred",
        "reason": "Core v2 plan reached funded 92.41% of rolling starts with 0% phase failures and 0% funded-stage failure. FON_CORE is the conservative default; FON_FAST adds funded frequency.",
    },
    {
        "account": "A2 5k 6%+5% DD8",
        "primary_plan": "CHALLENGE_CORE_V2 -> FON_CORE",
        "optional_plan": "CHALLENGE_CORE_V2 -> FON_FAST",
        "risk_level": "preferred_watch",
        "reason": "Core v2 plan reached funded 85.48% of rolling starts with 0% funded-stage failure. A2 still has phase failures, so keep FON_CORE as default.",
    },
    {
        "account": "A3 25k 6% trail4",
        "primary_plan": "CHALLENGE_CORE_V2 -> FON_CORE",
        "optional_plan": "No FON_FAST",
        "risk_level": "usable_watch",
        "reason": "Core v2 plan reached funded 73.60% of rolling starts with 0% funded-stage failure on FON_CORE. Avoid FON_FAST because trailing-DD funded failures remain high.",
    },
]


COMBO_DECISIONS = [
    {
        "combo": "CHALLENGE_CORE_V2 -> FON_CORE",
        "account_fit": "A1 preferred, A2 preferred-watch, A3 usable-watch",
        "decision": "Main operating plan.",
    },
    {
        "combo": "CHALLENGE_CORE_V2 -> FON_FAST",
        "account_fit": "A1 preferred, A2 preferred-watch, A3 not preferred",
        "decision": "Use after funded only when higher frequency is worth the added monitoring.",
    },
    {
        "combo": "CHALLENGE_FAST_V2 -> FON_CORE",
        "account_fit": "A1 usable, A2 usable-watch, A3 not preferred",
        "decision": "Secondary challenge option, not the default.",
    },
    {
        "combo": "CHALLENGE_FAST_V2 -> FON_FAST",
        "account_fit": "A1 usable, A2 aggressive-watch, A3 not preferred",
        "decision": "Do not use as default; too aggressive for A2/A3.",
    },
]


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    name_map = pd.DataFrame(NAME_MAP)
    recommendations = pd.DataFrame(RECOMMENDATIONS)
    combo_decisions = pd.DataFrame(COMBO_DECISIONS)

    name_map.to_csv(REPORT_DIR / "candidate_name_map.csv", index=False)
    recommendations.to_csv(REPORT_DIR / "account_recommendations.csv", index=False)
    combo_decisions.to_csv(REPORT_DIR / "combo_decisions.csv", index=False)
    write_report(name_map, recommendations, combo_decisions)
    print(f"Wrote: {REPORT_DIR}")


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return ""
    headers = list(frame.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(str(row[column]) for column in headers) + " |")
    return "\n".join(lines)


def write_report(name_map: pd.DataFrame, recommendations: pd.DataFrame, combo_decisions: pd.DataFrame) -> None:
    lines = [
        "# Fund Account Playbook",
        "",
        "This file is the operating name map and account-selection guide for the final candidate set.",
        "",
        "## Candidate Names",
        "",
        markdown_table(name_map),
        "",
        "## Account Recommendations",
        "",
        markdown_table(recommendations),
        "",
        "## Combo Decisions",
        "",
        markdown_table(combo_decisions),
        "",
        "## Operating Rule",
        "",
        "- Use CHALLENGE_* only while passing challenge phases.",
        "- After the final challenge phase is passed, switch to FON_*.",
        "- Default path: CHALLENGE_CORE_V2 -> FON_CORE.",
        "- Optional A1/A2 funded-frequency path: CHALLENGE_CORE_V2 -> FON_FAST.",
        "- For A3, use only CHALLENGE_CORE_V2 -> FON_CORE and avoid FON_FAST.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
