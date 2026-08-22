from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_challenge_v2_to_funded_lifecycle as challenge_lifecycle
import run_corrected_engine_3month_report as base_report
import run_frequency_expansion_tests as frequency
import run_main_candidate_filter_tests as filters
import run_manual_rule_v2_validation as manual_v2
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "body_fvg_6month_validation"
SEED = 20260705
SELECTED_MONTH_COUNT = 6
SYSTEMS = ["phase_selected", "funded_selected"]


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    apply_to: str
    setup_type_filter: str | None = None
    entry_mode: str | None = None


VARIANTS = [
    Variant("baseline", "Current active leg configs", "none"),
    Variant("spx_body_fvg_midpoint", "SPX body-FVG midpoint only", "spx", "body_fvg", "midpoint"),
    Variant("spx_body_fvg_ote705", "SPX body-FVG OTE 70.5 only", "spx", "body_fvg", "ote_705"),
    Variant("both_body_fvg_midpoint", "NQ + SPX body-FVG midpoint", "both", "body_fvg", "midpoint"),
]


def main() -> None:
    started = time.perf_counter()
    reset_report_dir()
    base_report.SEED = SEED
    base_report.SELECTED_MONTH_COUNT = SELECTED_MONTH_COUNT
    months = base_report.choose_months()
    loaded = filters.load_data()
    thresholds = filters.build_thresholds(loaded)
    threshold_lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}
    legs = filters.build_leg_specs()
    leg_lookup = {(leg.system, leg.leg_key): leg for leg in legs}

    runs: dict[tuple[str, str, str], pd.DataFrame] = {}
    leg_rows: list[dict[str, object]] = []
    all_trades: list[pd.DataFrame] = []

    for system in SYSTEMS:
        for leg_key in ["nq", "spx"]:
            leg = leg_lookup[(system, leg_key)]
            source = loaded[(leg.candidate.symbol, leg.candidate.timeframe)].copy()
            frame = base_report.select_month_windows(source, months)
            frame["date"] = frame["time"].dt.date
            for variant in VARIANTS:
                config = build_config(system, leg_key, leg, variant, threshold_lookup)
                trades = trades_to_frame(run_backtest(frame.copy(), config).trades)
                if not trades.empty:
                    trades = trades[trades["date"].str[:7].isin(months)].copy()
                    trades.insert(0, "variant", variant.name)
                    trades.insert(0, "leg_key", leg_key)
                    trades.insert(0, "label", leg.label)
                    trades.insert(0, "system", system)
                    all_trades.append(trades)
                runs[(system, leg_key, variant.name)] = trades
                leg_rows.append(build_leg_row(system, leg_key, variant, trades))

    pair_rows = build_pair_rows(runs)
    leg_comparison = pd.DataFrame(leg_rows)
    pair_comparison = pd.DataFrame(pair_rows)
    trades_frame = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()

    leg_comparison.to_csv(REPORT_DIR / "leg_comparison.csv", index=False)
    pair_comparison.to_csv(REPORT_DIR / "pair_comparison.csv", index=False)
    trades_frame.to_csv(REPORT_DIR / "all_trades.csv", index=False)
    write_report(months, leg_comparison, pair_comparison, time.perf_counter() - started)
    print(f"Selected months: {', '.join(months)}")
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


def build_config(system: str, leg_key: str, leg, variant: Variant, threshold_lookup: dict[tuple[str, str], dict[str, object]]):
    variant_name = challenge_lifecycle.FINAL_NQ_VARIANTS[system] if leg_key == "nq" else "baseline"
    spec = filters.VariantSpec(
        variant_name,
        variant_name,
        "engine",
        first30_quantile=manual_v2.variant_quantile(variant_name),
    )
    config = filters.build_variant_config(leg, spec, threshold_lookup)
    if system == "phase_selected":
        config = manual_v2.manual_v2_config(config)
    if variant.apply_to in {leg_key, "both"} and variant.setup_type_filter is not None:
        config = replace(
            config,
            setup_type_filter=variant.setup_type_filter,
            fvg_entry_mode=variant.entry_mode or config.fvg_entry_mode,
        )
    return config


def build_leg_row(system: str, leg_key: str, variant: Variant, trades: pd.DataFrame) -> dict[str, object]:
    stats = frequency.stats(trades)
    months = frequency.active_month_count(trades)
    return {
        "system": system,
        "leg_key": leg_key,
        "variant": variant.name,
        "description": variant.description,
        **stats,
        "avg_trades_per_month": round(len(trades) / months, 2) if len(trades) else 0.0,
    }


def build_pair_rows(runs: dict[tuple[str, str, str], pd.DataFrame]) -> list[dict[str, object]]:
    rows = []
    for system in SYSTEMS:
        baseline = capped_pair(system, "baseline", runs)
        baseline_stats = frequency.stats(baseline)
        for variant in VARIANTS:
            pair = capped_pair(system, variant.name, runs)
            stats = frequency.stats(pair)
            daily = pair.groupby("date")["r_multiple"].sum() if not pair.empty else pd.Series(dtype=float)
            rows.append(
                {
                    "system": system,
                    "variant": variant.name,
                    "description": variant.description,
                    **stats,
                    "avg_trades_per_month": round(len(pair) / frequency.active_month_count(pair), 2) if len(pair) else 0.0,
                    "worst_day_r": round(float(daily.min()), 2) if len(daily) else 0.0,
                    "delta_trades": int(stats["trades"] - baseline_stats["trades"]),
                    "delta_net_r": round(float(stats["net_r"]) - float(baseline_stats["net_r"]), 2),
                    "delta_max_drawdown_r": round(float(stats["max_drawdown_r"]) - float(baseline_stats["max_drawdown_r"]), 2),
                }
            )
    return rows


def capped_pair(system: str, variant: str, runs: dict[tuple[str, str, str], pd.DataFrame]) -> pd.DataFrame:
    frames = [runs[(system, "nq", variant)], runs[(system, "spx", variant)]]
    non_empty = [frame for frame in frames if not frame.empty]
    if not non_empty:
        return pd.DataFrame()
    pair = pd.concat(non_empty, ignore_index=True)
    pair["entry_time_dt"] = pd.to_datetime(pair["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    pair["group"] = system
    return apply_pair_risk_rule(pair, -1.0).sort_values("entry_time_dt").reset_index(drop=True)


def write_report(months: list[str], leg_comparison: pd.DataFrame, pair_comparison: pd.DataFrame, runtime_seconds: float) -> None:
    lines = [
        "# Body-FVG 6-Month Validation",
        "",
        f"Random seed: `{SEED}`",
        f"Selected months: {', '.join(months)}",
        f"Runtime seconds: {runtime_seconds:.1f}",
        "",
        "Purpose: validate the manual-regression body-FVG improvement on the active NQ+SPX phase/funded legs before lifecycle tests.",
        "",
        "Pair-level daily cap: `-1R`.",
        "",
        "## Pair Comparison",
        "",
        base_report.markdown_table(pair_comparison),
        "",
        "## Leg Comparison",
        "",
        base_report.markdown_table(leg_comparison),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
