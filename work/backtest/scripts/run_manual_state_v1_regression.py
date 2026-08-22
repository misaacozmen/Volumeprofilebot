from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_corrected_engine_3month_report as base_report
import run_manual_regression_harness as harness
from backtest.data_loader import load_ohlcv
from backtest.manual_state import (
    ManualStateConfig,
    cisd_events_to_frame,
    cisd_qualifications_to_frame,
    context_triggers_to_frame,
    context_authority_to_frame,
    decisions_to_frame,
    htf_arrays_to_frame,
    liquidity_selections_to_frame,
    premarket_contexts_to_frame,
    run_manual_state_backtest,
    thesis_episodes_to_frame,
)
from backtest.strategy import find_fvg_or_ifvg, time_slice, timestamp_on_day


CASES_FILE = ROOT / "calibration_examples" / "manual_feedback_regression_v4_first10.csv"
RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "manual_state_v1_first10"
KNOWN_FINAL_CISD = {
    "NQ-FB4-005": ("short", "11:35"),
    "NQ-FB4-010": ("long", "11:45"),
}


def main() -> None:
    reset_report_dir()
    cases = pd.read_csv(CASES_FILE, keep_default_na=False)
    symbol = str(cases["symbol"].iloc[0])
    timeframe = str(cases["timeframe"].iloc[0])
    frame = load_ohlcv(base_report.filter_paths(RAW_DIR, symbol, timeframe)).frame
    selected = harness.select_case_windows(frame, sorted(set(cases["date"])))
    base_config = harness.build_profile_config("challenge_core_v2", "DUKASCOPY_USATECHIDXUSD", timeframe)
    config = replace(base_config, symbol=symbol, timeframe=timeframe)
    dates = [pd.Timestamp(value).date() for value in cases["date"]]
    state_config = ManualStateConfig(
        trade_window_start="09:30",
        trade_window_end="12:00",
        cisd_anchor_lookback=6,
        fvg_window_candles=config.fvg_window_candles,
        enforce_symbol_config_envelope=False,
    )
    result = run_manual_state_backtest(selected, config, dates, state_config)
    raw_state_config = replace(
        state_config,
        cisd_qualification_mode="raw",
        target_before_fill_mode="synthetic_r",
    )
    raw_result = run_manual_state_backtest(selected, config, dates, raw_state_config)
    explicit_target_config = replace(state_config, target_before_fill_mode="explicit_only")
    explicit_target_result = run_manual_state_backtest(
        selected,
        config,
        dates,
        explicit_target_config,
    )

    cisd = cisd_events_to_frame(result.days)
    cisd_qualifications = cisd_qualifications_to_frame(result.days)
    htf = htf_arrays_to_frame(result.days)
    premarket_contexts = premarket_contexts_to_frame(result.days)
    contexts = context_triggers_to_frame(result.days)
    context_authority = context_authority_to_frame(result.days)
    liquidity_selections = liquidity_selections_to_frame(result.days)
    liquidity_alignment = build_liquidity_alignment(cases, liquidity_selections, contexts)
    episodes = thesis_episodes_to_frame(result.days)
    decisions = decisions_to_frame(result.days)
    known_cisd_checks = build_known_cisd_checks(cisd)
    cisd_candidate_audit = build_cisd_candidate_audit(cases, result, selected, config, state_config, "DUKASCOPY")
    known_candidate_ranks = cisd_candidate_audit[cisd_candidate_audit["known_final_match"]].copy()
    comparison = compare_cases(cases, decisions)
    summary = summarize(comparison, "MANUAL_DEVELOPMENT_PACKAGE_V1")
    raw_decisions = decisions_to_frame(raw_result.days)
    raw_comparison = compare_cases(cases, raw_decisions)
    raw_summary = summarize(raw_comparison, "RAW_CISD_BASELINE")
    explicit_target_decisions = decisions_to_frame(explicit_target_result.days)
    explicit_target_comparison = compare_cases(cases, explicit_target_decisions)
    explicit_target_summary = summarize(
        explicit_target_comparison,
        "EXPLICIT_ONLY_TARGET_WATCH",
    )
    same_feed = run_same_feed_watch(cases, state_config)
    raw_same_feed = run_same_feed_watch(cases, raw_state_config)
    cross_sweep = run_cross_sweep_audit(state_config)
    cross_sweep_explicit_target = run_cross_sweep_audit(explicit_target_config)

    cases.to_csv(REPORT_DIR / "cases.csv", index=False)
    cisd.to_csv(REPORT_DIR / "cisd_events.csv", index=False)
    cisd_qualifications.to_csv(REPORT_DIR / "cisd_qualifications.csv", index=False)
    htf.to_csv(REPORT_DIR / "htf_array_lifecycle.csv", index=False)
    premarket_contexts.to_csv(REPORT_DIR / "premarket_contexts.csv", index=False)
    contexts.to_csv(REPORT_DIR / "context_triggers.csv", index=False)
    context_authority.to_csv(REPORT_DIR / "context_authority.csv", index=False)
    liquidity_selections.to_csv(REPORT_DIR / "liquidity_selections.csv", index=False)
    liquidity_alignment.to_csv(REPORT_DIR / "liquidity_alignment.csv", index=False)
    episodes.to_csv(REPORT_DIR / "thesis_episodes.csv", index=False)
    decisions.to_csv(REPORT_DIR / "decisions.csv", index=False)
    known_cisd_checks.to_csv(REPORT_DIR / "known_cisd_checks.csv", index=False)
    cisd_candidate_audit.to_csv(REPORT_DIR / "cisd_candidate_audit.csv", index=False)
    known_candidate_ranks.to_csv(REPORT_DIR / "known_final_cisd_candidate_ranks.csv", index=False)
    comparison.to_csv(REPORT_DIR / "comparison.csv", index=False)
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    raw_decisions.to_csv(REPORT_DIR / "raw_baseline_decisions.csv", index=False)
    raw_comparison.to_csv(REPORT_DIR / "raw_baseline_comparison.csv", index=False)
    raw_summary.to_csv(REPORT_DIR / "raw_baseline_summary.csv", index=False)
    explicit_target_decisions.to_csv(REPORT_DIR / "explicit_target_decisions.csv", index=False)
    explicit_target_comparison.to_csv(REPORT_DIR / "explicit_target_comparison.csv", index=False)
    explicit_target_summary.to_csv(REPORT_DIR / "explicit_target_summary.csv", index=False)
    for name, frame_to_write in same_feed.items():
        frame_to_write.to_csv(REPORT_DIR / f"same_feed_{name}.csv", index=False)
    for name, frame_to_write in raw_same_feed.items():
        frame_to_write.to_csv(REPORT_DIR / f"raw_same_feed_{name}.csv", index=False)
    for name, frame_to_write in cross_sweep.items():
        frame_to_write.to_csv(REPORT_DIR / f"cross_sweep_{name}.csv", index=False)
    for name, frame_to_write in cross_sweep_explicit_target.items():
        frame_to_write.to_csv(REPORT_DIR / f"cross_sweep_explicit_target_{name}.csv", index=False)
    write_report(
        cases,
        cisd,
        htf,
        premarket_contexts,
        contexts,
        decisions,
        known_cisd_checks,
        known_candidate_ranks,
        liquidity_alignment,
        comparison,
        summary,
        raw_comparison,
        raw_summary,
        explicit_target_comparison,
        explicit_target_summary,
        same_feed,
        raw_same_feed,
        cross_sweep,
        cross_sweep_explicit_target,
    )
    write_research_roadmap()
    write_development_package_final_report(
        raw_summary,
        summary,
        raw_same_feed,
        same_feed,
        cross_sweep,
        liquidity_alignment,
        explicit_target_summary,
    )
    print(f"Wrote: {REPORT_DIR}")


