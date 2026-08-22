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
from backtest.strategy import lifecycles_to_frame, run_backtest, trades_to_frame


CASES_FILE = ROOT / "calibration_examples" / "manual_regression_cases.csv"
RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_regression_harness"

EXTRA_PROFILES = {
    "challenge_core_v3_spx_strong_swing",
    "challenge_core_v3_spx_session_only",
    "challenge_core_v3_spx_rejection_close",
    "challenge_core_v3_spx_session_rejection",
    "challenge_core_v3_spx_opening_reversal",
    "challenge_core_v3_spx_session_opening_reversal",
    "challenge_core_v3_spx_session_opening_fvg12",
    "challenge_core_v3_spx_session_opening_late_entry_fvg12",
    "challenge_core_v3_spx_body_fvg_midpoint",
    "challenge_core_v3_spx_body_fvg_ote705",
    "challenge_core_v3_spx_body_fvg_window12",
    "challenge_core_v3_spx_body_fvg_start_followup",
    "challenge_core_v3_spx_session_opening_entry_rescan",
    "challenge_core_v3_spx_session_opening_late_followup",
}


def main() -> None:
    reset_report_dir()
    cases = pd.read_csv(CASES_FILE, keep_default_na=False)
    actual_by_profile = run_profiles(cases)
    comparison = build_comparison(cases, actual_by_profile)
    summary = build_summary(comparison)

    cases.to_csv(REPORT_DIR / "cases.csv", index=False)
    concat_actual(actual_by_profile, "trades").to_csv(REPORT_DIR / "actual_trades.csv", index=False)
    concat_actual(actual_by_profile, "lifecycles").to_csv(REPORT_DIR / "actual_lifecycles.csv", index=False)
    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    write_report(summary, comparison)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def run_profiles(cases: pd.DataFrame) -> dict[tuple[str, str, str], dict[str, pd.DataFrame]]:
    outputs: dict[tuple[str, str, str], dict[str, pd.DataFrame]] = {}
    profile_names = sorted(set(cases["profile"]) | EXTRA_PROFILES)
    symbols = cases[["symbol", "timeframe"]].drop_duplicates()
    for profile in profile_names:
        for symbol, timeframe in symbols.itertuples(index=False):
            group = cases[(cases["symbol"] == symbol) & (cases["timeframe"] == timeframe)]
            if group.empty:
                continue
            if not profile_supported_for_symbol(profile, symbol, timeframe):
                continue
            execution_timeframe = profile_execution_timeframe(profile, timeframe)
            frame = load_ohlcv(base_report.filter_paths(RAW_DIR, symbol, execution_timeframe)).frame
            selected = select_case_windows(frame, sorted(set(group["date"])))
            config = build_profile_config(profile, symbol, timeframe)
            result = run_backtest(selected, config)
            trades = trades_to_frame(result.trades)
            lifecycles = lifecycles_to_frame(result.lifecycles)
            if not trades.empty:
                trades.insert(0, "profile", profile)
            if not lifecycles.empty:
                lifecycles.insert(0, "profile", profile)
            outputs[(profile, symbol, timeframe)] = {
                "trades": trades,
                "lifecycles": lifecycles,
            }
    return outputs


def concat_actual(
    actual_by_profile: dict[tuple[str, str, str], dict[str, pd.DataFrame]],
    frame_name: str,
) -> pd.DataFrame:
    frames = [output[frame_name] for output in actual_by_profile.values() if not output[frame_name].empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def profile_supported_for_symbol(profile: str, symbol: str, timeframe: str) -> bool:
    if profile.startswith("challenge_core_v3_spx_"):
        return symbol == "DUKASCOPY_USA500IDXUSD" and timeframe == "5m"
    if profile in {"challenge_core_v2_nq_5m", "challenge_core_v2_nq_5m_lunch_window"}:
        return symbol == "DUKASCOPY_USATECHIDXUSD" and timeframe == "3m"
    if profile == "challenge_core_v2_nq_lunch_window":
        return symbol == "DUKASCOPY_USATECHIDXUSD" and timeframe == "3m"
    return True


def profile_execution_timeframe(profile: str, case_timeframe: str) -> str:
    return "5m" if profile in {"challenge_core_v2_nq_5m", "challenge_core_v2_nq_5m_lunch_window"} else case_timeframe


def select_case_windows(frame: pd.DataFrame, dates: list[str]) -> pd.DataFrame:
    windows = []
    for date_text in dates:
        start = pd.Timestamp(date_text).tz_localize("America/New_York") - pd.Timedelta(days=2)
        end = pd.Timestamp(date_text).tz_localize("America/New_York") + pd.Timedelta(days=1)
        windows.append(frame[(frame["time"] >= start) & (frame["time"] < end)])
    selected = pd.concat(windows, ignore_index=True) if windows else frame.iloc[0:0].copy()
    return selected.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)


