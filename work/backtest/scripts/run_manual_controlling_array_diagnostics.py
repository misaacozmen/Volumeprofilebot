from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
LABELS_FILE = ROOT / "calibration_examples" / "manual_controlling_array_labels_v1.csv"
HTF_DIR = ROOT / "outputs" / "reports" / "manual_htf_pd_array_diagnostics"
OB_DIR = ROOT / "outputs" / "reports" / "manual_htf_orderblock_diagnostics"
REGRESSION_DIR = ROOT / "outputs" / "reports" / "manual_feedback_regression_v2"
CASE_SUMMARY_FILE = HTF_DIR / "case_summary.csv"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_controlling_array_diagnostics"


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    labels = pd.read_csv(LABELS_FILE, keep_default_na=False)
    pool = build_pool()
    trades = pd.read_csv(REGRESSION_DIR / "actual_trades.csv", keep_default_na=False)
    case_summary = pd.read_csv(CASE_SUMMARY_FILE, keep_default_na=False)
    body_trades = trades[trades["profile"] == "challenge_core_v3_spx_body_fvg_start_followup"].copy()

    rows: list[dict[str, object]] = []
    for _, label in labels.iterrows():
        cutoff = decision_cutoff(label["date"], body_trades)
        candidates = pool[(pool["date"] == label["date"]) & (pool["available_time"] <= cutoff)].copy()
        candidates = candidates[candidates["first_touch_time"] != ""]
        candidates = candidates[pd.to_datetime(candidates["first_touch_time"], utc=True) <= cutoff.tz_convert("UTC")]
        info = case_summary[case_summary["date"] == label["date"]].iloc[0]
        candidates = add_features(candidates, label, info)
        manual = candidates[candidates["is_manual_zone"]]
        rows.append({
            "case_id": label["case_id"],
            "date": label["date"],
            "candidate_count": len(candidates),
            "manual_candidate_found": not manual.empty,
            "newest": selected_manual(candidates, ["available_time"], [False]),
            "oracle_exact_zone": selected_manual(candidates, ["manual_center_distance"], [True]),
            "closest_to_open": selected_manual(candidates, ["distance_from_open"], [True]),
            "closest_to_va": selected_manual(candidates, ["distance_to_va"], [True]),
            "largest": selected_manual(candidates, ["size"], [False]),
            "opening_newest": selected_manual(candidates[candidates["opening_touch"]], ["available_time"], [False]),
            "opening_closest_open": selected_manual(candidates[candidates["opening_touch"]], ["distance_from_open"], [True]),
            "opening_closest_va": selected_manual(candidates[candidates["opening_touch"]], ["distance_to_va"], [True]),
            "opening_30m_newest": selected_manual(candidates[candidates["opening_touch"] & (candidates["htf"] == "30m")], ["available_time"], [False]),
        })

    comparison = pd.DataFrame(rows)
    rule_columns = [column for column in comparison.columns if column not in {"case_id", "date", "candidate_count", "manual_candidate_found", "oracle_exact_zone"}]
    summary = pd.DataFrame([
        {"selector": column, "matches": int(comparison[column].sum()), "cases": len(comparison), "match_rate": round(float(comparison[column].mean() * 100), 2)}
        for column in rule_columns
    ]).sort_values(["matches", "selector"], ascending=[False, True])
    comparison.to_csv(REPORT_DIR / "selector_comparison.csv", index=False)
    summary.to_csv(REPORT_DIR / "selector_summary.csv", index=False)
    write_report(summary, comparison)
    print(f"Wrote: {REPORT_DIR}")


