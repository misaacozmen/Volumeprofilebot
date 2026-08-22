from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_main_candidate_filter_tests as filters
import run_manual_rule_v2_validation as manual_v2
from backtest.data_loader import load_ohlcv
from backtest.strategy import (
    SweepEvent,
    build_liquidity_context,
    compute_first30_directionality,
    compute_first30_range,
    compute_volume_profile,
    detect_sweep,
    find_cisd,
    find_fvg_or_ifvg,
    near_value_area,
    time_slice,
    time_slice_from,
    timestamp_on_day,
    trades_to_frame,
    run_backtest,
)


RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_cisd_diagnostics"
CASES_FILE = ROOT / "calibration_examples" / "manual_regression_cases.csv"
TARGET_CASES = {"SPX-MAN-001", "SPX-MAN-002", "SPX-MAN-003"}
PROFILE = "challenge_core_v3_spx_session_opening_reversal"


def main() -> None:
    reset_report_dir()
    cases = pd.read_csv(CASES_FILE, keep_default_na=False)
    cases = cases[cases["case_id"].isin(TARGET_CASES)].copy()
    config = build_spx_profile_config()
    frame = load_ohlcv(base_report.filter_paths(RAW_DIR, "DUKASCOPY_USA500IDXUSD", "5m")).frame

    candidate_rows: list[dict[str, object]] = []
    candle_rows: list[dict[str, object]] = []
    trade_rows: list[pd.DataFrame] = []
    for date_text in sorted(cases["date"].unique()):
        selected = select_window(frame, date_text)
        day_result = run_backtest(selected.copy(), config)
        trades = trades_to_frame(day_result.trades)
        if not trades.empty:
            trades.insert(0, "profile", PROFILE)
            trade_rows.append(trades)
        candidate_rows.extend(build_day_diagnostics(selected, date_text, config))
        candle_rows.extend(build_candle_context(selected, date_text))

    candidates = pd.DataFrame(candidate_rows)
    candles = pd.DataFrame(candle_rows)
    trades = pd.concat(trade_rows, ignore_index=True) if trade_rows else pd.DataFrame()

    candidates.to_csv(REPORT_DIR / "cisd_candidates.csv", index=False)
    candles.to_csv(REPORT_DIR / "candle_context.csv", index=False)
    trades.to_csv(REPORT_DIR / "actual_trades.csv", index=False)
    write_report(cases, candidates, trades)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_spx_profile_config():
    loaded = filters.load_data()
    thresholds = filters.build_thresholds(loaded)
    threshold_lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}
    for leg in filters.build_leg_specs():
        if leg.system == "phase_selected" and leg.leg_key == "spx":
            spec = filters.VariantSpec("baseline", "baseline", "engine")
            config = manual_v2.manual_v2_config(filters.build_variant_config(leg, spec, threshold_lookup))
            return replace(config, session_liquidity_only=True, opening_premarket_direction_mode="reversal_only")
    raise SystemExit("Missing phase_selected SPX leg")


def select_window(frame: pd.DataFrame, date_text: str) -> pd.DataFrame:
    start = pd.Timestamp(date_text).tz_localize("America/New_York") - pd.Timedelta(days=2)
    end = pd.Timestamp(date_text).tz_localize("America/New_York") + pd.Timedelta(days=1)
    return frame[(frame["time"] >= start) & (frame["time"] < end)].drop_duplicates("time").sort_values("time").reset_index(drop=True)


