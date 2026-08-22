from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_corrected_engine_3month_report as base_report
from backtest.data_loader import load_ohlcv
from run_manual_controlling_array_diagnostics import LABELS_FILE, build_pool, decision_cutoff


RAW_DIR = ROOT / "data" / "raw"
REGRESSION_DIR = ROOT / "outputs" / "reports" / "manual_feedback_regression_v2"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_array_reaction_diagnostics"
HORIZONS = [15, 30, 45]


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    labels = pd.read_csv(LABELS_FILE, keep_default_na=False)
    pool = build_pool()
    trades = pd.read_csv(REGRESSION_DIR / "actual_trades.csv", keep_default_na=False)
    body_trades = trades[trades["profile"] == "challenge_core_v3_spx_body_fvg_start_followup"]
    frame = load_ohlcv(base_report.filter_paths(RAW_DIR, "DUKASCOPY_USA500IDXUSD", "5m")).frame

    detail_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    for horizon in HORIZONS:
        matched = 0
        for _, label in labels.iterrows():
            cutoff = decision_cutoff(label["date"], body_trades)
            candidates = eligible_opening_candidates(pool, label["date"], cutoff)
            scored = score_candidates(candidates, frame, horizon, label)
            selected = scored.sort_values(["reaction_score", "available_time"], ascending=[False, False]).iloc[0] if not scored.empty else None
            is_match = selected is not None and bool(selected["is_manual_zone"])
            matched += int(is_match)
            manual_row = scored[scored["is_manual_zone"]].sort_values("reaction_score", ascending=False)
            manual_score = "" if manual_row.empty else round(float(manual_row.iloc[0]["reaction_score"]), 3)
            detail_rows.append({
                "horizon_minutes": horizon,
                "case_id": label["case_id"],
                "date": label["date"],
                "candidate_count": len(scored),
                "selected_manual": is_match,
                "manual_reaction_score": manual_score,
                "selected_type": "" if selected is None else selected["array_type"],
                "selected_htf": "" if selected is None else selected["htf"],
                "selected_direction": "" if selected is None else selected["direction"],
                "selected_zone": "" if selected is None else f"{selected['lower']:.2f}-{selected['upper']:.2f}",
                "selected_score": "" if selected is None else round(float(selected["reaction_score"]), 3),
            })
        summary_rows.append({
            "selector": f"opening_reaction_{horizon}m",
            "matches": matched,
            "cases": len(labels),
            "match_rate": round(matched / len(labels) * 100, 2),
        })

    detail = pd.DataFrame(detail_rows)
    summary = pd.DataFrame(summary_rows).sort_values("matches", ascending=False)
    detail.to_csv(REPORT_DIR / "reaction_detail.csv", index=False)
    summary.to_csv(REPORT_DIR / "reaction_summary.csv", index=False)
    write_report(summary, detail)
    print(f"Wrote: {REPORT_DIR}")


def eligible_opening_candidates(pool: pd.DataFrame, date_text: str, cutoff: pd.Timestamp) -> pd.DataFrame:
    day = pool[(pool["date"] == date_text) & (pool["available_time"] <= cutoff)].copy()
    day = day[day["first_touch_time"] != ""]
    touch = pd.to_datetime(day["first_touch_time"], utc=True)
    local = touch.dt.tz_convert("America/New_York")
    opening = ((local.dt.hour == 9) & (local.dt.minute >= 30)) | ((local.dt.hour == 10) & (local.dt.minute == 0))
    return day[opening].copy()


def score_candidates(candidates: pd.DataFrame, frame: pd.DataFrame, horizon: int, label: pd.Series) -> pd.DataFrame:
    scored = candidates.copy()
    scores: list[float] = []
    manual_flags: list[bool] = []
    for _, zone in scored.iterrows():
        touch = pd.Timestamp(zone["first_touch_time"])
        end = touch + pd.Timedelta(minutes=horizon)
        window = frame[(frame["time"] >= touch) & (frame["time"] < end)]
        context = frame[(frame["time"] >= touch - pd.Timedelta(hours=1)) & (frame["time"] < touch)]
        median_range = float((context["high"] - context["low"]).median()) if not context.empty else 1.0
        median_range = median_range if median_range > 0 else 1.0
        if window.empty:
            reaction = 0.0
        elif zone["direction"] == "bullish":
            reaction = max(0.0, float(window["high"].max()) - float(zone["upper"]))
        else:
            reaction = max(0.0, float(zone["lower"]) - float(window["low"].min()))
        scores.append(reaction / median_range)
        center = (float(zone["lower"]) + float(zone["upper"])) / 2
        manual_center = (float(label["manual_lower"]) + float(label["manual_upper"])) / 2
        manual_flags.append(
            zone["array_type"] == label["array_type"]
            and zone["htf"] == label["htf"]
            and zone["direction"] == label["direction"]
            and abs(center - manual_center) <= 1.5
        )
    scored["reaction_score"] = scores
    scored["is_manual_zone"] = manual_flags
    return scored


def markdown_table(frame: pd.DataFrame) -> str:
    columns = list(frame.columns)
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(str(row[column]).replace("|", "/") for column in columns) + " |")
    return "\n".join(lines)


def write_report(summary: pd.DataFrame, detail: pd.DataFrame) -> None:
    lines = [
        "# Manual Array Reaction Diagnostics",
        "",
        "Read-only test of whether post-touch directional displacement identifies the manually controlling opening array.",
        "",
        "Reaction score = favorable excursion after first touch / median 5m range during the prior hour.",
        "",
        "## Summary",
        "",
        markdown_table(summary),
        "",
        "## Detail",
        "",
        markdown_table(detail),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