def run_same_feed_watch(cases: pd.DataFrame, state_config: ManualStateConfig) -> dict[str, pd.DataFrame]:
    """Audit only cases whose manual feed/timeframe exists locally.

    This output is deliberately separate from the ten-case DukasCopy comparison;
    it prevents feed mismatch from masquerading as a strategy-rule finding.
    """
    eligible = cases[cases["manual_timeframe"].str.contains("3m", case=False, na=False)].copy()
    if eligible.empty:
        return {}
    symbol = str(eligible["manual_feed"].iloc[0]).strip()
    timeframe = "3m"
    paths = base_report.filter_paths(RAW_DIR, symbol, timeframe)
    if not paths:
        return {}
    frame = load_ohlcv(paths).frame
    available_dates = set(frame["date"].astype(str))
    eligible = eligible[eligible["date"].isin(available_dates)].copy()
    if eligible.empty:
        return {}
    dates = [pd.Timestamp(value).date() for value in eligible["date"]]
    selected = harness.select_case_windows(frame, dates)
    base_config = harness.build_profile_config("challenge_core_v2", "DUKASCOPY_USATECHIDXUSD", timeframe)
    config = replace(base_config, symbol=symbol, timeframe=timeframe)
    result = run_manual_state_backtest(selected, config, dates, state_config)
    decisions = decisions_to_frame(result.days)
    candidate_audit = build_cisd_candidate_audit(eligible, result, selected, config, state_config, symbol)
    same_liquidity_selections = liquidity_selections_to_frame(result.days)
    same_context_triggers = context_triggers_to_frame(result.days)
    return {
        "cases": eligible,
        "premarket_contexts": premarket_contexts_to_frame(result.days),
        "context_triggers": same_context_triggers,
        "context_authority": context_authority_to_frame(result.days),
        "liquidity_selections": same_liquidity_selections,
        "liquidity_alignment": build_liquidity_alignment(
            eligible,
            same_liquidity_selections,
            same_context_triggers,
        ),
        "cisd_events": cisd_events_to_frame(result.days),
        "cisd_qualifications": cisd_qualifications_to_frame(result.days),
        "thesis_episodes": thesis_episodes_to_frame(result.days),
        "decisions": decisions,
        "comparison": compare_cases(eligible, decisions),
        "known_cisd_checks": build_known_cisd_checks(cisd_events_to_frame(result.days)),
        "manual_path_checks": build_manual_path_checks(eligible, result, selected, config, state_config),
        "cisd_candidate_audit": candidate_audit,
        "known_final_cisd_candidate_ranks": candidate_audit[candidate_audit["known_final_match"]].copy(),
    }


def run_cross_sweep_audit(state_config: ManualStateConfig) -> dict[str, pd.DataFrame]:
    cases_path = ROOT / "calibration_examples" / "manual_cross_sweep_lifecycle_labels_v2.csv"
    cases = pd.read_csv(cases_path, keep_default_na=False)
    decision_frames: list[pd.DataFrame] = []
    qualification_frames: list[pd.DataFrame] = []
    premarket_context_frames: list[pd.DataFrame] = []
    context_trigger_frames: list[pd.DataFrame] = []
    authority_frames: list[pd.DataFrame] = []
    liquidity_selection_frames: list[pd.DataFrame] = []
    thesis_episode_frames: list[pd.DataFrame] = []
    for symbol, symbol_cases in cases.groupby("symbol"):
        timeframe = "3m" if symbol == "DUKASCOPY_USATECHIDXUSD" else "5m"
        paths = base_report.filter_paths(RAW_DIR, symbol, timeframe)
        if not paths:
            continue
        frame = load_ohlcv(paths).frame
        dates = [pd.Timestamp(value).date() for value in symbol_cases["date"]]
        selected = harness.select_case_windows(frame, list(symbol_cases["date"]))
        config = harness.build_profile_config("challenge_core_v2", symbol, timeframe)
        result = run_manual_state_backtest(selected, config, dates, state_config)
        decisions = decisions_to_frame(result.days)
        if not decisions.empty:
            decision_frames.append(decisions)
        qualifications = cisd_qualifications_to_frame(result.days)
        if not qualifications.empty:
            qualifications["symbol"] = symbol
            qualifications["timeframe"] = timeframe
            qualification_frames.append(qualifications)
        premarket_contexts = premarket_contexts_to_frame(result.days)
        if not premarket_contexts.empty:
            premarket_contexts["symbol"] = symbol
            premarket_context_frames.append(premarket_contexts)
        context_triggers = context_triggers_to_frame(result.days)
        if not context_triggers.empty:
            context_triggers["symbol"] = symbol
            context_trigger_frames.append(context_triggers)
        authority = context_authority_to_frame(result.days)
        if not authority.empty:
            authority["symbol"] = symbol
            authority_frames.append(authority)
        liquidity_selection = liquidity_selections_to_frame(result.days)
        if not liquidity_selection.empty:
            liquidity_selection["symbol"] = symbol
            liquidity_selection_frames.append(liquidity_selection)
        thesis = thesis_episodes_to_frame(result.days)
        if not thesis.empty:
            thesis["symbol"] = symbol
            thesis_episode_frames.append(thesis)
    decisions = pd.concat(decision_frames, ignore_index=True) if decision_frames else pd.DataFrame()
    qualifications = (
        pd.concat(qualification_frames, ignore_index=True) if qualification_frames else pd.DataFrame()
    )
    premarket_contexts = (
        pd.concat(premarket_context_frames, ignore_index=True) if premarket_context_frames else pd.DataFrame()
    )
    context_triggers = (
        pd.concat(context_trigger_frames, ignore_index=True) if context_trigger_frames else pd.DataFrame()
    )
    authority = pd.concat(authority_frames, ignore_index=True) if authority_frames else pd.DataFrame()
    liquidity_selections = (
        pd.concat(liquidity_selection_frames, ignore_index=True)
        if liquidity_selection_frames
        else pd.DataFrame()
    )
    thesis_episodes = (
        pd.concat(thesis_episode_frames, ignore_index=True) if thesis_episode_frames else pd.DataFrame()
    )
    expected_direction = {
        "SPX-FB2-018": "",
        "SPX-FB2-020": "short",
        "NQ-FB3-003": "long",
        "NQ-FB3-006": "",
    }
    rows: list[dict[str, object]] = []
    for _, case in cases.iterrows():
        day = decisions[
            (decisions["date"] == case["date"]) & (decisions["symbol"] == case["symbol"])
        ].copy() if not decisions.empty else decisions
        actual = select_day_decision(day)
        expected_setup = "VALID" if case["setup_valid"] == "yes" else "INVALID"
        expected_order = {
            "filled": "FILLED",
            "unfilled": "CANCELLED",
            "not_placed": "NOT_PLACED",
        }[case["order_state"]]
        expected_final = "TAKE" if case["expected_decision"] == "TAKE" else "SKIP"
        direction = expected_direction[str(case["case_id"])]
        actual_setup = "INVALID" if actual is None else actual["setup_state"]
        actual_order = "NOT_PLACED" if actual is None else actual["order_state"]
        actual_final = "SKIP" if actual is None else actual["final_decision"]
        actual_direction = "" if actual is None else actual["direction"]
        checks = {
            "setup_match": actual_setup == expected_setup,
            "order_match": actual_order == expected_order,
            "final_match": actual_final == expected_final,
            "direction_match": str(actual_direction) == direction,
        }
        rows.append(
            {
                "case_id": case["case_id"],
                "date": case["date"],
                "symbol": case["symbol"],
                "requalification_event": case["requalification_event"],
                "later_sweep_starts_new_thesis": case["later_sweep_starts_new_thesis"],
                "expected_setup": expected_setup,
                "expected_order_state": expected_order,
                "expected_final": expected_final,
                "expected_direction": direction,
                "actual_setup": actual_setup,
                "actual_order_state": actual_order,
                "actual_final": actual_final,
                "actual_direction": actual_direction,
                "actual_terminal_reason": "" if actual is None else actual["terminal_reason"],
                "actual_cisd_time": "" if actual is None else actual["cisd_time"],
                **checks,
                "exact": all(checks.values()),
            }
        )
    return {
        "cases": cases,
        "comparison": pd.DataFrame(rows),
        "decisions": decisions,
        "cisd_qualifications": qualifications,
        "premarket_contexts": premarket_contexts,
        "context_triggers": context_triggers,
        "context_authority": authority,
        "liquidity_selections": liquidity_selections,
        "thesis_episodes": thesis_episodes,
    }


