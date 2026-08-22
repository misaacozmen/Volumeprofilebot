from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report


SOURCE_FILE = ROOT / "outputs" / "reports" / "main_candidate_first30_validation" / "summary_enriched.csv"
REPORT_DIR = ROOT / "outputs" / "reports" / "final_candidate_set"

FINAL_SELECTIONS = [
    {
        "candidate": "funded_selected",
        "variant": "first30_q70",
        "nq_first30_filter": "q70",
        "description": "Funded core: NQ 3R first30 q70 + SPX 2R latest entry 10:30",
    },
    {
        "candidate": "phase_selected",
        "variant": "first30_q60",
        "nq_first30_filter": "q60",
        "description": "Phase core: NQ 3R first30 q60 + SPX 2.5R latest entry 10:30",
    },
    {
        "candidate": "funded_selected_frequency",
        "variant": "first30_q70",
        "nq_first30_filter": "q70",
        "description": "Funded frequency: NQ 3R first30 q70 + SPX 2R latest entry 10:45",
    },
    {
        "candidate": "phase_selected_frequency",
        "variant": "first30_q70",
        "nq_first30_filter": "q70",
        "description": "Phase frequency: NQ 3R first30 q70 + SPX 2.5R latest entry 10:45",
    },
]


def main() -> None:
    reset_report_dir()
    final, baseline_comparison = build_final_candidate_set()
    final.to_csv(REPORT_DIR / "final_candidates.csv", index=False)
    baseline_comparison.to_csv(REPORT_DIR / "baseline_comparison.csv", index=False)
    write_report(final, baseline_comparison)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_final_candidate_set() -> tuple[pd.DataFrame, pd.DataFrame]:
    source = pd.read_csv(SOURCE_FILE)
    final_rows = []
    comparison_rows = []

    for selection in FINAL_SELECTIONS:
        candidate = selection["candidate"]
        selected = lookup(source, candidate, selection["variant"])
        baseline = lookup(source, candidate, "baseline")

        final_rows.append(normalize_selected_row(selection, selected))
        comparison_rows.append(normalize_comparison_row(selection, selected, baseline))

    return pd.DataFrame(final_rows), pd.DataFrame(comparison_rows)


def lookup(source: pd.DataFrame, system: str, variant: str) -> pd.Series:
    matches = source[(source["system"] == system) & (source["variant"] == variant)]
    if matches.empty:
        raise ValueError(f"Missing first30 validation row: {system} / {variant}")
    return matches.iloc[0]


def normalize_selected_row(selection: dict[str, str], row: pd.Series) -> dict[str, object]:
    return {
        "candidate": selection["candidate"],
        "description": selection["description"],
        "source_variant": selection["variant"],
        "nq_first30_filter": selection["nq_first30_filter"],
        "trades": int(row["trades"]),
        "avg_trades_per_month": float(row["avg_trades_per_month"]),
        "wins": int(row["wins"]),
        "losses": int(row["losses"]),
        "win_rate": float(row["win_rate"]),
        "net_r": float(row["net_r"]),
        "max_drawdown_r": float(row["max_drawdown_r"]),
        "profit_factor": float(row["profit_factor"]),
        "net_r_per_dd": float(row["net_r_per_dd"]),
        "worst_day_r": float(row["worst_day_r"]),
        "worst_month_r": float(row["worst_month_r"]),
        "worst_year_r": float(row["worst_year_r"]),
        "max_consecutive_losing_trades": int(row["max_consecutive_losing_trades"]),
        "max_consecutive_losing_days": int(row["max_consecutive_losing_days"]),
        "daily_loss_cap": "-1R",
    }


def normalize_comparison_row(selection: dict[str, str], selected: pd.Series, baseline: pd.Series) -> dict[str, object]:
    return {
        "candidate": selection["candidate"],
        "selected_variant": selection["variant"],
        "baseline_variant": "baseline",
        "trades": int(selected["trades"]),
        "baseline_trades": int(baseline["trades"]),
        "delta_trades": int(selected["trades"] - baseline["trades"]),
        "avg_trades_per_month": float(selected["avg_trades_per_month"]),
        "baseline_avg_trades_per_month": float(baseline["avg_trades_per_month"]),
        "delta_avg_trades_per_month": round(
            float(selected["avg_trades_per_month"] - baseline["avg_trades_per_month"]), 2
        ),
        "net_r": float(selected["net_r"]),
        "baseline_net_r": float(baseline["net_r"]),
        "delta_net_r": float(selected["net_r"] - baseline["net_r"]),
        "max_drawdown_r": float(selected["max_drawdown_r"]),
        "baseline_max_drawdown_r": float(baseline["max_drawdown_r"]),
        "delta_max_drawdown_r": float(selected["max_drawdown_r"] - baseline["max_drawdown_r"]),
        "profit_factor": float(selected["profit_factor"]),
        "baseline_profit_factor": float(baseline["profit_factor"]),
        "delta_profit_factor": round(float(selected["profit_factor"] - baseline["profit_factor"]), 2),
        "net_r_per_dd": float(selected["net_r_per_dd"]),
        "baseline_net_r_per_dd": float(baseline["net_r_per_dd"]),
        "delta_net_r_per_dd": round(float(selected["net_r_per_dd"] - baseline["net_r_per_dd"]), 2),
        "worst_month_r": float(selected["worst_month_r"]),
        "baseline_worst_month_r": float(baseline["worst_month_r"]),
        "delta_worst_month_r": float(selected["worst_month_r"] - baseline["worst_month_r"]),
    }


def write_report(final: pd.DataFrame, baseline_comparison: pd.DataFrame) -> None:
    lines = [
        "# Final Candidate Set",
        "",
        "These are the active candidates. Older variants remain historical unless explicitly revived.",
        "",
        "## Active candidates",
        "",
        base_report.markdown_table(final),
        "",
        "## Change versus previous baseline",
        "",
        base_report.markdown_table(baseline_comparison),
        "",
        "## Notes",
        "",
        "- Active final candidates now use the live-safe NQ first30_range filter chosen from the validation report.",
        "- funded_selected, funded_selected_frequency, and phase_selected_frequency use NQ first30_q70.",
        "- phase_selected uses NQ first30_q60 because it gives the cleaner quality/DD profile before forward testing.",
        "- SPX legs remain unchanged from the selected final systems: funded uses 2R, phase uses 2.5R.",
        "- Older baseline candidates remain historical and can be revived later if needed.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
