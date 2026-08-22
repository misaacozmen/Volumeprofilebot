from __future__ import annotations

import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_corrected_engine_3month_report as base_report
from backtest.data_loader import load_ohlcv
from backtest.volume_profile import compute_volume_profile


CASES_FILE = ROOT / "calibration_examples" / "manual_feedback_regression_v2.csv"
ZONE_LABELS_FILE = ROOT / "calibration_examples" / "manual_htf_zone_labels_v1.csv"
RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_htf_pd_array_diagnostics"
SYMBOL = "DUKASCOPY_USA500IDXUSD"
SOURCE_TIMEFRAME = "5m"
HTF_TIMEFRAMES = {"15m": "15min", "30m": "30min"}


@dataclass(frozen=True)
class HtfZone:
    date: str
    htf: str
    direction: str
    role: str
    formed_time: str
    lower: float
    upper: float
    size: float
    distance_from_open: float
    pre_open_state: str
    ny_window_state: str
    first_touch_time: str
    first_body_inside_time: str
    first_body_through_time: str
    relevant_to_va: bool


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cases = pd.read_csv(CASES_FILE, keep_default_na=False)
    frame = load_ohlcv(base_report.filter_paths(RAW_DIR, SYMBOL, SOURCE_TIMEFRAME)).frame
    htf_frames = {name: resample_ohlcv(frame, rule) for name, rule in HTF_TIMEFRAMES.items()}

    zones: list[HtfZone] = []
    summaries: list[dict[str, object]] = []
    for _, case in cases.iterrows():
        case_zones, summary = diagnose_case(frame, htf_frames, case)
        zones.extend(case_zones)
        summaries.append(summary)

    zones_frame = pd.DataFrame([asdict(zone) for zone in zones])
    summary_frame = pd.DataFrame(summaries)
    comparison = build_manual_comparison(cases, zones_frame)
    zone_labels = pd.read_csv(ZONE_LABELS_FILE, keep_default_na=False)
    exact_comparison = build_exact_zone_comparison(zone_labels, zones_frame)

    zones_frame.to_csv(REPORT_DIR / "htf_zones.csv", index=False)
    summary_frame.to_csv(REPORT_DIR / "case_summary.csv", index=False)
    comparison.to_csv(REPORT_DIR / "manual_state_comparison.csv", index=False)
    exact_comparison.to_csv(REPORT_DIR / "exact_zone_comparison.csv", index=False)
    write_report(summary_frame, comparison, exact_comparison)
    print(f"Wrote: {REPORT_DIR}")