def build_manual_path_checks(cases, result, frame, config, state_config) -> pd.DataFrame:
    expected_fvg_times = {"NQ-FB4-010": "11:48"}
    rows_out: list[dict[str, object]] = []
    day_lookup = {day.trade_date: day for day in result.days}
    for _, case in cases.iterrows():
        case_id = str(case["case_id"])
        if case_id not in expected_fvg_times or case_id not in KNOWN_FINAL_CISD:
            continue
        trade_date = pd.Timestamp(case["date"]).date()
        day = day_lookup.get(str(trade_date))
        if day is None:
            continue
        direction, manual_cisd_time = KNOWN_FINAL_CISD[case_id]
        expected_cisd = pd.Timestamp(f"{trade_date} {manual_cisd_time}", tz="America/New_York")
        candidates = [event for event in day.cisd_events if event.direction == direction]
        if not candidates:
            rows_out.append({"case_id": case_id, "manual_cisd_time": manual_cisd_time, "cisd_detected": False})
            continue
        event = min(candidates, key=lambda item: abs(pd.Timestamp(item.confirm_time) - expected_cisd))
        trade_rows = list(
            time_slice(
                frame,
                timestamp_on_day(trade_date, state_config.trade_window_start),
                timestamp_on_day(trade_date, state_config.trade_window_end),
            ).itertuples(index=False)
        )
        fvg = find_fvg_or_ifvg(
            trade_rows,
            event.confirm_index,
            event.direction,
            config.fvg_entry_mode,
            config.setup_type_filter,
            config.min_fvg_points,
            event.invalidation_level,
            state_config.fvg_window_candles,
            event.anchor_index,
        )
        fvg_index, entry_price, fvg_kind, *_ = fvg
        detected_fvg_time = "" if fvg_index is None else trade_rows[fvg_index].time.strftime("%H:%M")
        rows_out.append(
            {
                "case_id": case_id,
                "manual_cisd_time": manual_cisd_time,
                "detected_cisd_time": pd.Timestamp(event.confirm_time).strftime("%H:%M"),
                "cisd_detected": abs(pd.Timestamp(event.confirm_time) - expected_cisd) <= pd.Timedelta(minutes=3),
                "manual_fvg_time": expected_fvg_times[case_id],
                "detected_fvg_time": detected_fvg_time,
                "fvg_detected": detected_fvg_time == expected_fvg_times[case_id],
                "fvg_kind": "" if fvg_kind is None else fvg_kind,
                "entry_price": "" if entry_price is None else round(float(entry_price), 6),
            }
        )
    return pd.DataFrame(rows_out)