def build_day_diagnostics(frame: pd.DataFrame, date_text: str, config) -> list[dict[str, object]]:
    trade_date = pd.Timestamp(date_text).date()
    profile_start = pd.Timestamp(trade_date).tz_localize("America/New_York") - pd.Timedelta(hours=6)
    profile_end = pd.Timestamp(trade_date).tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=30)
    trade_start = timestamp_on_day(trade_date, config.trade_window_start)
    trade_end = max(timestamp_on_day(trade_date, config.trade_window_end), timestamp_on_day(trade_date, "11:30"))

    profile = compute_volume_profile(
        time_slice(frame, profile_start, profile_end),
        rows=config.volume_profile_rows,
        value_area_pct=config.value_area_pct,
    )
    if profile is None:
        return []

    liquidity_levels, opening_sweeps = build_liquidity_context(frame, trade_date, trade_start, config)
    rows = list(time_slice(frame, trade_start, trade_end).itertuples(index=False))
    first30_range = compute_first30_range(frame, trade_date)
    first30_directionality = compute_first30_directionality(frame, trade_date)
    swept_levels: set[str] = set()
    last_sweep: SweepEvent | None = opening_sweeps[-1] if opening_sweeps else None
    last_trade_direction: str | None = None
    output: list[dict[str, object]] = []

    for index, candle in enumerate(rows):
        near_va = near_value_area(candle, profile.vah, profile.val, config.vah_val_tolerance)
        fresh_sweep = detect_sweep(index, candle, liquidity_levels, swept_levels, profile.vah, profile.val, config)
        used_sweep = fresh_sweep
        direction_override = None
        candidate_source = "fresh_sweep" if fresh_sweep is not None else ""

        if fresh_sweep is None and near_va and last_sweep is not None:
            used_sweep = SweepEvent(index=index, level=last_sweep.level, is_fresh=False, source=last_sweep.source)
            direction_override = last_trade_direction
            if (
                direction_override is None
                and last_sweep.source == "opening_premarket"
                and config.opening_premarket_direction_mode == "reversal_only"
            ):
                direction_override = "short" if last_sweep.level.side == "high" else "long"
            candidate_source = "continuation"

        if used_sweep is None or not near_va:
            continue

        directions = [direction_override] if direction_override else ["long", "short"]
        for direction in directions:
            if direction is None:
                continue
            output.append(
                evaluate_candidate(
                    date_text,
                    rows,
                    index,
                    candle,
                    used_sweep,
                    direction,
                    candidate_source,
                    profile.vah,
                    profile.val,
                    config,
                    first30_range,
                    first30_directionality,
                )
            )

        if fresh_sweep is not None:
            swept_levels.add(fresh_sweep.level.name)

    return output


def evaluate_candidate(
    date_text: str,
    rows: list,
    index: int,
    candle,
    sweep: SweepEvent,
    direction: str,
    candidate_source: str,
    vah: float,
    val: float,
    config,
    first30_range: float | None,
    first30_directionality: float | None,
) -> dict[str, object]:
    cisd_index, cisd_stop, invalidation_level = find_cisd(
        rows,
        index,
        direction,
        config.require_post_sweep_cisd_reference,
        config.cisd_lookahead_candles,
    )
    long_cisd_index, _, _ = find_cisd(rows, index, direction, config.require_post_sweep_cisd_reference, 18)
    fvg_index = None
    entry_price = None
    setup_kind = None
    zone_lower = None
    zone_upper = None
    late_fvg_index = None
    late_setup_kind = None
    body_fvg_index = None
    body_fvg_entry = None
    body_fvg_kind = None
    body_fvg_zone_lower = None
    body_fvg_zone_upper = None
    body_fvg_ote705_entry = None
    reject_reason = ""
    if cisd_index is None:
        reject_reason = f"no_cisd_within_{config.cisd_lookahead_candles}"
    else:
        fvg_index, entry_price, setup_kind, zone_lower, zone_upper, _ = find_fvg_or_ifvg(
            rows,
            cisd_index,
            direction,
            config.fvg_entry_mode,
            config.setup_type_filter,
            config.min_fvg_points,
            invalidation_level if config.invalidate_cisd_before_fvg else None,
            config.fvg_window_candles,
        )
        if fvg_index is None:
            reject_reason = f"no_{config.setup_type_filter}_within_{config.fvg_window_candles}_after_cisd"
            late_fvg_index, _, late_setup_kind, _, _, _ = find_fvg_or_ifvg(
                rows,
                cisd_index,
                direction,
                config.fvg_entry_mode,
                config.setup_type_filter,
                config.min_fvg_points,
                invalidation_level if config.invalidate_cisd_before_fvg else None,
                12,
            )
        body_fvg_index, body_fvg_entry, body_fvg_kind, body_fvg_zone_lower, body_fvg_zone_upper, _ = find_fvg_or_ifvg(
            rows,
            cisd_index,
            direction,
            "midpoint",
            "body_fvg",
            config.min_fvg_points,
            invalidation_level if config.invalidate_cisd_before_fvg else None,
            12,
        )
        if body_fvg_zone_lower is not None and body_fvg_zone_upper is not None:
            body_fvg_ote705_entry = body_fvg_zone_upper - (body_fvg_zone_upper - body_fvg_zone_lower) * 0.705
            if direction == "short":
                body_fvg_ote705_entry = body_fvg_zone_lower + (body_fvg_zone_upper - body_fvg_zone_lower) * 0.705

    return {
        "date": date_text,
        "candidate_time": candle.time.isoformat(),
        "candidate_source": candidate_source,
        "near_va": True,
        "vah": round(vah, 2),
        "val": round(val, 2),
        "sweep_level": sweep.level.name,
        "sweep_side": sweep.level.side,
        "sweep_price": round(float(sweep.level.price), 2),
        "sweep_source": sweep.source,
        "is_fresh_sweep": sweep.is_fresh,
        "direction": direction,
        "cisd_time": "" if cisd_index is None else rows[cisd_index].time.isoformat(),
        "cisd_stop": "" if cisd_stop is None else round(float(cisd_stop), 2),
        "cisd_invalidation": "" if invalidation_level is None else round(float(invalidation_level), 2),
        "late_cisd_time_18": "" if long_cisd_index is None else rows[long_cisd_index].time.isoformat(),
        "fvg_time": "" if fvg_index is None else rows[fvg_index].time.isoformat(),
        "late_fvg_time_12": "" if late_fvg_index is None else rows[late_fvg_index].time.isoformat(),
        "setup_kind": setup_kind or "",
        "late_setup_kind_12": late_setup_kind or "",
        "body_fvg_time_12": "" if body_fvg_index is None else rows[body_fvg_index].time.isoformat(),
        "body_fvg_kind_12": body_fvg_kind or "",
        "body_fvg_zone_lower": "" if body_fvg_zone_lower is None else round(float(body_fvg_zone_lower), 2),
        "body_fvg_zone_upper": "" if body_fvg_zone_upper is None else round(float(body_fvg_zone_upper), 2),
        "body_fvg_midpoint_entry": "" if body_fvg_entry is None else round(float(body_fvg_entry), 2),
        "body_fvg_ote705_entry": "" if body_fvg_ote705_entry is None else round(float(body_fvg_ote705_entry), 2),
        "zone_lower": "" if zone_lower is None else round(float(zone_lower), 2),
        "zone_upper": "" if zone_upper is None else round(float(zone_upper), 2),
        "entry_price": "" if entry_price is None else round(float(entry_price), 2),
        "reject_reason": reject_reason,
        "first30_range": "" if first30_range is None else round(float(first30_range), 2),
        "first30_directionality": "" if first30_directionality is None else round(float(first30_directionality), 4),
    }