def build_pool() -> pd.DataFrame:
    fvg = pd.read_csv(HTF_DIR / "htf_zones.csv", keep_default_na=False)
    fvg_pool = pd.DataFrame({
        "date": fvg["date"],
        "array_type": "fvg",
        "htf": fvg["htf"],
        "direction": fvg["direction"],
        "lower": fvg["lower"].astype(float),
        "upper": fvg["upper"].astype(float),
        "available_time": pd.to_datetime(fvg["formed_time"], utc=True) + fvg["htf"].map({"15m": pd.Timedelta(minutes=15), "30m": pd.Timedelta(minutes=30)}),
        "first_touch_time": fvg["first_touch_time"],
        "distance_from_open": fvg["distance_from_open"].astype(float),
        "relevant_to_va": fvg["relevant_to_va"].astype(str).str.lower().eq("true"),
    })
    ob = pd.read_csv(OB_DIR / "htf_order_blocks.csv", keep_default_na=False)
    ob_pool = pd.DataFrame({
        "date": ob["date"],
        "array_type": "order_block",
        "htf": ob["htf"],
        "direction": ob["direction"],
        "lower": ob["lower"].astype(float),
        "upper": ob["upper"].astype(float),
        "available_time": pd.to_datetime(ob["confirmed_time"], utc=True),
        "first_touch_time": ob["first_touch_time"],
        "distance_from_open": 999.0,
        "relevant_to_va": False,
    })
    return pd.concat([fvg_pool, ob_pool], ignore_index=True)


def decision_cutoff(date_text: str, trades: pd.DataFrame) -> pd.Timestamp:
    day = trades[trades["date"] == date_text]
    if not day.empty:
        return pd.to_datetime(day["entry_time"], utc=True).min()
    local = pd.Timestamp(date_text).tz_localize("America/New_York") + pd.Timedelta(hours=10, minutes=30)
    return local.tz_convert("UTC")


def add_features(candidates: pd.DataFrame, label: pd.Series, info: pd.Series) -> pd.DataFrame:
    candidates = candidates.copy()
    manual_center = (float(label["manual_lower"]) + float(label["manual_upper"])) / 2
    candidates["center"] = (candidates["lower"] + candidates["upper"]) / 2
    candidates["size"] = candidates["upper"] - candidates["lower"]
    candidates["manual_center_distance"] = abs(candidates["center"] - manual_center)
    open_price = float(info["open_price"])
    vah = float(info["vah"])
    val = float(info["val"])
    candidates["distance_from_open"] = candidates.apply(
        lambda row: price_to_zone_distance(open_price, float(row["lower"]), float(row["upper"])), axis=1
    )
    candidates["distance_to_va"] = candidates.apply(
        lambda row: min(
            price_to_zone_distance(vah, float(row["lower"]), float(row["upper"])),
            price_to_zone_distance(val, float(row["lower"]), float(row["upper"])),
        ),
        axis=1,
    )
    candidates["is_manual_zone"] = (
        (candidates["array_type"] == label["array_type"])
        & (candidates["htf"] == label["htf"])
        & (candidates["direction"] == label["direction"])
        & (candidates["upper"] >= float(label["manual_lower"]) - 1.0)
        & (candidates["lower"] <= float(label["manual_upper"]) + 1.0)
        & (candidates["manual_center_distance"] <= 1.5)
    )
    touch = pd.to_datetime(candidates["first_touch_time"], utc=True)
    local_touch = touch.dt.tz_convert("America/New_York")
    candidates["opening_touch"] = (
        (local_touch.dt.hour == 9) & (local_touch.dt.minute >= 30)
        | (local_touch.dt.hour == 10) & (local_touch.dt.minute == 0)
    )
    return candidates


def price_to_zone_distance(price: float, lower: float, upper: float) -> float:
    if lower <= price <= upper:
        return 0.0
    return min(abs(price - lower), abs(price - upper))


def selected_manual(frame: pd.DataFrame, columns: list[str], ascending: list[bool]) -> bool:
    if frame.empty:
        return False
    selected = frame.sort_values(columns, ascending=ascending).iloc[0]
    return bool(selected["is_manual_zone"])


def markdown_table(frame: pd.DataFrame) -> str:
    columns = list(frame.columns)
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(str(row[column]) for column in columns) + " |")
    return "\n".join(lines)


def write_report(summary: pd.DataFrame, comparison: pd.DataFrame) -> None:
    lines = [
        "# Manual Controlling-Array Selector Diagnostics",
        "",
        "Read-only selector analysis over the 9 exact manually labelled controlling arrays.",
        "The oracle_exact_zone column only verifies candidate coverage using the manual box and is excluded from selector rankings.",
        "",
        "## Selector summary",
        "",
        markdown_table(summary),
        "",
        "## Case comparison",
        "",
        markdown_table(comparison),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