def resample_ohlcv(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    indexed = frame.set_index("time")
    result = indexed.resample(rule, label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        candle_count=("close", "count"),
    )
    expected = int(pd.Timedelta(rule) / pd.Timedelta(minutes=5))
    result = result.dropna(subset=["open", "high", "low", "close"])
    result = result[result["candle_count"] == expected].reset_index()
    return result


def diagnose_case(
    frame: pd.DataFrame,
    htf_frames: dict[str, pd.DataFrame],
    case: pd.Series,
) -> tuple[list[HtfZone], dict[str, object]]:
    trade_date = pd.Timestamp(case["date"]).tz_localize("America/New_York")
    open_time = trade_date + pd.Timedelta(hours=9, minutes=30)
    window_end = trade_date + pd.Timedelta(hours=11, minutes=30)
    profile_start = trade_date - pd.Timedelta(hours=6)
    profile_frame = slice_time(frame, profile_start, open_time)
    profile = compute_volume_profile(profile_frame, rows=1000, value_area_pct=0.70)
    vah = float(profile.vah) if profile else float("nan")
    val = float(profile.val) if profile else float("nan")
    open_rows = slice_time(frame, open_time, open_time + pd.Timedelta(minutes=5))
    open_price = float(open_rows.iloc[0]["open"]) if not open_rows.empty else float("nan")

    case_zones: list[HtfZone] = []
    for htf, htf_frame in htf_frames.items():
        candidates = detect_fvgs(
            htf_frame,
            start=trade_date - pd.Timedelta(days=3),
            end=window_end,
        )
        for candidate in candidates:
            formed_time = candidate["formed_time"]
            if formed_time > window_end:
                continue
            state_rows = slice_time(htf_frame, formed_time, window_end + pd.Timedelta(minutes=30))
            pre_rows = state_rows[state_rows["time"] < open_time]
            ny_rows = state_rows[(state_rows["time"] >= open_time) & (state_rows["time"] <= window_end)]
            pre_state, _, _, _ = zone_state(pre_rows, candidate)
            ny_state, touch, inside, through = zone_state(ny_rows, candidate)
            # A zone formed before NY open and already closed through before 09:30 is stale.
            # Keep zones formed during the NY window so opening HTF arrays remain diagnosable.
            if formed_time < open_time and pre_state == "body_closed_through":
                continue
            distance = zone_distance(open_price, candidate["lower"], candidate["upper"])
            relevant = zone_near_va(candidate["lower"], candidate["upper"], vah, val, tolerance=15.0)
            if distance > 45.0 and not relevant:
                continue
            case_zones.append(HtfZone(
                date=case["date"],
                htf=htf,
                direction=candidate["direction"],
                role="support" if candidate["direction"] == "bullish" else "resistance",
                formed_time=formed_time.isoformat(),
                lower=round(candidate["lower"], 2),
                upper=round(candidate["upper"], 2),
                size=round(candidate["upper"] - candidate["lower"], 2),
                distance_from_open=round(distance, 2),
                pre_open_state=pre_state,
                ny_window_state=ny_state,
                first_touch_time=format_time(touch),
                first_body_inside_time=format_time(inside),
                first_body_through_time=format_time(through),
                relevant_to_va=bool(relevant),
            ))

    case_zones.sort(key=lambda zone: (zone.distance_from_open, zone.htf, zone.formed_time))
    nearest = case_zones[0] if case_zones else None
    summary = {
        "case_id": case["case_id"],
        "date": case["date"],
        "manual_decision": case["expected_decision"],
        "manual_direction": case["expected_direction"],
        "manual_htf_context": case["htf_context"],
        "manual_htf_body_state": case["htf_body_state"],
        "vah": round(vah, 2),
        "val": round(val, 2),
        "open_price": round(open_price, 2),
        "candidate_zone_count": len(case_zones),
        "nearest_htf": "" if nearest is None else nearest.htf,
        "nearest_direction": "" if nearest is None else nearest.direction,
        "nearest_role": "" if nearest is None else nearest.role,
        "nearest_zone": "" if nearest is None else f"{nearest.lower:.2f}-{nearest.upper:.2f}",
        "nearest_pre_open_state": "" if nearest is None else nearest.pre_open_state,
        "nearest_ny_window_state": "" if nearest is None else nearest.ny_window_state,
    }
    return case_zones, summary


def detect_fvgs(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, object]]:
    selected = slice_time(frame, start, end).reset_index(drop=True)
    candidates: list[dict[str, object]] = []
    for idx in range(len(selected) - 2):
        first = selected.iloc[idx]
        third = selected.iloc[idx + 2]
        if float(first["high"]) < float(third["low"]):
            candidates.append({
                "direction": "bullish",
                "formed_time": third["time"],
                "lower": float(first["high"]),
                "upper": float(third["low"]),
            })
        if float(first["low"]) > float(third["high"]):
            candidates.append({
                "direction": "bearish",
                "formed_time": third["time"],
                "lower": float(third["high"]),
                "upper": float(first["low"]),
            })
    return candidates


def zone_state(rows: pd.DataFrame, zone: dict[str, object]) -> tuple[str, pd.Timestamp | None, pd.Timestamp | None, pd.Timestamp | None]:
    lower = float(zone["lower"])
    upper = float(zone["upper"])
    direction = str(zone["direction"])
    touch = inside = through = None
    for _, candle in rows.iterrows():
        time = candle["time"]
        if touch is None and float(candle["high"]) >= lower and float(candle["low"]) <= upper:
            touch = time
        body_low = min(float(candle["open"]), float(candle["close"]))
        body_high = max(float(candle["open"]), float(candle["close"]))
        if inside is None and body_high >= lower and body_low <= upper:
            inside = time
        crossed = float(candle["close"]) < lower if direction == "bullish" else float(candle["close"]) > upper
        if through is None and crossed:
            through = time
    if through is not None:
        state = "body_closed_through"
    elif inside is not None:
        state = "body_closed_inside"
    elif touch is not None:
        state = "wick_touched_respected"
    else:
        state = "untouched"
    return state, touch, inside, through


