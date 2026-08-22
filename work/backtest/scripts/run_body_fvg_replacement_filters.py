from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_frequency_expansion_tests as frequency
from backtest.data_loader import load_ohlcv
from backtest.risk import apply_pair_risk_rule


SOURCE_DIR = ROOT / "outputs" / "reports" / "body_fvg_6month_validation"
REPORT_DIR = ROOT / "outputs" / "reports" / "body_fvg_replacement_filters"
SYSTEMS = ["phase_selected", "funded_selected"]
BODY_VARIANTS = ["spx_body_fvg_midpoint", "spx_body_fvg_ote705"]


def main() -> None:
    reset_report_dir()
    source = pd.read_csv(SOURCE_DIR / "all_trades.csv")
    source = enrich_body_fvg_features(source)
    rows: list[dict[str, object]] = []
    trade_frames: list[pd.DataFrame] = []

    for system in SYSTEMS:
        baseline_pair = capped_pair(build_pair(source, system, "baseline"))
        rows.append(build_row(system, "baseline", "Current baseline pair", baseline_pair, baseline_pair))
        tag_and_store(trade_frames, baseline_pair, system, "baseline")

        for body_variant in BODY_VARIANTS:
            body_spx = source[(source["system"] == system) & (source["leg_key"] == "spx") & (source["variant"] == body_variant)].copy()
            baseline_spx = source[(source["system"] == system) & (source["leg_key"] == "spx") & (source["variant"] == "baseline")].copy()

            variants = {
                f"{body_variant}_no_baseline_day": replacement_spx(baseline_spx, body_spx, mode="all"),
                f"{body_variant}_named_no_baseline_day": replacement_spx(baseline_spx, body_spx, mode="named"),
                f"{body_variant}_first_named_no_baseline_day": replacement_spx(baseline_spx, body_spx, mode="first_named"),
                f"{body_variant}_named_compact_no_baseline_day": replacement_spx(baseline_spx, body_spx, mode="named_compact"),
                f"{body_variant}_named_strong_displacement_no_baseline_day": replacement_spx(
                    baseline_spx, body_spx, mode="named_strong_displacement"
                ),
            }

            for variant_name, spx_filtered in variants.items():
                pair = build_replacement_pair(source, system, spx_filtered)
                capped = capped_pair(pair)
                rows.append(build_row(system, variant_name, variant_description(variant_name), capped, baseline_pair))
                tag_and_store(trade_frames, capped, system, variant_name)

    comparison = pd.DataFrame(rows)
    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    trades.to_csv(REPORT_DIR / "filtered_trades.csv", index=False)
    write_report(comparison)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_pair(source: pd.DataFrame, system: str, variant: str) -> pd.DataFrame:
    pair = source[(source["system"] == system) & (source["variant"] == variant)].copy()
    return pair


