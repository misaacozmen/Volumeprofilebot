from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_corrected_engine_3month_report as base_report
from backtest.data_loader import load_ohlcv
from run_manual_htf_pd_array_diagnostics import resample_ohlcv, slice_time, zone_state


RAW_DIR = ROOT / "data" / "raw"
CASES_FILE = ROOT / "calibration_examples" / "manual_feedback_regression_v2.csv"
LABELS_FILE = ROOT / "calibration_examples" / "manual_htf_orderblock_labels_v1.csv"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_htf_orderblock_diagnostics"
SYMBOL = "DUKASCOPY_USA500IDXUSD"


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cases = pd.read_csv(CASES_FILE, keep_default_na=False)
    labels = pd.read_csv(LABELS_FILE, keep_default_na=False)
    frame = load_ohlcv(base_report.filter_paths(RAW_DIR, SYMBOL, "5m")).frame
    htf = resample_ohlcv(frame, "15min")

    rows: list[dict[str, object]] = []
    for date_text in cases["date"]:
        trade_date = pd.Timestamp(date_text).tz_localize("America/New_York")
        open_time = trade_date + pd.Timedelta(hours=9, minutes=30)
        end_time = trade_date + pd.Timedelta(hours=11, minutes=30)
        candidates = detect_order_blocks(htf, trade_date - pd.Timedelta(days=2), end_time)
        for candidate in candidates:
            state_rows = slice_time(htf, candidate["confirmed_time"], end_time + pd.Timedelta(minutes=15))
            pre_rows = state_rows[state_rows["time"] < open_time]
            ny_rows = state_rows[state_rows["time"] >= open_time]
            pre_state, _, _, pre_through = zone_state(pre_rows, candidate)
            if candidate["confirmed_time"] < open_time and pre_through is not None:
                continue
            ny_state, touch, inside, through = zone_state(ny_rows, candidate)
            rows.append({
                "date": date_text,
                "htf": "15m",
                "array_type": "order_block",
                "direction": candidate["direction"],
                "role": "support" if candidate["direction"] == "bullish" else "resistance",
                "origin_time": candidate["origin_time"].isoformat(),
                "confirmed_time": candidate["confirmed_time"].isoformat(),
                "lower": round(candidate["lower"], 3),
                "upper": round(candidate["upper"], 3),
                "displacement_ratio": round(candidate["displacement_ratio"], 3),
                "pre_open_state": pre_state,
                "ny_window_state": ny_state,
                "first_touch_time": format_time(touch),
                "first_body_inside_time": format_time(inside),
                "first_body_through_time": format_time(through),
            })

    candidates_frame = pd.DataFrame(rows)
    comparison = compare_labels(labels, candidates_frame)
    candidates_frame.to_csv(REPORT_DIR / "htf_order_blocks.csv", index=False)
    comparison.to_csv(REPORT_DIR / "exact_orderblock_comparison.csv", index=False)
    write_report(comparison)
    print(f"Wrote: {REPORT_DIR}")


def detect_order_blocks(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, object]]:
    selected = slice_time(frame, start, end).reset_index(drop=True)
    output: list[dict[str, object]] = []
    for index in range(len(selected) - 1):
        origin = selected.iloc[index]
        impulse = selected.iloc[index + 1]
        impulse_range = float(impulse["high"] - impulse["low"])
        if impulse_range <= 0:
            continue
        ratio = abs(float(impulse["close"] - impulse["open"])) / impulse_range
        if ratio < 0.55:
            continue
        origin_bearish = float(origin["close"]) < float(origin["open"])
        origin_bullish = float(origin["close"]) > float(origin["open"])
        bullish_displacement = (
            float(impulse["close"]) > float(impulse["open"])
            and float(impulse["close"]) > float(origin["high"])
        )
        bearish_displacement = (
            float(impulse["close"]) < float(impulse["open"])
            and float(impulse["close"]) < float(origin["low"])
        )
        direction = "bullish" if origin_bearish and bullish_displacement else "bearish" if origin_bullish and bearish_displacement else ""
        if not direction:
            continue
        output.append({
            "direction": direction,
            "origin_time": origin["time"],
            "confirmed_time": impulse["time"] + pd.Timedelta(minutes=15),
            "lower": min(float(origin["open"]), float(origin["close"])),
            "upper": max(float(origin["open"]), float(origin["close"])),
            "displacement_ratio": ratio,
        })
    return output


def compare_labels(labels: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, label in labels.iterrows():
        day = candidates[
            (candidates["date"] == label["date"])
            & (candidates["direction"] == label["direction"])
        ].copy()
        lower = float(label["manual_lower"])
        upper = float(label["manual_upper"])
        day["center_distance"] = abs((day["lower"] + day["upper"]) / 2 - (lower + upper) / 2)
        day["overlaps"] = (day["upper"] >= lower - 1.0) & (day["lower"] <= upper + 1.0)
        matches = day[day["overlaps"]].sort_values("center_distance")
        best = matches.iloc[0] if not matches.empty else None
        state_match = best is not None and best["ny_window_state"] in {"body_closed_inside", "body_closed_through"}
        rows.append({
            "case_id": label["case_id"],
            "date": label["date"],
            "manual_zone": f"{lower:.2f}-{upper:.2f}",
            "expected_state": label["expected_state"],
            "zone_match": best is not None,
            "state_match": bool(state_match),
            "machine_zone": "" if best is None else f"{best['lower']:.2f}-{best['upper']:.2f}",
            "origin_time": "" if best is None else best["origin_time"],
            "confirmed_time": "" if best is None else best["confirmed_time"],
            "machine_state": "" if best is None else best["ny_window_state"],
            "first_body_inside_time": "" if best is None else best["first_body_inside_time"],
            "center_distance": "" if best is None else round(float(best["center_distance"]), 2),
            "source_url": label["source_url"],
        })
    return pd.DataFrame(rows)


def format_time(value: pd.Timestamp | None) -> str:
    return "" if value is None else value.isoformat()


def markdown_table(frame: pd.DataFrame) -> str:
    columns = list(frame.columns)
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(str(row[column]).replace("|", "/") for column in columns) + " |")
    return "\n".join(lines)


def write_report(comparison: pd.DataFrame) -> None:
    matches = int((comparison["zone_match"] & comparison["state_match"]).sum())
    lines = [
        "# Manual HTF Order-Block Diagnostics",
        "",
        "Read-only 15m order-block diagnostic. No trade-selection changes.",
        "",
        "Definition: opposite-color origin candle followed by a same-direction displacement candle that closes beyond the origin wick with body/range >= 0.55. The OB zone uses the origin candle body.",
        "",
        f"Exact manual zone/state matches: {matches}/{len(comparison)}.",
        "",
        markdown_table(comparison),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
