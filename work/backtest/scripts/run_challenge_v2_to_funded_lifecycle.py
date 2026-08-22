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
import run_main_candidate_filter_tests as filters
import run_manual_rule_v2_validation as manual_v2
import run_phase_to_funded_account_lifecycle as phase_to_funded
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "challenge_v2_to_funded_lifecycle"

CHALLENGE_SYSTEMS = {
    "CHALLENGE_CORE_V2": "phase_selected",
    "CHALLENGE_FAST_V2": "phase_selected_frequency",
}
FUNDED_SYSTEMS = {
    "FON_CORE": "funded_selected",
    "FON_FAST": "funded_selected_frequency",
}
FINAL_NQ_VARIANTS = {
    "phase_selected": "first30_q60",
    "phase_selected_frequency": "first30_q70",
    "funded_selected": "first30_q70",
    "funded_selected_frequency": "first30_q70",
}


def main() -> None:
    reset_report_dir()
    loaded = filters.load_data()
    thresholds = filters.build_thresholds(loaded)
    threshold_lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}
    legs = filters.build_leg_specs()
    leg_lookup = {(leg.system, leg.leg_key): leg for leg in legs}

    candidate_trades = {}
    for operating_name, system in CHALLENGE_SYSTEMS.items():
        candidate_trades[operating_name] = build_pair_trades(system, leg_lookup, loaded, threshold_lookup, use_manual_v2=True)
    for operating_name, system in FUNDED_SYSTEMS.items():
        candidate_trades[operating_name] = build_pair_trades(system, leg_lookup, loaded, threshold_lookup, use_manual_v2=False)

    runs, stages = simulate_all(candidate_trades)
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


def build_pair_trades(
    system: str,
    leg_lookup: dict[tuple[str, str], object],
    loaded: dict[tuple[str, str], pd.DataFrame],
    threshold_lookup: dict[tuple[str, str], dict[str, object]],
    use_manual_v2: bool,
) -> pd.DataFrame:
    frames = []
    for leg_key in ["nq", "spx"]:
        leg = leg_lookup[(system, leg_key)]
        variant_name = FINAL_NQ_VARIANTS[system] if leg_key == "nq" else "baseline"
        variant = filters.VariantSpec(
            variant_name,
            variant_name,
            "engine",
            first30_quantile=manual_v2.variant_quantile(variant_name),
        )
        config = filters.build_variant_config(leg, variant, threshold_lookup)
        if use_manual_v2:
            config = manual_v2.manual_v2_config(config)
        frame = loaded[(leg.candidate.symbol, leg.candidate.timeframe)].copy()
        trades = trades_to_frame(run_backtest(frame, config).trades)
        if trades.empty:
            continue
        trades.insert(0, "leg_key", leg_key)
        trades.insert(0, "label", leg.label)
        trades.insert(0, "system", system)
        frames.append(trades)
    pair = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if pair.empty:
        return pair
    pair["entry_time_dt"] = pd.to_datetime(pair["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    pair["group"] = system
    capped = apply_pair_risk_rule(pair, -1.0)
    return capped.sort_values("entry_time_dt").reset_index(drop=True)


def simulate_all(candidate_trades: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    run_rows = []
    stage_rows = []
    for challenge_name in CHALLENGE_SYSTEMS:
        phase_trades = candidate_trades[challenge_name]
        for funded_name in FUNDED_SYSTEMS:
            funded_trades = candidate_trades[funded_name]
            combo = f"{challenge_name} -> {funded_name}"
            for account in lifecycle.ACCOUNTS:
                for start_index in range(len(phase_trades)):
                    run, stages = phase_to_funded.simulate_combo(
                        combo=combo,
                        phase_candidate=challenge_name,
                        funded_candidate=funded_name,
                        account=account,
                        phase_trades=phase_trades,
                        funded_trades=funded_trades,
                        start_index=start_index,
                    )
                    run_rows.append(run)
                    stage_rows.extend(stages)
    return pd.DataFrame(run_rows), pd.DataFrame(stage_rows)


def build_summary(runs: pd.DataFrame, stages: pd.DataFrame) -> pd.DataFrame:
    return phase_to_funded.build_summary(runs, stages)


def build_account_fit(summary: pd.DataFrame) -> pd.DataFrame:
    return phase_to_funded.build_account_fit(summary)


def build_breakdown(runs: pd.DataFrame) -> pd.DataFrame:
    return phase_to_funded.build_breakdown(runs)


def build_initial_start(runs: pd.DataFrame) -> pd.DataFrame:
    return phase_to_funded.build_initial_start(runs)


def build_funded_failures(runs: pd.DataFrame) -> pd.DataFrame:
    return phase_to_funded.build_funded_failures(runs)


def write_report(
    account_fit: pd.DataFrame,
    summary: pd.DataFrame,
    breakdown: pd.DataFrame,
    initial_start: pd.DataFrame,
    funded_failures: pd.DataFrame,
) -> None:
    lines = [
        "# Challenge V2 To Funded Lifecycle",
        "",
        "Scope: challenge phases use manual-rule v2 challenge systems; funded stage switches to current funded systems.",
        "",
        "Combinations tested:",
        "",
        "- CHALLENGE_CORE_V2 -> FON_CORE",
        "- CHALLENGE_CORE_V2 -> FON_FAST",
        "- CHALLENGE_FAST_V2 -> FON_CORE",
        "- CHALLENGE_FAST_V2 -> FON_FAST",
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