def build_replacement_pair(source: pd.DataFrame, system: str, spx: pd.DataFrame) -> pd.DataFrame:
    nq = source[(source["system"] == system) & (source["leg_key"] == "nq") & (source["variant"] == "baseline")].copy()
    frames = [frame for frame in [nq, spx] if not frame.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def replacement_spx(baseline_spx: pd.DataFrame, body_spx: pd.DataFrame, mode: str) -> pd.DataFrame:
    if baseline_spx.empty:
        baseline_dates: set[str] = set()
    else:
        baseline_dates = set(baseline_spx["date"].astype(str))
    replacement = body_spx[~body_spx["date"].astype(str).isin(baseline_dates)].copy()
    if mode in {"named", "first_named", "named_compact", "named_strong_displacement"}:
        replacement = replacement[~replacement["liquidity_context"].astype(str).str.startswith("swing_")].copy()
    if mode == "named_compact":
        replacement = replacement[pd.to_numeric(replacement["body_fvg_size"], errors="coerce") <= 2.0].copy()
    if mode == "named_strong_displacement":
        replacement = replacement[pd.to_numeric(replacement["fvg_body_ratio"], errors="coerce") >= 0.7].copy()
    if mode == "first_named" and not replacement.empty:
        replacement["entry_time_dt"] = pd.to_datetime(replacement["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
        replacement = replacement.sort_values(["date", "entry_time_dt"]).groupby("date", as_index=False, sort=False).head(1)
    return pd.concat([baseline_spx, replacement], ignore_index=True) if not baseline_spx.empty else replacement


def enrich_body_fvg_features(source: pd.DataFrame) -> pd.DataFrame:
    frame = source.copy()
    frame["body_fvg_size"] = pd.NA
    frame["fvg_body_ratio"] = pd.NA
    frame["body_fvg_size"] = pd.to_numeric(frame["body_fvg_size"], errors="coerce")
    frame["fvg_body_ratio"] = pd.to_numeric(frame["fvg_body_ratio"], errors="coerce")
    spx = load_ohlcv(base_report.filter_paths(ROOT / "data" / "raw", "DUKASCOPY_USA500IDXUSD", "5m")).frame
    spx = spx.sort_values("time").reset_index(drop=True)
    index_by_time = {str(timestamp): idx for idx, timestamp in enumerate(spx["time"])}
    mask = (frame["leg_key"] == "spx") & frame["variant"].astype(str).str.contains("body_fvg")
    for row_index, row in frame[mask].iterrows():
        fvg_time = str(pd.Timestamp(row["fvg_time"]).tz_convert("America/New_York"))
        idx = index_by_time.get(fvg_time)
        if idx is None or idx < 2:
            continue
        first = spx.iloc[idx - 2]
        third = spx.iloc[idx]
        first_body_low = min(float(first.open), float(first.close))
        first_body_high = max(float(first.open), float(first.close))
        third_body_low = min(float(third.open), float(third.close))
        third_body_high = max(float(third.open), float(third.close))
        if row["direction"] == "short":
            lower, upper = third_body_high, first_body_low
        else:
            lower, upper = first_body_high, third_body_low
        candle_range = float(third.high - third.low)
        candle_body = abs(float(third.close - third.open))
        frame.at[row_index, "body_fvg_size"] = round(float(upper - lower), 4)
        frame.at[row_index, "fvg_body_ratio"] = round(candle_body / candle_range, 4) if candle_range else 0.0
    return frame


def capped_pair(pair: pd.DataFrame) -> pd.DataFrame:
    if pair.empty:
        return pair
    capped = pair.copy()
    capped["entry_time_dt"] = pd.to_datetime(capped["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    capped["group"] = capped["system"]
    return apply_pair_risk_rule(capped, -1.0).sort_values("entry_time_dt").reset_index(drop=True)


def build_row(system: str, variant: str, description: str, trades: pd.DataFrame, baseline: pd.DataFrame) -> dict[str, object]:
    stats = frequency.stats(trades)
    base_stats = frequency.stats(baseline)
    daily = trades.groupby("date")["r_multiple"].sum() if not trades.empty else pd.Series(dtype=float)
    return {
        "system": system,
        "variant": variant,
        "description": description,
        **stats,
        "avg_trades_per_month": round(len(trades) / frequency.active_month_count(trades), 2) if len(trades) else 0.0,
        "worst_day_r": round(float(daily.min()), 2) if len(daily) else 0.0,
        "delta_trades": int(stats["trades"] - base_stats["trades"]),
        "delta_net_r": round(float(stats["net_r"]) - float(base_stats["net_r"]), 2),
        "delta_max_drawdown_r": round(float(stats["max_drawdown_r"]) - float(base_stats["max_drawdown_r"]), 2),
    }


def tag_and_store(frames: list[pd.DataFrame], trades: pd.DataFrame, system: str, filter_variant: str) -> None:
    if trades.empty:
        return
    tagged = trades.copy()
    tagged.insert(0, "filter_variant", filter_variant)
    tagged.insert(0, "filter_system", system)
    frames.append(tagged)


def variant_description(variant: str) -> str:
    if variant.endswith("_first_named_no_baseline_day"):
        return "Baseline SPX plus first named-session body-FVG only on SPX no-baseline-trade days"
    if variant.endswith("_named_compact_no_baseline_day"):
        return "Baseline SPX plus named-session compact body-FVG size <= 2.0 on SPX no-baseline-trade days"
    if variant.endswith("_named_strong_displacement_no_baseline_day"):
        return "Baseline SPX plus named-session strong displacement body-FVG ratio >= 0.7 on SPX no-baseline-trade days"
    if variant.endswith("_named_no_baseline_day"):
        return "Baseline SPX plus named-session body-FVG only on SPX no-baseline-trade days"
    return "Baseline SPX plus body-FVG only on SPX no-baseline-trade days"


def write_report(comparison: pd.DataFrame) -> None:
    lines = [
        "# Body-FVG Replacement Filter Tests",
        "",
        "Source: `outputs/reports/body_fvg_6month_validation/all_trades.csv`",
        "",
        "Goal: test body-FVG as a replacement/fallback, not broad additive frequency.",
        "",
        "Pair-level daily cap: `-1R`.",
        "",
    ]
    for system in SYSTEMS:
        subset = comparison[comparison["system"] == system].copy()
        lines.extend([f"## {system}", "", base_report.markdown_table(subset), ""])
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