def build_profile_config(profile: str, symbol: str, timeframe: str):
    if profile in {
        "challenge_core_v2",
        "challenge_core_v2_htf_requalification",
        "challenge_core_v2_nq_5m",
        "challenge_core_v2_nq_5m_lunch_window",
        "challenge_core_v2_nq_lunch_window",
        "challenge_core_v3_spx_strong_swing",
        "challenge_core_v3_spx_session_only",
        "challenge_core_v3_spx_rejection_close",
        "challenge_core_v3_spx_session_rejection",
        "challenge_core_v3_spx_opening_reversal",
        "challenge_core_v3_spx_session_opening_reversal",
        "challenge_core_v3_spx_session_opening_fvg12",
        "challenge_core_v3_spx_session_opening_late_entry_fvg12",
        "challenge_core_v3_spx_body_fvg_midpoint",
        "challenge_core_v3_spx_body_fvg_ote705",
        "challenge_core_v3_spx_body_fvg_window12",
        "challenge_core_v3_spx_body_fvg_start_followup",
        "challenge_core_v3_spx_session_opening_entry_rescan",
        "challenge_core_v3_spx_session_opening_late_followup",
    }:
        loaded = filters.load_data()
        thresholds = filters.build_thresholds(loaded)
        threshold_lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}
        for leg in filters.build_leg_specs():
            timeframe_matches = leg.candidate.timeframe == timeframe or (
                profile in {"challenge_core_v2_nq_5m", "challenge_core_v2_nq_5m_lunch_window"}
                and symbol == "DUKASCOPY_USATECHIDXUSD"
                and leg.candidate.timeframe == "3m"
            )
            if leg.system == "phase_selected" and leg.candidate.symbol == symbol and timeframe_matches:
                variant = "first30_q60" if leg.leg_key == "nq" else "baseline"
                spec = filters.VariantSpec(
                    variant,
                    variant,
                    "engine",
                    first30_quantile=manual_v2.variant_quantile(variant),
                )
                config = manual_v2.manual_v2_config(filters.build_variant_config(leg, spec, threshold_lookup))
                if profile == "challenge_core_v3_spx_strong_swing":
                    return replace(config, swing_liquidity_mode="strong_only", strong_swing_min_touches=3)
                if profile == "challenge_core_v3_spx_session_only":
                    return replace(config, session_liquidity_only=True)
                if profile == "challenge_core_v3_spx_rejection_close":
                    return replace(config, require_sweep_rejection_close=True)
                if profile == "challenge_core_v3_spx_session_rejection":
                    return replace(config, session_liquidity_only=True, require_sweep_rejection_close=True)
                if profile == "challenge_core_v3_spx_opening_reversal":
                    return replace(config, opening_premarket_direction_mode="reversal_only")
                if profile == "challenge_core_v3_spx_session_opening_reversal":
                    return replace(config, session_liquidity_only=True, opening_premarket_direction_mode="reversal_only")
                if profile == "challenge_core_v3_spx_session_opening_fvg12":
                    return replace(
                        config,
                        session_liquidity_only=True,
                        opening_premarket_direction_mode="reversal_only",
                        fvg_window_candles=12,
                    )
                if profile == "challenge_core_v3_spx_session_opening_late_entry_fvg12":
                    return replace(
                        config,
                        session_liquidity_only=True,
                        opening_premarket_direction_mode="reversal_only",
                        fvg_window_candles=12,
                        latest_entry_time=None,
                    )
                if profile == "challenge_core_v3_spx_body_fvg_midpoint":
                    return replace(
                        config,
                        session_liquidity_only=True,
                        opening_premarket_direction_mode="reversal_only",
                        setup_type_filter="body_fvg",
                    )
                if profile == "challenge_core_v3_spx_body_fvg_ote705":
                    return replace(
                        config,
                        session_liquidity_only=True,
                        opening_premarket_direction_mode="reversal_only",
                        setup_type_filter="body_fvg",
                        fvg_entry_mode="ote_705",
                    )
                if profile == "challenge_core_v3_spx_body_fvg_window12":
                    return replace(
                        config,
                        session_liquidity_only=True,
                        opening_premarket_direction_mode="reversal_only",
                        setup_type_filter="body_fvg",
                        fvg_window_candles=12,
                    )
                if profile == "challenge_core_v3_spx_body_fvg_start_followup":
                    return replace(
                        config,
                        session_liquidity_only=True,
                        opening_premarket_direction_mode="reversal_only",
                        setup_type_filter="body_fvg",
                        fvg_entry_mode="start",
                        active_trade_block_mode="entry",
                        trade_window_end="11:30",
                        latest_entry_time=None,
                    )
                if profile == "challenge_core_v3_spx_session_opening_entry_rescan":
                    return replace(
                        config,
                        session_liquidity_only=True,
                        opening_premarket_direction_mode="reversal_only",
                        active_trade_block_mode="entry",
                    )
                if profile == "challenge_core_v3_spx_session_opening_late_followup":
                    return replace(
                        config,
                        session_liquidity_only=True,
                        opening_premarket_direction_mode="reversal_only",
                        active_trade_block_mode="entry",
                        trade_window_end="11:30",
                        latest_entry_time=None,
                    )
                if profile == "challenge_core_v2_htf_requalification":
                    return replace(config, htf_body_close_requalification="fvg_ob")
                if profile == "challenge_core_v2_nq_5m":
                    return replace(config, timeframe="5m")
                if profile == "challenge_core_v2_nq_5m_lunch_window":
                    return replace(config, timeframe="5m", trade_window_end="12:00")
                if profile == "challenge_core_v2_nq_lunch_window":
                    return replace(config, trade_window_end="12:00")
                return config
        raise SystemExit(f"No CHALLENGE_CORE_V2 leg found for {symbol} {timeframe}")

    if profile == "spx_corrected_legacy":
        candidate = next(item for item in base_report.CANDIDATES if item.slug == "spx_fvg_opposite_edge_3r")
        return base_report.build_config(candidate)

    raise SystemExit(f"Unsupported manual regression profile: {profile}")


