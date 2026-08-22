from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from hashlib import md5
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_frequency_expansion_tests as frequency
import run_selected_candidates_full_data_report as selected
from backtest.data_loader import load_ohlcv
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame
from frozen_first30_thresholds import create_artifact, load_thresholds


REPORT_DIR = ROOT / "outputs" / "reports" / "main_candidate_filter_tests"
RAW_DIR = ROOT / "data" / "raw"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


@dataclass(frozen=True)
class LegSpec:
    system: str
    leg_key: str
    label: str
    candidate: selected.SelectedCandidate
    config: object


@dataclass(frozen=True)
class VariantSpec:
    variant: str
    description: str
    mode: str
    direction: str | None = None
    excluded_weekday: str | None = None
    first30_quantile: float | None = None


def main() -> None:
    started = time.perf_counter()
    loaded = load_data()
    if "--rebuild-threshold-artifact" in sys.argv:
        payload = create_artifact(loaded, threshold_source_paths())
        print(f"Wrote frozen threshold artifact: {payload['artifact_id']}")
        return
    reset_report_dir()
    thresholds = build_thresholds(loaded)
    legs = build_leg_specs()
    runs = run_leg_variants(legs, loaded, thresholds)
    pair_rows, monthly_rows = build_pair_impacts(legs, runs)

    leg_comparison = pd.DataFrame([leg_row(run) for run in runs.values()])
    pair_comparison = pd.DataFrame(pair_rows).sort_values(["system", "score", "net_r"], ascending=[True, False, False])
    monthly_detail = pd.DataFrame(monthly_rows)
    threshold_frame = pd.DataFrame(thresholds).sort_values(["symbol", "timeframe"])

    leg_comparison.to_csv(REPORT_DIR / "leg_comparison.csv", index=False)
    pair_comparison.to_csv(REPORT_DIR / "pair_comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    threshold_frame.to_csv(REPORT_DIR / "first30_thresholds.csv", index=False)
    write_report(pair_comparison, leg_comparison, threshold_frame, time.perf_counter() - started)
    print(f"Wrote: {REPORT_DIR}")
    print(f"Runtime seconds: {time.perf_counter() - started:.1f}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path == REPORT_DIR / "_leg_cache" or (REPORT_DIR / "_leg_cache") in path.parents:
                continue
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_leg_specs() -> list[LegSpec]:
    funded_nq = frequency.funded_nq()
    funded_spx = frequency.funded_spx()
    phase_nq = frequency.phase_nq()
    phase_spx = frequency.phase_spx()
    return [
        LegSpec("funded_selected", "nq", "NQ funded", funded_nq, frequency.funded_nq_config()),
        LegSpec("funded_selected", "spx", "SPX funded", funded_spx, replace(frequency.funded_spx_config(), latest_entry_time="10:30")),
        LegSpec("phase_selected", "nq", "NQ phase", phase_nq, frequency.phase_nq_config()),
        LegSpec("phase_selected", "spx", "SPX phase", phase_spx, replace(frequency.phase_spx_config(), latest_entry_time="10:30")),
        LegSpec("funded_selected_frequency", "nq", "NQ funded", funded_nq, frequency.funded_nq_config()),
        LegSpec("funded_selected_frequency", "spx", "SPX funded", funded_spx, replace(frequency.funded_spx_config(), latest_entry_time="10:45")),
        LegSpec("phase_selected_frequency", "nq", "NQ phase", phase_nq, frequency.phase_nq_config()),
        LegSpec("phase_selected_frequency", "spx", "SPX phase", phase_spx, replace(frequency.phase_spx_config(), latest_entry_time="10:45")),
    ]


def load_data() -> dict[tuple[str, str], pd.DataFrame]:
    data: dict[tuple[str, str], pd.DataFrame] = {}
    for candidate in selected.CANDIDATES:
        key = (candidate.symbol, candidate.timeframe)
        if key not in data:
            data[key] = load_ohlcv(
                base_report.filter_paths(RAW_DIR, candidate.symbol, candidate.timeframe)
            ).frame
    return data


def threshold_source_paths() -> dict[tuple[str, str], list[Path]]:
    return {
        key: base_report.filter_paths(RAW_DIR, *key)
        for key in {(candidate.symbol, candidate.timeframe) for candidate in selected.CANDIDATES}
    }


def build_thresholds(
    loaded: dict[tuple[str, str], pd.DataFrame] | None = None,
) -> list[dict[str, object]]:
    # `loaded` remains in the signature for old callers, but active thresholds only come
    # from the versioned, pre-holdout artifact. They are never recalculated from a run's
    # full dataset.
    del loaded
    return load_thresholds()


def run_leg_variants(
    legs: list[LegSpec],
    loaded: dict[tuple[str, str], pd.DataFrame],
    thresholds: list[dict[str, object]],
) -> dict[tuple[str, str, str], dict[str, object]]:
    cache: dict[tuple[object, ...], pd.DataFrame] = {}
    runs: dict[tuple[str, str, str], dict[str, object]] = {}
    threshold_lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}

    for leg in legs:
        for variant in variants_for_leg(leg):
            config = build_variant_config(leg, variant, threshold_lookup)
            cache_key = config_cache_key(leg, config)
            if cache_key not in cache:
                print(f"{leg.system} / {leg.leg_key} / {variant.variant}")
                frame = loaded[(leg.candidate.symbol, leg.candidate.timeframe)].copy()
                cache[cache_key] = cached_run(cache_key, frame, config)
            runs[(leg.system, leg.leg_key, variant.variant)] = {
                "leg": leg,
                "variant": variant,
                "trades": tag_leg_trades(cache[cache_key].copy(), leg, variant),
            }
    return runs


def cached_run(cache_key: tuple[object, ...], frame: pd.DataFrame, config: object) -> pd.DataFrame:
    cache_dir = REPORT_DIR / "_leg_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = md5(repr(cache_key).encode("utf-8")).hexdigest()
    path = cache_dir / f"{digest}.csv"
    if path.exists():
        return pd.read_csv(path)
    trades = trades_to_frame(run_backtest(frame, config).trades)
    trades.to_csv(path, index=False)
    return trades


def variants_for_leg(leg: LegSpec) -> list[VariantSpec]:
    variants = [
        VariantSpec("baseline", "No extra filter", "engine"),
        VariantSpec("long_only", "Long direction only", "engine", direction="long"),
        VariantSpec("short_only", "Short direction only", "engine", direction="short"),
        VariantSpec("first30_q60", "Tighter live-safe first30 range q60", "engine", first30_quantile=0.60),
        VariantSpec("first30_q70", "Tighter live-safe first30 range q70", "engine", first30_quantile=0.70),
    ]
    allowed = [weekday for weekday in WEEKDAYS if weekday in leg.config.allowed_weekdays.split(",")]
    for weekday in allowed:
        variants.append(VariantSpec(f"exclude_{weekday.lower()}", f"Remove {weekday} from this leg", "engine", excluded_weekday=weekday))
    return variants


def build_variant_config(
    leg: LegSpec,
    variant: VariantSpec,
    threshold_lookup: dict[tuple[str, str], dict[str, object]],
):
    frozen = threshold_lookup[(leg.candidate.symbol, leg.candidate.timeframe)]
    # Retire legacy candidate constants that were calibrated without an auditable
    # cutoff. Baseline and q60 both use the frozen pre-2025 q60; q70 is the only
    # alternative threshold tested here.
    config = replace(
        leg.config,
        first30_range_filter="live_safe_max",
        first30_range_max=frozen["first30_q60"],
    )
    if variant.direction is not None:
        config = replace(config, direction_filter=variant.direction)
    if variant.excluded_weekday is not None:
        weekdays = [part for part in config.allowed_weekdays.split(",") if part != variant.excluded_weekday]
        config = replace(config, allowed_weekdays=",".join(weekdays) if weekdays else "all")
    if variant.first30_quantile is not None:
        key = "first30_q60" if variant.first30_quantile == 0.60 else "first30_q70"
        threshold = frozen[key]
        config = replace(config, first30_range_filter="live_safe_max", first30_range_max=threshold)
    return config


def config_cache_key(leg: LegSpec, config: object) -> tuple[object, ...]:
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
        config.htf_body_close_requalification,
    )


