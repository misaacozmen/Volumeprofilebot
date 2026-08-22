from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_challenge_speed_sensitivity as speed
import run_corrected_engine_3month_report as base_report
import run_main_candidate_filter_tests as filters
import run_manual_rule_v2_validation as manual_v2
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "challenge_nq_spx_addon_sensitivity"

FINAL_NQ_VARIANT = "first30_q60"

SYSTEM_VARIANTS = [
    {
        "system": "CHALLENGE_CORE_V2",
        "description": "Baseline manual-rule v2 challenge system",
        "nq": {},
        "spx": {},
    },
    {
        "system": "CORE_V2_SPX_LATEST_1045",
        "description": "SPX add-on: allow latest entry until 10:45",
        "nq": {},
        "spx": {"latest_entry_time": "10:45"},
    },
    {
        "system": "CORE_V2_SPX_LATEST_1100",
        "description": "SPX add-on: allow latest entry until 11:00",
        "nq": {},
        "spx": {"latest_entry_time": "11:00"},
    },
    {
        "system": "CORE_V2_NQ_WINDOW_1100",
        "description": "NQ add-on: extend trade window to 11:00",
        "nq": {"trade_window_end": "11:00"},
        "spx": {},
    },
    {
        "system": "CORE_V2_NQ_ALL_WEEKDAYS",
        "description": "NQ add-on: allow Monday-Friday",
        "nq": {"allowed_weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday"},
        "spx": {},
    },
    {
        "system": "CORE_V2_NQ_1100_SPX_1045",
        "description": "Combined add-on: NQ window 11:00 + SPX latest entry 10:45",
        "nq": {"trade_window_end": "11:00"},
        "spx": {"latest_entry_time": "10:45"},
    },
]