def build_comparison(
    cases: pd.DataFrame,
    actual_by_profile: dict[tuple[str, str, str], dict[str, pd.DataFrame]],
) -> pd.DataFrame:
    rows = []
    for key, output in actual_by_profile.items():
        profile, symbol, timeframe = key
        matching_cases = cases[(cases["symbol"] == symbol) & (cases["timeframe"] == timeframe)]
        for _, case in matching_cases.iterrows():
            day_trades = trades_for_day(output["trades"], case["date"])
            day_lifecycles = lifecycles_for_day(output["lifecycles"], case["date"])
            matched = match_trade(case, day_trades)
            matched_lifecycle = match_lifecycle(case, day_lifecycles)
            rows.append(compare_case(case, day_trades, matched, day_lifecycles, matched_lifecycle, profile))
    return pd.DataFrame(rows)


def trades_for_day(trades: pd.DataFrame, date_text: str) -> pd.DataFrame:
    if trades.empty:
        return trades
    day = trades[trades["date"] == date_text].copy()
    if day.empty:
        return day
    day["entry_time_dt"] = pd.to_datetime(day["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    return day.sort_values("entry_time_dt").reset_index(drop=True)


def match_trade(case: pd.Series, day_trades: pd.DataFrame) -> pd.Series | None:
    if day_trades.empty:
        return None
    order = int(case["expected_order"] or 1)
    if len(day_trades) >= order:
        return day_trades.iloc[order - 1]
    return None


def lifecycles_for_day(lifecycles: pd.DataFrame, date_text: str) -> pd.DataFrame:
    if lifecycles.empty:
        return lifecycles
    day = lifecycles[lifecycles["date"] == date_text].copy()
    if day.empty:
        return day
    day["sort_time"] = pd.to_datetime(day["fvg_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    return day.sort_values("sort_time").reset_index(drop=True)


def match_lifecycle(case: pd.Series, day_lifecycles: pd.DataFrame) -> pd.Series | None:
    if day_lifecycles.empty:
        return None
    order = int(case["expected_order"] or 1)
    if len(day_lifecycles) >= order:
        return day_lifecycles.iloc[order - 1]
    return None


def compare_case(
    case: pd.Series,
    day_trades: pd.DataFrame,
    matched: pd.Series | None,
    day_lifecycles: pd.DataFrame,
    matched_lifecycle: pd.Series | None,
    profile: str,
) -> dict[str, object]:
    if lifecycle_columns_present(case):
        return compare_lifecycle_case(case, day_trades, day_lifecycles, matched_lifecycle, profile)

    expected = case["expected_decision"]
    actual_decision = "TAKE" if not day_trades.empty else "SKIP"
    matched_direction = "" if matched is None else str(matched["direction"])
    direction_ok = (
        case["expected_direction"] == ""
        or matched is not None
        and matched_direction == case["expected_direction"]
    )
    if expected == "WATCH":
        status = "WATCH"
    elif expected == "SKIP":
        status = "PASS" if day_trades.empty else "FAIL"
    elif expected == "TAKE":
        status = "PASS" if matched is not None and direction_ok else "FAIL"
    else:
        status = "WATCH"

    return {
        "case_id": case["case_id"],
        "profile": profile,
        "symbol": case["symbol"],
        "timeframe": case["timeframe"],
        "date": case["date"],
        "expected_decision": expected,
        "expected_direction": case["expected_direction"],
        "expected_order": case["expected_order"],
        "actual_day_decision": actual_decision,
        "actual_day_trade_count": len(day_trades),
        "matched_direction": matched_direction,
        "matched_liquidity": "" if matched is None else matched["liquidity_context"],
        "matched_sweep_time": "" if matched is None else matched["sweep_time"],
        "matched_cisd_time": "" if matched is None else matched["cisd_time"],
        "matched_fvg_time": "" if matched is None else matched["fvg_time"],
        "matched_entry_time": "" if matched is None else matched["entry_time"],
        "matched_result": "" if matched is None else matched["result"],
        "matched_notes": "" if matched is None else matched["notes"],
        "status": status,
        "manual_classification": case["manual_classification"],
        "issue_type": case["issue_type"],
        "manual_notes": case["notes"],
    }


def lifecycle_columns_present(case: pd.Series) -> bool:
    return all(
        column in case.index
        for column in [
            "expected_setup",
            "expected_order_state",
            "expected_final",
            "expected_outcome",
            "expected_terminal_reason",
        ]
    )


def compare_lifecycle_case(
    case: pd.Series,
    day_trades: pd.DataFrame,
    day_lifecycles: pd.DataFrame,
    matched: pd.Series | None,
    profile: str,
) -> dict[str, object]:
    actual = {
        "setup": "INVALID",
        "order": "NOT_PLACED",
        "final": "SKIP",
        "direction": "",
        "outcome": "NO_TRADE",
        "terminal": "UNOBSERVED",
    }
    if matched is not None:
        actual = {
            "setup": str(matched["setup_state"]).upper(),
            "order": str(matched["order_state"]).upper(),
            "final": str(matched["final_decision"]).upper(),
            "direction": str(matched["direction"]).lower(),
            "outcome": str(matched["outcome"]).upper(),
            "terminal": str(matched["terminal_reason"]).upper(),
        }

    expected_direction = normalize_direction(case["expected_direction"])
    checks = {
        "setup_match": actual["setup"] == str(case["expected_setup"]).upper(),
        "order_match": actual["order"] == str(case["expected_order_state"]).upper(),
        "final_match": actual["final"] == str(case["expected_final"]).upper(),
        "direction_match": actual["direction"] == expected_direction,
        "outcome_match": actual["outcome"] == str(case["expected_outcome"]).upper(),
        "terminal_match": (
            None
            if actual["terminal"] == "UNOBSERVED"
            else terminal_reasons_match(str(case["expected_terminal_reason"]), actual["terminal"])
        ),
    }
    decision_exact = all(checks[key] for key in ["setup_match", "order_match", "final_match", "direction_match"])
    lifecycle_exact = decision_exact and checks["terminal_match"] is not False
    return {
        "case_id": case["case_id"],
        "profile": profile,
        "symbol": case["symbol"],
        "timeframe": case["timeframe"],
        "date": case["date"],
        "expected_setup": case["expected_setup"],
        "expected_order_state": case["expected_order_state"],
        "expected_final": case["expected_final"],
        "expected_direction": case["expected_direction"],
        "expected_outcome": case["expected_outcome"],
        "expected_terminal_reason": case["expected_terminal_reason"],
        "actual_setup": actual["setup"],
        "actual_order_state": actual["order"],
        "actual_final": actual["final"],
        "actual_direction": actual["direction"],
        "actual_outcome": actual["outcome"],
        "actual_terminal_reason": actual["terminal"],
        "actual_day_trade_count": len(day_trades),
        "actual_day_lifecycle_count": len(day_lifecycles),
        **checks,
        "decision_exact": decision_exact,
        "lifecycle_exact": lifecycle_exact,
        "status": "PASS" if decision_exact else "FAIL",
        "matched_liquidity": "" if matched is None else matched["liquidity_context"],
        "matched_sweep_time": "" if matched is None else matched["sweep_time"],
        "matched_cisd_time": "" if matched is None else matched["cisd_time"],
        "matched_fvg_time": "" if matched is None else matched["fvg_time"],
        "matched_entry_time": "" if matched is None else matched["entry_time"],
        "matched_notes": "" if matched is None else matched["setup_kind"],
        "manual_classification": case.get("manual_classification", ""),
        "issue_type": case.get("issue_type", ""),
        "manual_notes": case.get("notes", ""),
    }


def normalize_direction(value: object) -> str:
    text = str(value).strip().lower()
    return "" if text in {"", "yok", "none", "not_applicable"} else text


def terminal_reasons_match(expected: str, actual: str) -> bool:
    expected_key = expected.strip().upper()
    actual_key = actual.strip().upper()
    aliases = {
        "TARGET_BEFORE_FILL": "CANCELLED_TARGET_BEFORE_FILL",
        "LUNCH_END_NO_FILL": "CANCELLED_TRADE_WINDOW_END",
        "SESSION_END_NO_FILL": "CANCELLED_TRADE_WINDOW_END",
        "TP": "FILLED_TP",
        "SL": "FILLED_SL",
    }
    return aliases.get(expected_key, expected_key) == aliases.get(actual_key, actual_key)


def build_summary(comparison: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for profile, profile_group in comparison.groupby("profile"):
        rows.append(summary_row(profile_group, profile, "all"))
    for (profile, issue_type), group in comparison.groupby(["profile", "issue_type"]):
        rows.append(summary_row(group, profile, issue_type))
    return pd.DataFrame(rows)


def summary_row(group: pd.DataFrame, profile: str, bucket: str) -> dict[str, object]:
    scored_group = group[group["status"] != "WATCH"]
    row = {
        "profile": profile,
        "bucket": bucket,
        "cases": len(group),
        "scored_cases": len(scored_group),
        "passed": int((scored_group["status"] == "PASS").sum()),
        "failed": int((scored_group["status"] == "FAIL").sum()),
        "watch": int((group["status"] == "WATCH").sum()),
        "pass_rate": round(float((scored_group["status"] == "PASS").mean() * 100), 2) if len(scored_group) else 0.0,
    }
    for column in [
        "setup_match",
        "order_match",
        "final_match",
        "direction_match",
        "outcome_match",
        "terminal_match",
        "decision_exact",
        "lifecycle_exact",
    ]:
        if column in scored_group:
            values = pd.to_numeric(scored_group[column], errors="coerce").dropna()
            metric = column.replace("_match", "_accuracy").replace("_exact", "_exact_rate")
            row[metric] = round(float(values.mean() * 100), 2) if len(values) else ""
    return row


def write_report(summary: pd.DataFrame, comparison: pd.DataFrame) -> None:
    lines = [
        "# Manual Regression Harness",
        "",
        "Scope: compare selected manual chart-review cases against the current engine profile.",
        "",
        "This is not a profitability test. It checks whether the engine takes/skips the same scenarios as manual review.",
        "",
        "## Summary",
        "",
        base_report.markdown_table(summary),
        "",
        "## Comparison",
        "",
        base_report.markdown_table(comparison),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