def tag_leg_trades(trades: pd.DataFrame, leg: LegSpec, variant: VariantSpec) -> pd.DataFrame:
    if trades.empty:
        return trades
    tagged = trades.copy()
    tagged.insert(0, "variant", variant.variant)
    tagged.insert(0, "leg_key", leg.leg_key)
    tagged.insert(0, "label", leg.label)
    tagged.insert(0, "group", leg.system)
    tagged.insert(0, "system", leg.system)
    return tagged


def build_pair_impacts(
    legs: list[LegSpec],
    runs: dict[tuple[str, str, str], dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = []
    monthly_rows = []
    systems = sorted({leg.system for leg in legs})
    for system in systems:
        baseline_pair = pair_trades(runs, system, "nq", "baseline", "spx", "baseline")
        baseline_capped = apply_daily_cap(baseline_pair)
        rows.append(pair_row(system, "pair", "baseline", "No extra filter", baseline_capped, baseline_capped))
        monthly_rows.extend(monthly_stats_rows(system, "pair", "baseline", baseline_capped))
        for leg_key in ["nq", "spx"]:
            leg_variants = [key[2] for key in runs if key[0] == system and key[1] == leg_key and key[2] != "baseline"]
            for variant_name in sorted(leg_variants):
                variant = runs[(system, leg_key, variant_name)]["variant"]
                other_key = "spx" if leg_key == "nq" else "nq"
                pair = pair_trades(runs, system, leg_key, variant_name, other_key, "baseline")
                capped = apply_daily_cap(pair)
                rows.append(pair_row(system, leg_key, variant_name, variant.description, capped, baseline_capped))
                monthly_rows.extend(monthly_stats_rows(system, leg_key, variant_name, capped))
    return rows, monthly_rows


def pair_trades(
    runs: dict[tuple[str, str, str], dict[str, object]],
    system: str,
    leg_a: str,
    variant_a: str,
    leg_b: str,
    variant_b: str,
) -> pd.DataFrame:
    frames = [runs[(system, leg_a, variant_a)]["trades"], runs[(system, leg_b, variant_b)]["trades"]]
    non_empty = [frame for frame in frames if not frame.empty]
    return pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()


def apply_daily_cap(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    capped = trades.copy()
    capped["entry_time_dt"] = pd.to_datetime(capped["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    return apply_pair_risk_rule(capped, -1.0)


def pair_row(
    system: str,
    changed_leg: str,
    variant: str,
    description: str,
    trades: pd.DataFrame,
    baseline: pd.DataFrame,
) -> dict[str, object]:
    stats = frequency.stats(trades)
    base_stats = frequency.stats(baseline)
    daily = trades.groupby("date")["r_multiple"].sum() if not trades.empty else pd.Series(dtype=float)
    return {
        "system": system,
        "changed_leg": changed_leg,
        "variant": variant,
        "description": description,
        **stats,
        "avg_trades_per_month": round(len(trades) / frequency.active_month_count(trades), 2) if len(trades) else 0.0,
        "worst_day_r": round(float(daily.min()), 2) if len(daily) else 0.0,
        "delta_trades": int(stats["trades"] - base_stats["trades"]),
        "delta_net_r": round(float(stats["net_r"]) - float(base_stats["net_r"]), 2),
        "delta_max_drawdown_r": round(float(stats["max_drawdown_r"]) - float(base_stats["max_drawdown_r"]), 2),
        "score": score_pair(stats),
    }


def score_pair(stats: dict[str, object]) -> float:
    net_r = float(stats["net_r"])
    drawdown = abs(float(stats["max_drawdown_r"]))
    pf = float(stats["profit_factor"])
    trades = float(stats["trades"])
    return round(net_r * 2 + pf * 8 - drawdown * 3 + min(trades, 350) * 0.02, 2)


def leg_row(run: dict[str, object]) -> dict[str, object]:
    leg = run["leg"]
    variant = run["variant"]
    return {
        "system": leg.system,
        "leg_key": leg.leg_key,
        "label": leg.label,
        "variant": variant.variant,
        "description": variant.description,
        **frequency.stats(run["trades"]),
    }


def monthly_stats_rows(system: str, changed_leg: str, variant: str, trades: pd.DataFrame) -> list[dict[str, object]]:
    if trades.empty:
        return []
    frame = trades.copy()
    frame["entry_time_dt"] = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    frame["month"] = frame["entry_time_dt"].dt.strftime("%Y-%m")
    rows = []
    for month, group in frame.groupby("month"):
        rows.append(
            {
                "system": system,
                "changed_leg": changed_leg,
                "variant": variant,
                "month": month,
                "trades": len(group),
                "net_r": round(float(group["r_multiple"].sum()), 2),
                "win_rate": round(float((group["result"] == "win").mean() * 100), 2),
            }
        )
    return rows


def write_report(
    pair_comparison: pd.DataFrame,
    leg_comparison: pd.DataFrame,
    thresholds: pd.DataFrame,
    runtime_seconds: float,
) -> None:
    best = pair_comparison.groupby("system", group_keys=False).apply(
        lambda group: group.sort_values(["score", "net_r"], ascending=[False, False]).head(8)
    )
    improved = []
    for system, group in pair_comparison.groupby("system"):
        baseline = group[group["variant"] == "baseline"].iloc[0]
        better = group[
            (group["variant"] != "baseline")
            & (group["net_r"] >= baseline["net_r"])
            & (group["max_drawdown_r"] >= baseline["max_drawdown_r"])
            & (group["profit_factor"] >= baseline["profit_factor"])
        ]
        if not better.empty:
            improved.append(better.sort_values(["score", "net_r"], ascending=[False, False]))
    improved_frame = pd.concat(improved, ignore_index=True) if improved else pd.DataFrame()
    lines = [
        "# Main Candidate Filter Tests",
        "",
        "Scope: active NQ/SPX systems. One leg is changed at a time, the other leg remains baseline, then pair daily -1R cap is applied.",
        f"Runtime: {runtime_seconds:.1f} seconds",
        "",
        "Filters tested: direction long/short, frozen pre-2025 first30 q60/q70, and removing one allowed weekday from one leg.",
        "First30 thresholds come from a checksummed pre-2025 artifact; 2025+ data is not used for calibration.",
        "",
        "## First30 Thresholds",
        "",
        base_report.markdown_table(thresholds),
        "",
        "## Clean Improvements",
        "",
        base_report.markdown_table(improved_frame) if not improved_frame.empty else "No filter improved net R, DD, and PF together.",
        "",
        "## Best Per System",
        "",
        base_report.markdown_table(best),
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
