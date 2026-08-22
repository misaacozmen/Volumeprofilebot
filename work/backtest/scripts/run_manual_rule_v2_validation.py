from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_frequency_expansion_tests as frequency
import run_main_candidate_filter_tests as filters
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "manual_rule_v2_validation"

FINAL_NQ_VARIANTS = {
    "funded_selected": "first30_q70",
    "phase_selected": "first30_q60",
    "funded_selected_frequency": "first30_q70",
    "phase_selected_frequency": "first30_q70",
}


def main() -> None:
    started = time.perf_counter()
    reset_report_dir()
    loaded = filters.load_data()
    thresholds = filters.build_thresholds(loaded)
    threshold_lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}
    legs = filters.build_leg_specs()

    runs = {}
    for leg in legs:
        base_variant = FINAL_NQ_VARIANTS[leg.system] if leg.leg_key == "nq" else "baseline"
        base_config = filters.build_variant_config(leg, filters.VariantSpec(base_variant, base_variant, "engine", first30_quantile=variant_quantile(base_variant)), threshold_lookup)
        runs[(leg.system, leg.leg_key, "current")] = run_leg(leg, "current", base_config, loaded)
        runs[(leg.system, leg.leg_key, "manual_v2")] = run_leg(leg, "manual_v2", manual_v2_config(base_config), loaded)

    summary = build_pair_summary(runs)
    deltas = build_deltas(summary)
    all_trades = pd.concat([run["trades"] for run in runs.values() if not run["trades"].empty], ignore_index=True)

    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    deltas.to_csv(REPORT_DIR / "deltas.csv", index=False)
    all_trades.to_csv(REPORT_DIR / "all_leg_trades.csv", index=False)
    write_report(summary, deltas, time.perf_counter() - started)
    print(f"Wrote: {REPORT_DIR}")
    print(f"Runtime seconds: {time.perf_counter() - started:.1f}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def variant_quantile(variant: str) -> float | None:
    if variant == "first30_q60":
        return 0.60
    if variant == "first30_q70":
        return 0.70
    return None


def manual_v2_config(config):
    return replace(
        config,
        opening_premarket_sweep_mode="recent_as_setup",
        opening_premarket_sweep_start="09:15",
        require_swing_near_value_area=True,
        cancel_pending_on_opposite_cisd=True,
    )


def run_leg(leg, variant_name: str, config, loaded: dict[tuple[str, str], pd.DataFrame]) -> dict[str, object]:
    print(f"{leg.system} / {leg.leg_key} / {variant_name}")
    frame = loaded[(leg.candidate.symbol, leg.candidate.timeframe)].copy()
    trades = trades_to_frame(run_backtest(frame, config).trades)
    if not trades.empty:
        trades.insert(0, "manual_variant", variant_name)
        trades.insert(0, "leg_key", leg.leg_key)
        trades.insert(0, "label", leg.label)
        trades.insert(0, "system", leg.system)
    return {"leg": leg, "variant": variant_name, "trades": trades}


def build_pair_summary(runs: dict[tuple[str, str, str], dict[str, object]]) -> pd.DataFrame:
    rows = []
    for system in sorted({key[0] for key in runs}):
        for variant in ["current", "manual_v2"]:
            pair = pd.concat(
                [
                    runs[(system, "nq", variant)]["trades"],
                    runs[(system, "spx", variant)]["trades"],
                ],
                ignore_index=True,
            )
            capped = apply_daily_cap(pair)
            rows.append({"system": system, "variant": variant, **summary_stats(capped)})
    return pd.DataFrame(rows).sort_values(["system", "variant"])


def apply_daily_cap(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    capped = trades.copy()
    capped["entry_time_dt"] = pd.to_datetime(capped["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    capped["group"] = capped["system"]
    return apply_pair_risk_rule(capped, -1.0)


def summary_stats(trades: pd.DataFrame) -> dict[str, object]:
    stats = frequency.stats(trades)
    months = frequency.active_month_count(trades)
    daily = trades.groupby("date")["r_multiple"].sum() if not trades.empty else pd.Series(dtype=float)
    stats["avg_trades_per_month"] = round(len(trades) / months, 2) if len(trades) else 0.0
    stats["worst_day_r"] = round(float(daily.min()), 2) if len(daily) else 0.0
    return stats


def build_deltas(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for system, group in summary.groupby("system"):
        current = group[group["variant"] == "current"].iloc[0]
        v2 = group[group["variant"] == "manual_v2"].iloc[0]
        rows.append(
            {
                "system": system,
                "delta_trades": int(v2["trades"] - current["trades"]),
                "delta_avg_trades_per_month": round(float(v2["avg_trades_per_month"]) - float(current["avg_trades_per_month"]), 2),
                "delta_win_rate": round(float(v2["win_rate"]) - float(current["win_rate"]), 2),
                "delta_net_r": round(float(v2["net_r"]) - float(current["net_r"]), 2),
                "delta_max_drawdown_r": round(float(v2["max_drawdown_r"]) - float(current["max_drawdown_r"]), 2),
                "delta_profit_factor": round(float(v2["profit_factor"]) - float(current["profit_factor"]), 2),
            }
        )
    return pd.DataFrame(rows).sort_values("system")


def write_report(summary: pd.DataFrame, deltas: pd.DataFrame, runtime_seconds: float) -> None:
    lines = [
        "# Manual Rule V2 Validation",
        "",
        "Scope: compare current final candidates against first-pass manual-rule v2 gates.",
        "",
        "V2 rules enabled:",
        "",
        "- opening_premarket_sweep_mode=recent_as_setup",
        "- opening_premarket_sweep_start=09:15",
        "- require_swing_near_value_area=True",
        "- cancel_pending_on_opposite_cisd=True",
        "",
        f"Runtime: {runtime_seconds:.1f} seconds",
        "",
        "## Summary",
        "",
        base_report.markdown_table(summary),
        "",
        "## Deltas",
        "",
        base_report.markdown_table(deltas),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