def main() -> None:
    reset_report_dir()
    loaded = filters.load_data()
    thresholds = filters.build_thresholds(loaded)
    threshold_lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}
    legs = filters.build_leg_specs()
    leg_lookup = {(leg.system, leg.leg_key): leg for leg in legs}

    trade_cache: dict[tuple[object, ...], pd.DataFrame] = {}
    system_trades = {}
    trade_count_rows = []
    for variant in SYSTEM_VARIANTS:
        trades = build_system_trades(variant, leg_lookup, loaded, threshold_lookup, trade_cache)
        system_trades[variant["system"]] = trades
        trade_count_rows.append(trade_count_row(variant, trades))

    runs, stages = simulate_all(system_trades)
    summary = speed.build_summary(runs, stages)
    breakdown = speed.build_breakdown(runs)
    deltas = build_deltas(summary)
    recommendation = build_recommendation(summary, deltas)
    trade_counts = pd.DataFrame(trade_count_rows)

    runs.to_csv(REPORT_DIR / "challenge_runs.csv", index=False)
    stages.to_csv(REPORT_DIR / "stage_details.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    breakdown.to_csv(REPORT_DIR / "outcome_breakdown.csv", index=False)
    recommendation.to_csv(REPORT_DIR / "recommendation.csv", index=False)
    deltas.to_csv(REPORT_DIR / "deltas.csv", index=False)
    trade_counts.to_csv(REPORT_DIR / "trade_counts.csv", index=False)
    write_report(recommendation, deltas, trade_counts, summary, breakdown)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_system_trades(
    variant: dict[str, object],
    leg_lookup: dict[tuple[str, str], object],
    loaded: dict[tuple[str, str], pd.DataFrame],
    threshold_lookup: dict[tuple[str, str], dict[str, object]],
    trade_cache: dict[tuple[object, ...], pd.DataFrame],
) -> pd.DataFrame:
    frames = []
    for leg_key in ["nq", "spx"]:
        leg = leg_lookup[("phase_selected", leg_key)]
        variant_name = FINAL_NQ_VARIANT if leg_key == "nq" else "baseline"
        spec = filters.VariantSpec(
            variant_name,
            variant_name,
            "engine",
            first30_quantile=manual_v2.variant_quantile(variant_name),
        )
        config = filters.build_variant_config(leg, spec, threshold_lookup)
        config = manual_v2.manual_v2_config(config)
        config = apply_overrides(config, variant[leg_key])
        key = config_cache_key(leg, config)
        if key not in trade_cache:
            frame = loaded[(leg.candidate.symbol, leg.candidate.timeframe)].copy()
            trade_cache[key] = trades_to_frame(run_backtest(frame, config).trades)
        trades = trade_cache[key].copy()
        if trades.empty:
            continue
        trades.insert(0, "leg_key", leg_key)
        trades.insert(0, "label", leg.label)
        trades.insert(0, "system", variant["system"])
        frames.append(trades)
    pair = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    pair["entry_time_dt"] = pd.to_datetime(pair["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    pair["group"] = variant["system"]
    capped = apply_pair_risk_rule(pair, -1.0)
    return capped.sort_values("entry_time_dt").reset_index(drop=True)


def apply_overrides(config: object, overrides: object) -> object:
    data = dict(overrides)
    return replace(config, **data) if data else config


def config_cache_key(leg: object, config: object) -> tuple[object, ...]:
    return (
        leg.candidate.symbol,
        leg.candidate.timeframe,
        config.reward_r,
        config.max_trades_per_day,
        config.allowed_weekdays,
        config.setup_type_filter,
        config.direction_filter,
        config.fvg_entry_mode,
        config.stop_model,
        config.session_liquidity_only,
        config.swing_liquidity_mode,
        config.strong_swing_min_touches,
        config.trade_window_start,
        config.trade_window_end,
        config.latest_entry_time,
        config.first30_range_filter,
        config.first30_range_max,
        config.opening_premarket_sweep_mode,
        config.opening_premarket_sweep_start,
        config.require_swing_near_value_area,
        config.cancel_pending_on_opposite_cisd,
        config.require_sweep_rejection_close,
        config.opening_premarket_direction_mode,
        config.active_trade_block_mode,
    )


def simulate_all(system_trades: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    run_rows = []
    stage_rows = []
    accounts = [account for account in speed.ACCOUNT_SCENARIOS if account.key in {"A1_RISK_1P00", "A1_RISK_1P25", "A2_RISK_1P00"}]
    for system_name, trades in system_trades.items():
        for account in accounts:
            for start_index in range(len(trades)):
                run, stages = speed.simulate_challenge(system_name, account, trades, start_index)
                run_rows.append(run)
                stage_rows.extend(stages)
    return pd.DataFrame(run_rows), pd.DataFrame(stage_rows)


def trade_count_row(variant: dict[str, object], trades: pd.DataFrame) -> dict[str, object]:
    by_label = trades.groupby("label").size().to_dict()
    return {
        "system": variant["system"],
        "description": variant["description"],
        "total_trades_after_daily_cap": len(trades),
        "nq_trades": int(by_label.get("NQ phase", 0)),
        "spx_trades": int(by_label.get("SPX phase", 0)),
        "first_trade_date": str(trades["entry_time_dt"].min().date()) if not trades.empty else "",
        "last_trade_date": str(trades["entry_time_dt"].max().date()) if not trades.empty else "",
    }


def build_recommendation(summary: pd.DataFrame, deltas: pd.DataFrame) -> pd.DataFrame:
    delta_lookup = {
        (row["system"], row["account"]): row
        for _, row in deltas.iterrows()
    }
    rows = []
    for _, row in summary.iterrows():
        pass_rate = float(row["pass_rate"])
        phase_fails = int(row["phase1_failed"]) + int(row["phase2_failed"])
        if row["system"] == "CHALLENGE_CORE_V2":
            decision = "baseline"
        else:
            delta = delta_lookup[(row["system"], row["account"])]
            phase_delta = int(delta["delta_phase_failures"])
            calendar_delta = float(delta["delta_avg_calendar_days"])
            trade_day_delta = float(delta["delta_avg_trade_days"])
            pass_delta = float(delta["delta_pass_rate"])
            if pass_delta >= -1.0 and phase_delta <= 2 and (calendar_delta < 0 or trade_day_delta < 0):
                decision = "candidate"
            elif pass_delta >= -4.0 and phase_delta <= 15 and (calendar_delta < 0 or trade_day_delta < 0):
                decision = "speed_watch"
            elif pass_delta >= 0 and phase_delta <= 0:
                decision = "quality_watch_not_speed"
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
    return pd.DataFrame(rows).sort_values(["account", "decision", "system"])


def build_deltas(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for account, group in summary.groupby("account"):
        baseline = group[group["system"] == "CHALLENGE_CORE_V2"]
        if baseline.empty:
            continue
        base = baseline.iloc[0]
        for _, row in group[group["system"] != "CHALLENGE_CORE_V2"].iterrows():
            rows.append(
                {
                    "system": row["system"],
                    "account": account,
                    "delta_pass_rate": round(float(row["pass_rate"]) - float(base["pass_rate"]), 2),
                    "delta_phase_failures": int(row["phase1_failed"] + row["phase2_failed"]) - int(base["phase1_failed"] + base["phase2_failed"]),
                    "delta_avg_trade_days": round(float(row["avg_total_trade_days"]) - float(base["avg_total_trade_days"]), 2),
                    "delta_median_trade_days": round(float(row["median_total_trade_days"]) - float(base["median_total_trade_days"]), 2),
                    "delta_avg_calendar_days": round(float(row["avg_total_calendar_days"]) - float(base["avg_total_calendar_days"]), 2),
                    "delta_median_calendar_days": round(float(row["median_total_calendar_days"]) - float(base["median_total_calendar_days"]), 2),
                }
            )
    return pd.DataFrame(rows).sort_values(["account", "delta_phase_failures", "delta_avg_calendar_days"])


def write_report(
    recommendation: pd.DataFrame,
    deltas: pd.DataFrame,
    trade_counts: pd.DataFrame,
    summary: pd.DataFrame,
    breakdown: pd.DataFrame,
) -> None:
    lines = [
        "# Challenge NQ/SPX Add-On Sensitivity",
        "",
        "Scope: challenge-only rolling phase test for CHALLENGE_CORE_V2 NQ/SPX add-ons.",
        "",
        "Baseline: manual-rule v2 CHALLENGE_CORE_V2, NQ first30_q60, SPX baseline, pair-level daily -1R cap.",
        "",
        "Tested add-ons:",
        "",
        "- SPX latest_entry_time 10:45",
        "- SPX latest_entry_time 11:00",
        "- NQ trade_window_end 11:00",
        "- NQ all weekdays",
        "- NQ trade_window_end 11:00 + SPX latest_entry_time 10:45",
        "",
        "Decision rule: keep A1/A2 pass rate and phase failures close to baseline while reducing trade-day/calendar duration.",
        "",
        "## Recommendation",
        "",
        base_report.markdown_table(recommendation),
        "",
        "## Delta Versus Baseline",
        "",
        base_report.markdown_table(deltas),
        "",
        "## Trade Counts After Daily Cap",
        "",
        base_report.markdown_table(trade_counts),
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