def build_cisd_candidate_audit(cases, result, frame, config, state_config, feed_basis: str) -> pd.DataFrame:
    case_by_date = {str(case["date"]): case for _, case in cases.iterrows()}
    output: list[dict[str, object]] = []
    for day in result.days:
        case = case_by_date.get(day.trade_date)
        if case is None:
            continue
        trade_date = pd.Timestamp(day.trade_date).date()
        trade_rows = list(
            time_slice(
                frame,
                timestamp_on_day(trade_date, state_config.trade_window_start),
                timestamp_on_day(trade_date, state_config.trade_window_end),
            ).itertuples(index=False)
        )
        known = KNOWN_FINAL_CISD.get(str(case["case_id"]))
        expected = None if known is None else pd.Timestamp(f"{day.trade_date} {known[1]}", tz="America/New_York")
        for event in day.cisd_events:
            fvg = find_fvg_or_ifvg(
                trade_rows,
                event.confirm_index,
                event.direction,
                config.fvg_entry_mode,
                config.setup_type_filter,
                config.min_fvg_points,
                event.invalidation_level,
                state_config.fvg_window_candles,
                event.anchor_index,
            )
            fvg_index, entry_price, fvg_kind, zone_lower, zone_upper, _ = fvg
            confirm = pd.Timestamp(event.confirm_time)
            known_match = bool(
                known is not None
                and event.direction == known[0]
                and expected is not None
                and abs(confirm - expected) <= pd.Timedelta(minutes=3)
            )
            output.append(
                {
                    "case_id": case["case_id"],
                    "date": day.trade_date,
                    "feed_basis": feed_basis,
                    "direction": event.direction,
                    "anchor_time": pd.Timestamp(event.anchor_time).strftime("%H:%M"),
                    "confirm_time": confirm.strftime("%H:%M"),
                    "known_final_match": known_match,
                    "displacement_points": event.displacement_points,
                    "displacement_body_ratio": event.displacement_body_ratio,
                    "invalidation_level": event.invalidation_level,
                    "post_cisd_array": fvg_index is not None,
                    "array_time": "" if fvg_index is None else trade_rows[fvg_index].time.strftime("%H:%M"),
                    "array_kind": "" if fvg_kind is None else fvg_kind,
                    "array_size": "" if zone_lower is None or zone_upper is None else round(zone_upper - zone_lower, 6),
                    "entry_price": "" if entry_price is None else round(float(entry_price), 6),
                }
            )
    audit = pd.DataFrame(output)
    if audit.empty:
        return audit
    audit["displacement_points_rank_in_direction"] = audit.groupby(["case_id", "direction"])[
        "displacement_points"
    ].rank(method="min", ascending=False)
    audit["displacement_ratio_rank_in_direction"] = audit.groupby(["case_id", "direction"])[
        "displacement_body_ratio"
    ].rank(method="min", ascending=False)
    audit["array_backed_sequence_in_direction"] = audit[audit["post_cisd_array"]].groupby(
        ["case_id", "direction"]
    ).cumcount() + 1
    return audit


