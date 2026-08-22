from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_challenge_v2_to_funded_lifecycle as challenge_v2
import run_corrected_engine_3month_report as base_report
import run_final_candidate_fund_account_lifecycle as lifecycle
import run_main_candidate_filter_tests as filters
import run_manual_rule_v2_validation as manual_v2
import run_phase_to_funded_account_lifecycle as phase_to_funded
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "a3_static_dd_sensitivity"

ACCOUNTS = [
    lifecycle.AccountModel("A3_TRAIL4", "A3 current 6% trail4 risk0.5", (6.0,), 4.0, 0.5, "trailing"),
    lifecycle.AccountModel("A3_STATIC4", "A3 alt 6% static4 risk0.5", (6.0,), 4.0, 0.5, "static"),
    lifecycle.AccountModel("A3_STATIC5", "A3 alt 6% static5 risk0.5", (6.0,), 5.0, 0.5, "static"),
    lifecycle.AccountModel("A3_STATIC6", "A3 alt 6% static6 risk0.5", (6.0,), 6.0, 0.5, "static"),
]


def main() -> None:
    reset_report_dir()
    challenge_trades, funded_trades = build_trade_sets()
    runs, stages = simulate_accounts(challenge_trades, funded_trades)
    summary = phase_to_funded.build_summary(runs, stages)
    account_fit = phase_to_funded.build_account_fit(summary)
    breakdown = phase_to_funded.build_breakdown(runs)
    initial_start = phase_to_funded.build_initial_start(runs)

    runs.to_csv(REPORT_DIR / "lifecycle_runs.csv", index=False)
    stages.to_csv(REPORT_DIR / "stage_details.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    account_fit.to_csv(REPORT_DIR / "account_fit.csv", index=False)
    breakdown.to_csv(REPORT_DIR / "outcome_breakdown.csv", index=False)
    initial_start.to_csv(REPORT_DIR / "initial_start.csv", index=False)
    write_report(account_fit, summary, breakdown, initial_start)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_trade_sets() -> tuple[pd.DataFrame, pd.DataFrame]:
    loaded = filters.load_data()
    thresholds = filters.build_thresholds(loaded)
    threshold_lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}
    legs = filters.build_leg_specs()
    leg_lookup = {(leg.system, leg.leg_key): leg for leg in legs}
    challenge = build_pair_trades("phase_selected", leg_lookup, loaded, threshold_lookup, use_manual_v2=True)
    funded = build_pair_trades("funded_selected", leg_lookup, loaded, threshold_lookup, use_manual_v2=False)
    return challenge, funded


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
        variant_name = challenge_v2.FINAL_NQ_VARIANTS[system] if leg_key == "nq" else "baseline"
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
    pair = pd.concat(frames, ignore_index=True)
    pair["entry_time_dt"] = pd.to_datetime(pair["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    pair["group"] = system
    return apply_pair_risk_rule(pair, -1.0).sort_values("entry_time_dt").reset_index(drop=True)


def simulate_accounts(challenge_trades: pd.DataFrame, funded_trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    run_rows = []
    stage_rows = []
    combo = "CHALLENGE_CORE_V2 -> FON_CORE"
    for account in ACCOUNTS:
        for start_index in range(len(challenge_trades)):
            run, stages = phase_to_funded.simulate_combo(
                combo=combo,
                phase_candidate="CHALLENGE_CORE_V2",
                funded_candidate="FON_CORE",
                account=account,
                phase_trades=challenge_trades,
                funded_trades=funded_trades,
                start_index=start_index,
            )
            run_rows.append(run)
            stage_rows.extend(stages)
    return pd.DataFrame(run_rows), pd.DataFrame(stage_rows)


def write_report(
    account_fit: pd.DataFrame,
    summary: pd.DataFrame,
    breakdown: pd.DataFrame,
    initial_start: pd.DataFrame,
) -> None:
    lines = [
        "# A3 Static DD Sensitivity",
        "",
        "Scope: same operating plan, CHALLENGE_CORE_V2 -> FON_CORE. Only A3 drawdown model changes.",
        "",
        "## Account Fit",
        "",
        base_report.markdown_table(account_fit),
        "",
        "## Summary",
        "",
        base_report.markdown_table(summary),
        "",
        "## Outcome Breakdown",
        "",
        base_report.markdown_table(breakdown),
        "",
        "## Initial Start",
        "",
        base_report.markdown_table(initial_start),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
