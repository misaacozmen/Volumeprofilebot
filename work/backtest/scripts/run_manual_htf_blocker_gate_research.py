from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = ROOT / "calibration_examples" / "manual_feedback_regression_v2.csv"
REGRESSION_DIR = ROOT / "outputs" / "reports" / "manual_feedback_regression_v2"
HTF_DIR = ROOT / "outputs" / "reports" / "manual_htf_pd_array_diagnostics"
OB_DIR = ROOT / "outputs" / "reports" / "manual_htf_orderblock_diagnostics"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_htf_blocker_gate_research"

SOURCE_PROFILES = {
    "challenge_core_v2": "challenge_core_v2",
    "body_fvg_start_followup": "challenge_core_v3_spx_body_fvg_start_followup",
}
DISTANCE_VARIANTS = {
    "htf_blocker_10pt": 10.0,
    "htf_blocker_15pt": 15.0,
    "htf_blocker_25pt": 25.0,
}


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cases = pd.read_csv(CASES_FILE, keep_default_na=False)
    trades = pd.read_csv(REGRESSION_DIR / "actual_trades.csv", keep_default_na=False)
    zones = pd.read_csv(HTF_DIR / "htf_zones.csv", keep_default_na=False)
    order_blocks = pd.read_csv(OB_DIR / "htf_order_blocks.csv", keep_default_na=False)

    filtered_frames: list[pd.DataFrame] = []
    decision_rows: list[dict[str, object]] = []
    blocker_rows: list[dict[str, object]] = []

    for source_label, source_profile in SOURCE_PROFILES.items():
        source_trades = trades[trades["profile"] == source_profile].copy()
        decision_rows.extend(compare_cases(cases, source_trades, source_label, "no_gate"))
        for variant, max_distance in DISTANCE_VARIANTS.items():
            filtered, blocked = apply_gate(source_trades, zones, max_distance)
            filtered.insert(0, "gate_variant", variant)
            filtered.insert(0, "source_profile", source_label)
            filtered_frames.append(filtered)
            for row in blocked:
                blocker_rows.append({"source_profile": source_label, "gate_variant": variant, **row})
            decision_rows.extend(compare_cases(cases, filtered, source_label, variant))
        dominant_variant = "dominant_htf_15pt"
        filtered, blocked = apply_dominant_gate(source_trades, zones, 15.0)
        filtered.insert(0, "gate_variant", dominant_variant)
        filtered.insert(0, "source_profile", source_label)
        filtered_frames.append(filtered)
        for row in blocked:
            blocker_rows.append({"source_profile": source_label, "gate_variant": dominant_variant, **row})
        decision_rows.extend(compare_cases(cases, filtered, source_label, dominant_variant))
        controlling_variant = "controlling_fvg_ob_15pt"
        filtered, blocked = apply_controlling_array_gate(source_trades, zones, order_blocks, 15.0)
        filtered.insert(0, "gate_variant", controlling_variant)
        filtered.insert(0, "source_profile", source_label)
        filtered_frames.append(filtered)
        for row in blocked:
            blocker_rows.append({"source_profile": source_label, "gate_variant": controlling_variant, **row})
        decision_rows.extend(compare_cases(cases, filtered, source_label, controlling_variant))
        opening_variant = "opening_controlling_fvg_ob_15pt"
        filtered, blocked = apply_controlling_array_gate(
            source_trades, zones, order_blocks, 15.0, opening_touch_only=True
        )
        filtered.insert(0, "gate_variant", opening_variant)
        filtered.insert(0, "source_profile", source_label)
        filtered_frames.append(filtered)
        for row in blocked:
            blocker_rows.append({"source_profile": source_label, "gate_variant": opening_variant, **row})
        decision_rows.extend(compare_cases(cases, filtered, source_label, opening_variant))

    decisions = pd.DataFrame(decision_rows)
    summary = summarize(decisions)
    blocked = pd.DataFrame(blocker_rows)
    filtered_all = pd.concat(filtered_frames, ignore_index=True) if filtered_frames else pd.DataFrame()

    decisions.to_csv(REPORT_DIR / "comparison.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    blocked.to_csv(REPORT_DIR / "blocked_trades.csv", index=False)
    filtered_all.to_csv(REPORT_DIR / "filtered_trades.csv", index=False)
    write_report(summary, decisions, blocked)
    print(f"Wrote: {REPORT_DIR}")


def apply_gate(
    trades: pd.DataFrame,
    zones: pd.DataFrame,
    max_distance: float,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    kept_indices: list[int] = []
    blocked_rows: list[dict[str, object]] = []
    for index, trade in trades.iterrows():
        entry_time = pd.Timestamp(trade["entry_time"])
        entry_price = float(trade["entry_price"])
        opposing = "bearish" if trade["direction"] == "long" else "bullish"
        day = zones[(zones["date"] == trade["date"]) & (zones["direction"] == opposing)].copy()
        candidates: list[dict[str, object]] = []
        for _, zone in day.iterrows():
            formed = pd.Timestamp(zone["formed_time"])
            if formed > entry_time:
                continue
            through_text = str(zone["first_body_through_time"])
            if through_text and pd.Timestamp(through_text) <= entry_time:
                continue
            touch_text = str(zone["first_touch_time"])
            if not touch_text or pd.Timestamp(touch_text) > entry_time:
                continue
            lower = float(zone["lower"])
            upper = float(zone["upper"])
            distance = price_to_zone_distance(entry_price, lower, upper)
            if distance > max_distance:
                continue
            if trade["direction"] == "long" and upper < entry_price:
                continue
            if trade["direction"] == "short" and lower > entry_price:
                continue
            candidates.append({
                "htf": zone["htf"],
                "zone_direction": zone["direction"],
                "zone_lower": lower,
                "zone_upper": upper,
                "zone_distance": round(distance, 2),
                "zone_formed_time": zone["formed_time"],
                "zone_pre_open_state": zone["pre_open_state"],
            })
        if not candidates:
            kept_indices.append(index)
            continue
        blocker = sorted(candidates, key=lambda item: (item["zone_distance"], item["htf"]))[0]
        blocked_rows.append({
            "date": trade["date"],
            "trade_direction": trade["direction"],
            "entry_time": trade["entry_time"],
            "entry_price": entry_price,
            "liquidity_context": trade["liquidity_context"],
            **blocker,
        })
    return trades.loc[kept_indices].reset_index(drop=True), blocked_rows


def price_to_zone_distance(price: float, lower: float, upper: float) -> float:
    if lower <= price <= upper:
        return 0.0
    return min(abs(price - lower), abs(price - upper))


def apply_dominant_gate(
    trades: pd.DataFrame,
    zones: pd.DataFrame,
    max_distance: float,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    kept_indices: list[int] = []
    blocked_rows: list[dict[str, object]] = []
    for index, trade in trades.iterrows():
        entry_time = pd.Timestamp(trade["entry_time"])
        entry_price = float(trade["entry_price"])
        day = zones[zones["date"] == trade["date"]]
        candidates: list[dict[str, object]] = []
        for _, zone in day.iterrows():
            formed = pd.Timestamp(zone["formed_time"])
            if formed > entry_time:
                continue
            through_text = str(zone["first_body_through_time"])
            if through_text and pd.Timestamp(through_text) <= entry_time:
                continue
            touch_text = str(zone["first_touch_time"])
            if not touch_text or pd.Timestamp(touch_text) > entry_time:
                continue
            lower = float(zone["lower"])
            upper = float(zone["upper"])
            distance = price_to_zone_distance(entry_price, lower, upper)
            if distance > max_distance:
                continue
            # Bullish FVGs act as support below/around entry; bearish FVGs act as
            # resistance above/around entry. Ignore zones on the wrong side.
            if zone["direction"] == "bullish" and lower > entry_price:
                continue
            if zone["direction"] == "bearish" and upper < entry_price:
                continue
            candidates.append({
                "htf": zone["htf"],
                "zone_direction": zone["direction"],
                "zone_lower": lower,
                "zone_upper": upper,
                "zone_distance": round(distance, 2),
                "zone_formed_time": zone["formed_time"],
                "zone_pre_open_state": zone["pre_open_state"],
                "formed_dt": formed,
            })
        if not candidates:
            kept_indices.append(index)
            continue
        dominant = sorted(
            candidates,
            key=lambda item: (item["formed_dt"], item["htf"] == "30m", -item["zone_distance"]),
            reverse=True,
        )[0]
        opposing = "bearish" if trade["direction"] == "long" else "bullish"
        if dominant["zone_direction"] != opposing:
            kept_indices.append(index)
            continue
        dominant.pop("formed_dt", None)
        blocked_rows.append({
            "date": trade["date"],
            "trade_direction": trade["direction"],
            "entry_time": trade["entry_time"],
            "entry_price": entry_price,
            "liquidity_context": trade["liquidity_context"],
            **dominant,
        })
    return trades.loc[kept_indices].reset_index(drop=True), blocked_rows


def apply_controlling_array_gate(
    trades: pd.DataFrame,
    fvg_zones: pd.DataFrame,
    order_blocks: pd.DataFrame,
    max_distance: float,
    opening_touch_only: bool = False,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    kept_indices: list[int] = []
    blocked_rows: list[dict[str, object]] = []
    for index, trade in trades.iterrows():
        entry_time = pd.Timestamp(trade["entry_time"])
        entry_price = float(trade["entry_price"])
        candidates: list[dict[str, object]] = []

        for _, zone in fvg_zones[fvg_zones["date"] == trade["date"]].iterrows():
            formed = pd.Timestamp(zone["formed_time"])
            if formed > entry_time:
                continue
            touch_text = str(zone["first_touch_time"])
            if not touch_text or pd.Timestamp(touch_text) > entry_time:
                continue
            if opening_touch_only and not opening_touch(zone["date"], touch_text):
                continue
            candidate = controlling_candidate(
                array_type="fvg",
                direction=zone["direction"],
                htf=zone["htf"],
                lower=float(zone["lower"]),
                upper=float(zone["upper"]),
                formed=formed,
                entry_price=entry_price,
                max_distance=max_distance,
                transition_time=str(zone["first_body_through_time"]),
                entry_time=entry_time,
            )
            if candidate:
                candidates.append(candidate)

        for _, zone in order_blocks[order_blocks["date"] == trade["date"]].iterrows():
            confirmed = pd.Timestamp(zone["confirmed_time"])
            if confirmed > entry_time:
                continue
            touch_text = str(zone["first_touch_time"])
            if not touch_text or pd.Timestamp(touch_text) > entry_time:
                continue
            if opening_touch_only and not opening_touch(zone["date"], touch_text):
                continue
            # For an OB, a candle body closing back inside the body-zone is the
            # manual transition; complete close-through is not required.
            transition_text = str(zone["first_body_inside_time"] or zone["first_body_through_time"])
            candidate = controlling_candidate(
                array_type="order_block",
                direction=zone["direction"],
                htf=zone["htf"],
                lower=float(zone["lower"]),
                upper=float(zone["upper"]),
                formed=confirmed,
                entry_price=entry_price,
                max_distance=max_distance,
                transition_time=transition_text,
                entry_time=entry_time,
            )
            if candidate:
                candidates.append(candidate)

        if not candidates:
            kept_indices.append(index)
            continue
        controlling = sorted(
            candidates,
            key=lambda item: (item["formed_dt"], item["array_type"] == "order_block", -item["zone_distance"]),
            reverse=True,
        )[0]
        opposing = "bearish" if trade["direction"] == "long" else "bullish"
        if controlling["zone_direction"] != opposing or controlling["transition_complete"]:
            kept_indices.append(index)
            continue
        controlling.pop("formed_dt", None)
        blocked_rows.append({
            "date": trade["date"],
            "trade_direction": trade["direction"],
            "entry_time": trade["entry_time"],
            "entry_price": entry_price,
            "liquidity_context": trade["liquidity_context"],
            **controlling,
        })
    return trades.loc[kept_indices].reset_index(drop=True), blocked_rows


def opening_touch(date_text: str, touch_text: str) -> bool:
    day = pd.Timestamp(date_text).tz_localize("America/New_York")
    start = day + pd.Timedelta(hours=9, minutes=30)
    end = day + pd.Timedelta(hours=10)
    touch = pd.Timestamp(touch_text)
    return start <= touch <= end


def controlling_candidate(
    *,
    array_type: str,
    direction: str,
    htf: str,
    lower: float,
    upper: float,
    formed: pd.Timestamp,
    entry_price: float,
    max_distance: float,
    transition_time: str,
    entry_time: pd.Timestamp,
) -> dict[str, object] | None:
    distance = price_to_zone_distance(entry_price, lower, upper)
    if distance > max_distance:
        return None
    if direction == "bullish" and lower > entry_price:
        return None
    if direction == "bearish" and upper < entry_price:
        return None
    transitioned = bool(transition_time) and pd.Timestamp(transition_time) <= entry_time
    return {
        "array_type": array_type,
        "htf": htf,
        "zone_direction": direction,
        "zone_lower": lower,
        "zone_upper": upper,
        "zone_distance": round(distance, 2),
        "zone_formed_time": formed.isoformat(),
        "transition_complete": transitioned,
        "formed_dt": formed,
    }


def compare_cases(cases: pd.DataFrame, trades: pd.DataFrame, source_profile: str, variant: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for _, case in cases.iterrows():
        day = trades[trades["date"] == case["date"]].copy()
        if not day.empty:
            day["entry_dt"] = pd.to_datetime(day["entry_time"], utc=True, format="mixed")
            day = day.sort_values("entry_dt")
        order = int(case["expected_order"] or 1)
        matched = day.iloc[order - 1] if len(day) >= order else None
        expected = case["expected_decision"]
        direction_ok = case["expected_direction"] == "" or (
            matched is not None and matched["direction"] == case["expected_direction"]
        )
        if expected == "SKIP":
            status = "PASS" if day.empty else "FAIL"
        elif expected == "TAKE":
            status = "PASS" if matched is not None and direction_ok else "FAIL"
        else:
            status = "WATCH"
        rows.append({
            "source_profile": source_profile,
            "gate_variant": variant,
            "case_id": case["case_id"],
            "date": case["date"],
            "issue_type": case["issue_type"],
            "expected_decision": expected,
            "expected_direction": case["expected_direction"],
            "actual_decision": "TAKE" if not day.empty else "SKIP",
            "actual_trade_count": len(day),
            "matched_direction": "" if matched is None else matched["direction"],
            "status": status,
        })
    return rows


def summarize(comparison: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for (source, variant), group in comparison.groupby(["source_profile", "gate_variant"], sort=False):
        scored = group[group["status"] != "WATCH"]
        rows.append({
            "source_profile": source,
            "gate_variant": variant,
            "cases": len(group),
            "passed": int((scored["status"] == "PASS").sum()),
            "failed": int((scored["status"] == "FAIL").sum()),
            "pass_rate": round(float((scored["status"] == "PASS").mean() * 100), 2),
        })
    return pd.DataFrame(rows)


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_No rows._"
    columns = list(frame.columns)
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(str(row[column]).replace("|", "/") for column in columns) + " |")
    return "\n".join(lines)


def write_report(summary: pd.DataFrame, comparison: pd.DataFrame, blocked: pd.DataFrame) -> None:
    best = summary.sort_values(["passed", "source_profile"], ascending=[False, True]).head(1)
    lines = [
        "# Manual HTF Blocker Gate Research",
        "",
        "Manual-regression-only post-filter. It does not change strategy.py, config defaults, CHALLENGE_CORE_V2, or FON_CORE.",
        "",
        "Rules under research: opposing active/touched 15m/30m FVG blockers, dominant FVG selection, and a controlling-array variant combining FVGs with 15m order blocks. Aligned arrays never create a trade by themselves.",
        "",
        "## Summary",
        "",
        markdown_table(summary),
        "",
        "## Best row",
        "",
        markdown_table(best),
        "",
        "## Blocked trades",
        "",
        markdown_table(blocked),
        "",
        "## Case comparison",
        "",
        markdown_table(comparison),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