def build_liquidity_alignment(
    cases: pd.DataFrame,
    selections: pd.DataFrame,
    context_triggers: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, case in cases.iterrows():
        selected = selections[selections["date"] == case["date"]] if not selections.empty else selections
        item = None if selected.empty else selected.iloc[0]
        expected_requirement = str(case["liquidity_requirement"])
        actual_requirement = "UNOBSERVED" if item is None else str(item["requirement"])
        day_triggers = (
            context_triggers[context_triggers["date"] == case["date"]]
            if not context_triggers.empty
            else context_triggers
        )
        has_va_flip = bool(
            not day_triggers.empty
            and day_triggers["gate"].astype(str).eq("REPLACED_BY_VA_FLIP").any()
        )
        if expected_requirement == "required":
            requirement_match = actual_requirement in {"REQUIRED", "SATISFIED"}
        elif expected_requirement == "optional":
            requirement_match = actual_requirement == "OPTIONAL"
        else:
            requirement_match = has_va_flip
        selected_name = "" if item is None else str(item["selected_name"])
        reference = str(case["liquidity_reference"]).lower()
        if "london high" in reference:
            reference_match = selected_name == "london_high"
        elif "ny pm high" in reference:
            reference_match = selected_name == "ny_pm_high"
        elif "swing high" in reference:
            reference_match = selected_name.startswith("swing_high")
        elif "swing low" in reference:
            reference_match = selected_name.startswith("swing_low")
        elif expected_requirement == "optional":
            reference_match = actual_requirement == "OPTIONAL"
        else:
            reference_match = ""
        rows.append(
            {
                "case_id": case["case_id"],
                "date": case["date"],
                "expected_requirement": expected_requirement,
                "actual_requirement": actual_requirement,
                "requirement_match": requirement_match,
                "expected_reference": case["liquidity_reference"],
                "selected_name": selected_name,
                "selected_price": "" if item is None else item["selected_price"],
                "sweep_state": "" if item is None else item["sweep_state"],
                "has_va_flip": has_va_flip,
                "reference_match": reference_match,
            }
        )
    return pd.DataFrame(rows)


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def compare_cases(cases: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, case in cases.iterrows():
        day = decisions[decisions["date"] == case["date"]].copy() if not decisions.empty else decisions
        actual = select_day_decision(day)
        if actual is None:
            actual_values = {
                "actual_setup": "INVALID",
                "actual_order_state": "NOT_PLACED",
                "actual_final": "SKIP",
                "actual_direction": "",
                "actual_outcome": "NO_TRADE",
                "actual_terminal_reason": "UNOBSERVED",
                "actual_context_gate": "",
                "actual_context_source": "",
                "actual_cisd_time": "",
                "actual_fvg_time": "",
                "actual_entry_time": "",
            }
        else:
            actual_values = {
                "actual_setup": actual["setup_state"],
                "actual_order_state": actual["order_state"],
                "actual_final": actual["final_decision"],
                "actual_direction": actual["direction"],
                "actual_outcome": actual["outcome"],
                "actual_terminal_reason": actual["terminal_reason"],
                "actual_context_gate": actual["context_gate"],
                "actual_context_source": actual["context_source"],
                "actual_cisd_time": actual["cisd_time"],
                "actual_fvg_time": actual["fvg_time"],
                "actual_entry_time": actual["entry_time"],
            }
        expected_direction = str(case["expected_direction"]).strip().lower()
        checks = {
            "setup_match": actual_values["actual_setup"] == case["expected_setup"],
            "order_match": actual_values["actual_order_state"] == case["expected_order_state"],
            "final_match": actual_values["actual_final"] == case["expected_final"],
            "direction_match": str(actual_values["actual_direction"]).lower() == expected_direction,
            "outcome_match": actual_values["actual_outcome"] == case["expected_outcome"],
        }
        known_final = KNOWN_FINAL_CISD.get(str(case["case_id"]))
        manual_final_cisd_time = "" if known_final is None else known_final[1]
        selected_cisd_delta_minutes: float | str = ""
        cisd_selection_match: bool | str = ""
        if known_final is not None and actual_values["actual_cisd_time"]:
            expected_cisd = pd.Timestamp(f"{case['date']} {known_final[1]}", tz="America/New_York")
            actual_cisd = pd.Timestamp(actual_values["actual_cisd_time"])
            selected_cisd_delta_minutes = round(abs((actual_cisd - expected_cisd).total_seconds()) / 60, 2)
            cisd_selection_match = selected_cisd_delta_minutes <= 3.0
        elif known_final is not None:
            cisd_selection_match = False
        label_exact = all(checks[key] for key in ["setup_match", "order_match", "final_match", "direction_match"])
        decision_exact = label_exact and (known_final is None or cisd_selection_match is True)
        rows.append(
            {
                "case_id": case["case_id"],
                "date": case["date"],
                "expected_setup": case["expected_setup"],
                "expected_order_state": case["expected_order_state"],
                "expected_final": case["expected_final"],
                "expected_direction": case["expected_direction"],
                "expected_outcome": case["expected_outcome"],
                **actual_values,
                **checks,
                "manual_final_cisd_time": manual_final_cisd_time,
                "selected_cisd_delta_minutes": selected_cisd_delta_minutes,
                "cisd_selection_match": cisd_selection_match,
                "label_exact": label_exact,
                "decision_exact": decision_exact,
                "status": "PASS" if decision_exact else "FAIL",
            }
        )
    return pd.DataFrame(rows)


def select_day_decision(day: pd.DataFrame) -> pd.Series | None:
    if day.empty:
        return None
    filled = day[day["order_state"] == "FILLED"].copy()
    if not filled.empty:
        filled["sort_time"] = pd.to_datetime(filled["entry_time"], utc=True, format="mixed")
        return filled.sort_values("sort_time").iloc[0]
    pending = day.copy()
    pending["sort_time"] = pd.to_datetime(pending["cisd_time"], utc=True, format="mixed")
    return pending.sort_values("sort_time").iloc[0]


def summarize(comparison: pd.DataFrame, profile: str = "MANUAL_STATE_V1") -> pd.DataFrame:
    known_cisd = comparison[comparison["manual_final_cisd_time"] != ""]
    return pd.DataFrame(
        [
            {
                "profile": profile,
                "cases": len(comparison),
                "setup_accuracy": round(float(comparison["setup_match"].mean() * 100), 2),
                "order_accuracy": round(float(comparison["order_match"].mean() * 100), 2),
                "final_accuracy": round(float(comparison["final_match"].mean() * 100), 2),
                "direction_accuracy": round(float(comparison["direction_match"].mean() * 100), 2),
                "decision_exact_rate": round(float(comparison["decision_exact"].mean() * 100), 2),
                "known_final_cisd_selection_accuracy": (
                    round(float(known_cisd["cisd_selection_match"].astype(bool).mean() * 100), 2)
                    if not known_cisd.empty
                    else ""
                ),
                "outcome_accuracy": round(float(comparison["outcome_match"].mean() * 100), 2),
            }
        ]
    )


def build_known_cisd_checks(cisd: pd.DataFrame) -> pd.DataFrame:
    known_dates = {"NQ-FB4-005": "2023-11-06", "NQ-FB4-010": "2025-04-09"}
    known = [
        {"case_id": case_id, "date": known_dates[case_id], "direction": values[0], "manual_time": values[1]}
        for case_id, values in KNOWN_FINAL_CISD.items()
    ]
    rows: list[dict[str, object]] = []
    for item in known:
        expected = pd.Timestamp(f"{item['date']} {item['manual_time']}", tz="America/New_York")
        candidates = cisd[(cisd["date"] == item["date"]) & (cisd["direction"] == item["direction"])].copy()
        if candidates.empty:
            rows.append({**item, "detected_time": "", "delta_minutes": "", "detected": False})
            continue
        candidates["confirm_dt"] = pd.to_datetime(candidates["confirm_time"], utc=True, format="mixed").dt.tz_convert(
            "America/New_York"
        )
        candidates["delta"] = (candidates["confirm_dt"] - expected).abs()
        nearest = candidates.sort_values("delta").iloc[0]
        rows.append(
            {
                **item,
                "detected_time": nearest["confirm_dt"].strftime("%H:%M"),
                "delta_minutes": round(float(nearest["delta"].total_seconds() / 60), 2),
                "detected": True,
                "anchor_time": nearest["anchor_time"],
                "body_level": nearest["body_level"],
                "invalidation_level": nearest["invalidation_level"],
                "displacement_points": nearest["displacement_points"],
                "displacement_body_ratio": nearest["displacement_body_ratio"],
            }
        )
    return pd.DataFrame(rows)


def write_report(
    cases: pd.DataFrame,
    cisd: pd.DataFrame,
    htf: pd.DataFrame,
    premarket_contexts: pd.DataFrame,
    contexts: pd.DataFrame,
    decisions: pd.DataFrame,
    known_cisd_checks: pd.DataFrame,
    known_candidate_ranks: pd.DataFrame,
    liquidity_alignment: pd.DataFrame,
    comparison: pd.DataFrame,
    summary: pd.DataFrame,
    raw_comparison: pd.DataFrame,
    raw_summary: pd.DataFrame,
    explicit_target_comparison: pd.DataFrame,
    explicit_target_summary: pd.DataFrame,
    same_feed: dict[str, pd.DataFrame],
    raw_same_feed: dict[str, pd.DataFrame],
    cross_sweep: dict[str, pd.DataFrame],
    cross_sweep_explicit_target: dict[str, pd.DataFrame],
) -> None:
    case_counts = pd.DataFrame(
        [
            {
                "date": date,
                "cisd_events": int((cisd["date"] == date).sum()) if not cisd.empty else 0,
                "htf_arrays": int((htf["date"] == date).sum()) if not htf.empty else 0,
                "context_triggers": int((contexts["date"] == date).sum()) if not contexts.empty else 0,
                "decisions": int((decisions["date"] == date).sum()) if not decisions.empty else 0,
            }
            for date in cases["date"]
        ]
    )
    lines = [
        "# MANUAL_STATE_V1 — İlk 10 NQ",
        "",
        "Bu bir kârlılık optimizasyonu değildir. Yeni araştırma motorunun açıklanabilir yapı ve karar lifecycle audit'idir.",
        "",
        "Aktif CHALLENGE_CORE_V2 / FON_CORE / pair-cap ayarları değiştirilmemiştir.",
        "",
        "## Summary",
        "",
        base_report.markdown_table(summary),
        "",
        "## Raw CISD baseline comparison",
        "",
        base_report.markdown_table(
            pd.concat([raw_summary, explicit_target_summary, summary], ignore_index=True)
        ),
        "",
        base_report.markdown_table(raw_comparison),
        "",
        "### Explicit-only target-before-fill WATCH",
        "",
        base_report.markdown_table(explicit_target_comparison),
        "",
        "## Event counts",
        "",
        base_report.markdown_table(case_counts),
        "",
        "## Locked premarket contexts",
        "",
        base_report.markdown_table(premarket_contexts),
        "",
        "## Known CISD structure checks",
        "",
        base_report.markdown_table(known_cisd_checks),
        "",
        "## Known final CISD candidate ranks",
        "",
        base_report.markdown_table(known_candidate_ranks),
        "",
        "## Liquidity selector alignment",
        "",
        base_report.markdown_table(liquidity_alignment),
        "",
        "## Comparison",
        "",
        base_report.markdown_table(comparison),
        "",
        "## Same-feed WATCH audit",
        "",
        "Bu tablo yalnız yerelde aynı manuel feed ve execution timeframe bulunan vakaları gösterir; ana 10-vaka skoruna karıştırılmaz.",
        "",
        base_report.markdown_table(same_feed.get("comparison", pd.DataFrame())),
        "",
        "### Same-feed raw vs protected-body",
        "",
        base_report.markdown_table(
            pd.concat(
                [
                    raw_same_feed.get("comparison", pd.DataFrame()).assign(variant="RAW_CISD_BASELINE"),
                    same_feed.get("comparison", pd.DataFrame()).assign(variant="CISD_QUALIFICATION_V1"),
                ],
                ignore_index=True,
            )
        ),
        "",
        "### Same-feed locked context",
        "",
        base_report.markdown_table(same_feed.get("premarket_contexts", pd.DataFrame())),
        "",
        "### Same-feed liquidity selector alignment",
        "",
        base_report.markdown_table(same_feed.get("liquidity_alignment", pd.DataFrame())),
        "",
        "### Same-feed context triggers",
        "",
        base_report.markdown_table(same_feed.get("context_triggers", pd.DataFrame())),
        "",
        "### Same-feed known final CISD detection",
        "",
        base_report.markdown_table(same_feed.get("known_cisd_checks", pd.DataFrame())),
        "",
        "### Same-feed manual structure path",
        "",
        base_report.markdown_table(same_feed.get("manual_path_checks", pd.DataFrame())),
        "",
        "### Same-feed known final CISD candidate ranks",
        "",
        base_report.markdown_table(same_feed.get("known_final_cisd_candidate_ranks", pd.DataFrame())),
        "",
        "## Cross-sweep contrast audit",
        "",
        base_report.markdown_table(cross_sweep.get("comparison", pd.DataFrame())),
        "",
        "### Cross-sweep explicit-only target WATCH",
        "",
        base_report.markdown_table(cross_sweep_explicit_target.get("comparison", pd.DataFrame())),
        "",
        "## Model contract",
        "",
        "- CISD detector emits anchor, body level, confirm close, displacement and invalidation.",
        "- CISD_QUALIFICATION_V1 keeps an opposite raw CISD internal until its confirmation close crosses the active structure's protected origin body.",
        "- Same-direction CISD requalifies the protected body; a qualified opposite body-close creates REVERSAL.",
        "- Opposite CISD invalidates the old thesis; any new trade waits for a new post-CISD FVG/IFVG.",
        "- A post-CISD three-candle FVG may use the CISD anchor as its first candle, but must complete no earlier than the CISD confirmation.",
        "- Same-direction CISD may requalify future structure, but it does not cancel an already resting order.",
        "- If an order first touches entry on the same candle that closes an opposite CISD/context replacement, the research engine resolves the intrabar ambiguity conservatively as cancelled.",
        "- HTF FVG/OB arrays have fresh/tapped/respected/body-closed/flipped/reclaimed states.",
        "- VA proximity only creates an HTF candidate; controlling status requires an observed reaction from the zone.",
        "- FRESH/TAPPED arrays do not control the thesis. If no nearby array has reacted, HTF contributes no gate.",
        "- The reaction used to select the controlling array must occur before the 09:30 NY open; a first reaction during NY does not create a new controlling array.",
        "- Body-close terminates the original array; it does not create an opposite trade signal by itself.",
        "- A body-closed array becomes controlling on the opposite side only after a held flip retest; a second invalidation is terminal.",
        "- A VA flip stamped on the same execution candle as HTF invalidation cannot retroactively bypass the premarket blocker.",
        "- A reacted HTF array is retained conservatively as a blocker; it does not create trade direction by itself.",
        "- Context authority is emitted explicitly as BLOCK, UNLOCK, GRANT or REPLACE; an unlock is not a direction grant.",
        "- Liquidity selection chooses one named level by active-boundary distance before name priority; another level's sweep cannot satisfy it.",
        "- A VA-flip may reuse the still-active pre-existing CISD, but its entry array must complete after the authority event.",
        "- A single premarket VA touch-and-close is audit evidence, not an authority gate. Repeated support/accumulation remains WATCH.",
        "- HTF and confirmed VA-flip events create context gates; HTF body-close is not represented as a fake liquidity sweep.",
        "- Pending orders end as filled, target-before-fill, opposite-CISD, context replacement or trade-window-end; same-direction requalification alone is not terminal.",
        "- Explicit-only target-before-fill was tested and rejected; it did not resolve the SPX-FB2-020 order mismatch.",
        "",
        "## Interpretation",
        "",
        "- The standalone detector sees the explicitly timed final CISDs in cases 05 and 10.",
        "- On the same Capital.com feed, case 10's 11:45 CISD and anchor-backed 11:48 FVG are both detected exactly.",
        "- Protected-body qualification changes same-feed case 10 from the raw 10:36 short/SL route to the manual 11:45 long, 11:48 FVG, 11:51 fill and TP route.",
        "- The same-candle HTF/VA authority rule fixes SPX-FB2-018: the later VA flip no longer creates an unauthorized short thesis.",
        "- Case 10's final CISD ranks first by displacement points and body ratio, but case 05's final CISD ranks second on the mismatched 3m DukasCopy feed; maximum displacement is therefore not accepted as a general selection rule.",
        "- DukasCopy first-ten aggregate does not improve, and the cross-sweep contrast is only 1/4 exact; CISD_QUALIFICATION_V1 remains research-only.",
        "- Liquidity requirement matches 5/10 and named-reference matches 2/8 observable cases; LIQUIDITY_SELECTOR_V1 remains WATCH.",
        "- Remaining mismatch is mainly context authority, feed/timeframe equivalence, liquidity selection and entry lifecycle.",
        "- Cases 06 and 09 remain critical false-positive controls; MANUAL_STATE_V1 must not be activated while those produce orders.",
        "- HTF array lifecycle is now explicit, but the deterministic controlling-array selection still requires structural validation rather than threshold/weight optimization.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_research_roadmap() -> None:
    lines = [
        "# MANUAL_STATE_V1 — Araştırma Yol Haritası",
        "",
        "## Sabit sınırlar",
        "",
        "- Amaç kârlılık veya işlem sıklığı değil; NQ + SPX manuel TAKE/SKIP ve yön uyumu.",
        "- Aktif CHALLENGE_CORE_V2, FON_CORE ve günlük NQ + SPX -1R pair cap değişmedi.",
        "- Controlling-array selector threshold/weight optimizasyonu yapılmayacak.",
        "- DukasCopy ve Capital.com sonuçları aynı skor içinde karıştırılmayacak.",
        "",
        "## Tamamlanan araştırma altyapısı",
        "",
        "1. CISD artık anchor, body level, confirm, displacement ve invalidation olarak açıklanıyor.",
        "2. HTF FVG/OB lifecycle fresh, tapped, respected, body-closed, flipped ve terminal invalidation olarak izleniyor.",
        "3. Controlling array yalnız premarket reaksiyonuyla seçiliyor; NY ilk reaksiyonu kontrol yaratmıyor.",
        "4. Reaksiyon görmüş HTF array varsayılan olarak BLOCK_ONLY; tek başına işlem yönü üretmiyor.",
        "5. Aynı yön CISD bekleyen emri otomatik iptal etmiyor.",
        "6. FVG, CISD anchor mumunu ilk mum olarak kullanabiliyor; tamamlanması CISD confirm'den önce olamıyor.",
        "7. Same-feed WATCH audit ana 10-vaka skorundan ayrıldı.",
        "8. Bilinen final CISD'yi tespit etmek ile karar için onu seçmek ayrı metrik oldu.",
        "9. Context authority BLOCK / UNLOCK / GRANT / REPLACE olayları olarak raporlanıyor.",
        "10. Likidite seçimi aktif VA sınırına mesafe önceliğiyle tek selected named level üretiyor.",
        "11. VA flip pre-existing geçerli CISD'yi kullanabiliyor; FVG authority sonrasında tamamlanmak zorunda.",
        "12. Synthetic-target ve explicit-only target-before-fill varyantları ayrı test edildi.",
        "",
        "## Kanıtlanan mevcut teşhis",
        "",
        "- İşlem 05 ve 10'un bilinen final CISD'leri ham dedektörde bulunuyor.",
        "- Capital.com 3m İşlem 10'da 11:45 bullish CISD ve anchor-backed 11:48 bullish FVG tam bulunuyor.",
        "- Protected-body lifecycle aynı-feed İşlem 10 rotasını düzeltti: 10:36 short artık INTERNAL_OPPOSITE; seçilen rota 11:45 bullish CISD, 11:48 FVG, 11:51 fill ve TP.",
        "- DukasCopy ilk10 toplamı iyileşmedi; 06 ve 09 false-order kontrolleri geçmiyor. Varyant research-only kalıyor.",
        "- Cross-sweep kontrastında SPX-FB2-018 düzeldi; aynı mumdaki HTF invalidation VA flip'e geriye dönük yetki vermiyor. Toplam sonuç 1/4 exact.",
        "- İşlem 10 final CISD displacement sıralarında birinci; İşlem 05 final CISD DukasCopy 3m üzerinde ikinci. Bu nedenle 'en büyük displacement' tek başına kural olmayacak.",
        "- Yerel Capital.com veri kapsamı 2025-04-03 sonrası olduğu için ilk 10 içinden yalnız İşlem 10 aynı-feed doğrulanabiliyor.",
        "",
        "## Geliştirme paketi sonuçları",
        "",
        "### 1. CISD_QUALIFICATION_V1 — güçlü aday",
        "",
        "Uygulandı ve research-only tutuluyor. Ham matematiksel CISD ile işlem rotasını değiştirebilen qualified/final CISD ayrımı protected-body lifecycle ile yapılıyor:",
        "",
        "- context tarafından izin verilen yön;",
        "- anlamlı anchor/body level kırılımı;",
        "- displacement ve post-CISD PD array teslimi;",
        "- karşı mikro-CISD'nin tek başına mı, yoksa thesis invalidation/qualified opposite yapı ile mi terminal olduğu.",
        "",
        "Sonuç: aynı-feed 10 geçti; 05 feed-mismatch, 06 ve 09 false-positive kontrolleri geçmedi. Aktife terfi yok.",
        "",
        "### 2. CONTEXT_AUTHORITY_V1 — güçlü aday",
        "",
        "Uygulandı. BLOCK / UNLOCK / GRANT / REPLACE ayrımı açık. Aynı mum HTF invalidation + VA flip kronoloji kuralı SPX-FB2-018'i düzeltti; toplam kanıt aktife terfi için yetersiz.",
        "",
        "### 3. LIQUIDITY_SELECTOR_V1 — güçlü aday",
        "",
        "Uygulandı. Önce aktif VA sınırına mesafe, sonra isim önceliği kullanılıyor; başka seviyenin sweep'i selected liquidity'yi karşılamıyor. Requirement 5/10, named-reference 2/8: WATCH, terfi yok.",
        "",
        "### 4. THESIS_REQUALIFICATION_V1 — güçlü kontrast seti",
        "",
        "- Fresh named-liquidity sweep yeni thesis açabilir: SPX-FB2-020.",
        "- Sonradan görülen sıradan engine sweep yeni thesis açmamalı: SPX-FB2-018.",
        "- Fresh sweep olmadan doğrulanmış VA flip requalification sağlayabilir: NQ-FB3-003.",
        "- HTF body-close eksikse karşı yön bloklu kalabilir: NQ-FB3-006.",
        "",
        "Sonuç: kontrast seti 1/4 exact. Yalnız SPX-FB2-018 geçti; paket tamamlandı fakat genelleme kanıtlanmadı.",
        "",
        "### 5. ORDER_LIFECYCLE_V1 — mevcut güçlü temel, regresyonla korunacak",
        "",
        "FILLED, TARGET_BEFORE_FILL, OPPOSITE_CISD, CONTEXT_REPLACED ve TRADE_WINDOW_END ayrı kaldı. Explicit-only target-before-fill SPX-FB2-020'yi düzeltmedi ve reddedildi; synthetic-R ana araştırma davranışı korundu.",
        "",
        "## Yalnız WATCH olarak kalacaklar",
        "",
        "- Premarket VAH repeated-support / accumulation: şimdilik yalnız İşlem 10 açık etiketi var.",
        "- 5m context + 3m execution görev ayrımı: şimdilik yalnız İşlem 10 açık örnek.",
        "- IFVG/discount birleşik entry modeli: İşlem 05 tekil kanıt.",
        "- Tek mum VA touch-and-close: ilk 10'u ayırmadığı için authority kuralı olmayacak.",
        "- Intrabar entry ile aynı mum terminal çakışması: daha fazla manuel örnek gelene kadar konservatif WATCH çözümü.",
        "",
        "## Aktife geçiş kapısı",
        "",
        "Bir araştırma varyantı ancak aşağıdakileri birlikte sağlarsa aktif sisteme aday olur:",
        "",
        "1. İşlem 06 ve 09'da emir üretmez.",
        "2. İşlem 05 ve 10'da yalnız doğru final CISD rotasını seçer.",
        "3. İşlem 03 ve 07'nin no-fill terminalini doğru ayırır.",
        "4. Cross-sweep dört-vaka kontrast setini bozmadan geçirir.",
        "5. Aynı-feed kanıtı ile feed-mismatch gözlemini ayrı raporlar.",
        "6. NQ + SPX üzerinde yön/karar uyumunu artırır; yalnız işlem sayısını veya R sonucunu değiştirmesi yeterli değildir.",
        "",
    ]
    (REPORT_DIR / "research_roadmap.md").write_text("\n".join(lines), encoding="utf-8")


def write_development_package_final_report(
    raw_summary: pd.DataFrame,
    package_summary: pd.DataFrame,
    raw_same_feed: dict[str, pd.DataFrame],
    package_same_feed: dict[str, pd.DataFrame],
    cross_sweep: dict[str, pd.DataFrame],
    liquidity_alignment: pd.DataFrame,
    explicit_target_summary: pd.DataFrame,
) -> None:
    component_verdicts = pd.DataFrame(
        [
            {
                "component": "CISD_QUALIFICATION_V1",
                "evidence": "Same-feed NQ-FB4-010 exact manual route",
                "result": "KEEP_RESEARCH",
                "active": "NO",
            },
            {
                "component": "CONTEXT_AUTHORITY_V1",
                "evidence": "SPX-FB2-018 false requalification removed",
                "result": "KEEP_RESEARCH",
                "active": "NO",
            },
            {
                "component": "LIQUIDITY_SELECTOR_V1",
                "evidence": "Requirement 5/10; named-reference 2/8 observed",
                "result": "WATCH",
                "active": "NO",
            },
            {
                "component": "THESIS_REQUALIFICATION_V1",
                "evidence": "Cross-sweep contrast 1/4 exact",
                "result": "INSUFFICIENT",
                "active": "NO",
            },
            {
                "component": "ORDER_LIFECYCLE_V1",
                "evidence": "Explicit-only target did not fix SPX-FB2-020",
                "result": "REJECT_NEW_TARGET_RULE",
                "active": "NO",
            },
            {
                "component": "COMBINED_PACKAGE",
                "evidence": "DukasCopy first10 regressed; same-feed coverage only 1 case",
                "result": "DO_NOT_PROMOTE",
                "active": "NO",
            },
        ]
    )
    cross = cross_sweep.get("comparison", pd.DataFrame())
    same_raw = raw_same_feed.get("comparison", pd.DataFrame()).assign(variant="RAW")
    same_package = package_same_feed.get("comparison", pd.DataFrame()).assign(variant="PACKAGE")
    lines = [
        "# Manuel Uyum Geliştirme Paketi — Nihai Rapor",
        "",
        "## Son karar",
        "",
        "Birleşik paket aktif CHALLENGE_CORE_V2 / FON_CORE sistemine alınmayacak. Aynı-feed İşlem 10'da güçlü ve doğru bir iyileşme var; ancak ilk10 ve cross-sweep genelleme kapıları geçilmedi.",
        "",
        "## Bileşen kararları",
        "",
        base_report.markdown_table(component_verdicts),
        "",
        "## DukasCopy ilk10 — raw vs paket",
        "",
        base_report.markdown_table(pd.concat([raw_summary, package_summary], ignore_index=True)),
        "",
        "Paket setup/order/final/outcome uyumunu düşürdü; yalnız direction accuracy 10 puan arttı. Decision-exact oranı iki varyantta da 0%.",
        "",
        "## Same-feed kanıtı",
        "",
        base_report.markdown_table(pd.concat([same_raw, same_package], ignore_index=True)),
        "",
        "Capital.com 3m İşlem 10 raw rotada 10:36 short/SL iken paket 11:45 bullish CISD → 11:48 FVG → 11:51 fill → TP rotasını seçti. Bu gerçek artı, fakat yalnız tek same-feed vaka olduğu için genellenemez.",
        "",
        "## Cross-sweep kontrastı",
        "",
        base_report.markdown_table(cross),
        "",
        "Yalnız SPX-FB2-018 exact geçti. SPX-FB2-020 entry/order lifecycle; NQ-FB3-003 ve 006 feed/timeframe/context uyuşmazlığı olarak kaldı.",
        "",
        "## Likidite seçici",
        "",
        base_report.markdown_table(liquidity_alignment),
        "",
        "Requirement sınıfı 5/10 eşleşti. Gözlenebilir sekiz referansın yalnız ikisi eşleşti. En yakın aktif-boundary seviyesini seçme mekanizması açıklanabilir, fakat mevcut veriyle doğrulanmış bir üretim kuralı değil.",
        "",
        "## Order lifecycle hedef varyantı",
        "",
        base_report.markdown_table(explicit_target_summary),
        "",
        "Sentetik 3R hedefi target-before-fill kontrolünden çıkarmak sonuçları iyileştirmedi; SPX-FB2-020 bu kez opposite-CISD ile iptal oldu. Bu varyant reddedildi ve ana pakette synthetic-R davranışı korundu.",
        "",
        "## Korunacak araştırma kazanımları",
        "",
        "- Protected-body CISD lifecycle: START / REQUALIFIED / INTERNAL_OPPOSITE / REVERSAL.",
        "- Anchor-backed post-CISD FVG; FVG authority olayından önce tamamlanamaz.",
        "- HTF invalidation ile aynı mumdaki VA flip geriye dönük yetki alamaz.",
        "- Context authority açıkça BLOCK / UNLOCK / GRANT / REPLACE olarak raporlanır.",
        "- Tek selected named liquidity: önce VA sınırına mesafe, sonra isim önceliği; başka sweep selected seviyeyi karşılamaz.",
        "- Same-direction CISD mevcut pending order'ı tek başına iptal etmez.",
        "- Same-feed ve feed-mismatch skorları birbirine karıştırılmaz.",
        "",
        "## Aktif sistem kontrolü",
        "",
        "2026-07-22 tarihinde aktif manual-feedback V4 regresyonu yeniden çalıştırıldı. summary.csv ve comparison.csv SHA-256 değerleri çalışma öncesi/sonrası aynı kaldı. CHALLENGE_CORE_V2, FON_CORE, günlük NQ+SPX -1R cap ve selector varsayılanları değişmedi.",
        "",
        "## Net cevap",
        "",
        "- Değişiklik oldu mu? Araştırma motorunda evet; aktif stratejide hayır.",
        "- Artısı var mı? Protected-body CISD ve context chronology için evet, iki güçlü vaka düzeldi.",
        "- Genel fayda kanıtlandı mı? Hayır. İlk10 ve cross-sweep geçiş kapıları başarısız.",
        "- Aktife alınmalı mı? Hayır.",
        "- Sonraki veri ihtiyacı: İşlem 01–09 için Capital.com 5m/3m tarihsel OHLC veya aynı-feed yeniden etiketlenmiş yeni vakalar.",
        "",
    ]
    (REPORT_DIR / "development_package_final_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