def build_manual_comparison(cases: pd.DataFrame, zones: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    labelled = cases[~cases["htf_body_state"].isin(["", "none", "unknown", "not_applicable"])]
    for _, case in labelled.iterrows():
        day = zones[zones["date"] == case["date"]].copy() if not zones.empty else zones
        expected_direction = "bullish" if "bullish" in case["htf_context"].lower() else "bearish" if "bearish" in case["htf_context"].lower() else ""
        if expected_direction:
            day = day[day["direction"] == expected_direction]
        expected_transition = "body_closed_through" in case["htf_body_state"]
        if expected_transition and not expected_direction:
            # A body-close transition used to unlock a trade normally breaks the opposing array.
            expected_direction = "bullish" if case["expected_direction"] == "short" else "bearish"
            day = day[day["direction"] == expected_direction]
        if expected_transition:
            matches = day[day["ny_window_state"] == "body_closed_through"]
        else:
            matches = day[
                day["pre_open_state"].isin(["wick_touched_respected", "body_closed_inside", "untouched"])
                & day["ny_window_state"].isin(["wick_touched_respected", "body_closed_inside", "untouched"])
            ]
        best = matches.sort_values(["distance_from_open", "htf"]).iloc[0] if not matches.empty else None
        rows.append({
            "case_id": case["case_id"],
            "date": case["date"],
            "manual_state": case["htf_body_state"],
            "expected_htf_direction": expected_direction,
            "diagnostic_match": best is not None,
            "matched_htf": "" if best is None else best["htf"],
            "matched_zone": "" if best is None else f"{best['lower']:.2f}-{best['upper']:.2f}",
            "matched_state": "" if best is None else best["ny_window_state"],
            "distance_from_open": "" if best is None else best["distance_from_open"],
            "note": "Candidate match only; manual price-zone coordinates are not yet labelled.",
        })
    return pd.DataFrame(rows)


def build_exact_zone_comparison(labels: pd.DataFrame, zones: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, label in labels.iterrows():
        day = zones[
            (zones["date"] == label["date"])
            & (zones["htf"] == label["htf"])
            & (zones["direction"] == label["direction"])
        ].copy()
        manual_lower = float(label["manual_lower"])
        manual_upper = float(label["manual_upper"])
        if not day.empty:
            day["overlap"] = day.apply(
                lambda row: max(0.0, min(manual_upper, float(row["upper"])) - max(manual_lower, float(row["lower"]))),
                axis=1,
            )
            day["center_distance"] = (
                (day["lower"] + day["upper"]) / 2.0 - (manual_lower + manual_upper) / 2.0
            ).abs()
            day["padded_overlap"] = day.apply(
                lambda row: float(row["upper"]) >= manual_lower - 1.0 and float(row["lower"]) <= manual_upper + 1.0,
                axis=1,
            )
            # Prefer the zone whose center matches the manually drawn box. Wider nested
            # zones can have more raw overlap while representing a different PD array.
            matches = day[day["padded_overlap"]].sort_values(
                ["center_distance", "overlap"], ascending=[True, False]
            )
        else:
            matches = day
        best = matches.iloc[0] if not matches.empty else None
        expected = str(label["expected_state"])
        if best is None:
            state_match = False
        elif "body_closed_through" in expected:
            state_match = best["ny_window_state"] == "body_closed_through"
        elif "body_closed_inside" in expected:
            state_match = best["ny_window_state"] in {"body_closed_inside", "wick_touched_respected"}
        else:
            state_match = best["ny_window_state"] != "body_closed_through"
        rows.append({
            "case_id": label["case_id"],
            "date": label["date"],
            "manual_htf": label["htf"],
            "manual_direction": label["direction"],
            "manual_zone": f"{manual_lower:.2f}-{manual_upper:.2f}",
            "expected_state": expected,
            "zone_match": best is not None,
            "state_match": bool(state_match),
            "exact_match": best is not None and bool(state_match),
            "machine_formed_time": "" if best is None else best["formed_time"],
            "machine_zone": "" if best is None else f"{best['lower']:.2f}-{best['upper']:.2f}",
            "machine_pre_open_state": "" if best is None else best["pre_open_state"],
            "machine_ny_state": "" if best is None else best["ny_window_state"],
            "center_distance": "" if best is None else round(float(best["center_distance"]), 2),
            "source_url": label["source_url"],
            "confidence": label["confidence"],
        })
    return pd.DataFrame(rows)


def slice_time(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return frame[(frame["time"] >= start) & (frame["time"] < end)]


def zone_distance(price: float, lower: float, upper: float) -> float:
    if lower <= price <= upper:
        return 0.0
    return min(abs(price - lower), abs(price - upper))


def zone_near_va(lower: float, upper: float, vah: float, val: float, tolerance: float) -> bool:
    return any(lower - tolerance <= level <= upper + tolerance for level in [vah, val])


def format_time(value: pd.Timestamp | None) -> str:
    return "" if value is None else value.isoformat()


def write_report(summary: pd.DataFrame, comparison: pd.DataFrame, exact_comparison: pd.DataFrame) -> None:
    matched = int(comparison["diagnostic_match"].sum()) if not comparison.empty else 0
    exact_matched = int(exact_comparison["exact_match"].sum()) if not exact_comparison.empty else 0
    lines = [
        "# Manual HTF PD-Array Diagnostics",
        "",
        "Read-only diagnostic. It does not change CHALLENGE_CORE_V2, FON_CORE, or trade selection.",
        "",
        "15m/30m candles are resampled from the existing SPX 5m data. Standard wick FVGs are detected.",
        "States: untouched, wick_touched_respected, body_closed_inside, body_closed_through.",
        "",
        f"Manual HTF-labelled cases with a heuristic state candidate: {matched}/{len(comparison)}.",
        "The heuristic section is retained for audit; exact chart-derived zone validation is reported separately below.",
        "",
        "## Exact-zone validation",
        "",
        f"Exact manual zone and state matches: {exact_matched}/{len(exact_comparison)}.",
        "Manual bounds are chart-axis readings from the linked TradingView snapshots; a 1-point feed/reading tolerance is allowed.",
        "",
        base_report.markdown_table(exact_comparison),
        "",
        "## Manual-state comparison",
        "",
        base_report.markdown_table(comparison),
        "",
        "## Per-case summary",
        "",
        base_report.markdown_table(summary),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