def build_candle_context(frame: pd.DataFrame, date_text: str) -> list[dict[str, object]]:
    trade_date = pd.Timestamp(date_text).date()
    start = timestamp_on_day(trade_date, "09:30")
    end = timestamp_on_day(trade_date, "11:30")
    rows = []
    for candle in time_slice(frame, start, end).itertuples(index=False):
        rows.append(
            {
                "date": date_text,
                "time": candle.time.isoformat(),
                "open": round(float(candle.open), 2),
                "high": round(float(candle.high), 2),
                "low": round(float(candle.low), 2),
                "close": round(float(candle.close), 2),
            }
        )
    return rows


def write_report(cases: pd.DataFrame, candidates: pd.DataFrame, trades: pd.DataFrame) -> None:
    lines = [
        "# Manual CISD Diagnostics",
        "",
        f"Profile: `{PROFILE}`",
        "",
        "## Cases",
        markdown_table(cases),
        "",
        "## Actual Trades",
        markdown_table(trades) if not trades.empty else "No trades.",
        "",
        "## Candidate Summary",
    ]
    if candidates.empty:
        lines.append("No candidates.")
    else:
        summary = (
            candidates.groupby(["date", "direction", "reject_reason"], dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values(["date", "direction", "reject_reason"])
        )
        lines.append(markdown_table(summary))
        lines.extend(["", "## Candidate Rows"])
        display = candidates[
            [
                "date",
                "candidate_time",
                "candidate_source",
                "sweep_level",
                "sweep_side",
                "direction",
                "cisd_time",
                "late_cisd_time_18",
                "fvg_time",
                "late_fvg_time_12",
                "setup_kind",
                "late_setup_kind_12",
                "body_fvg_time_12",
                "body_fvg_kind_12",
                "body_fvg_midpoint_entry",
                "body_fvg_ote705_entry",
                "reject_reason",
            ]
        ]
        lines.append(markdown_table(display))
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "No rows."
    columns = list(frame.columns)
    rows = []
    rows.append("| " + " | ".join(columns) + " |")
    rows.append("| " + " | ".join(["---"] * len(columns)) + " |")
    for _, row in frame.iterrows():
        values = [str(row[column]).replace("\n", " ") for column in columns]
        rows.append("| " + " | ".join(values) + " |")
    return "\n".join(rows)


if __name__ == "__main__":
    main()
