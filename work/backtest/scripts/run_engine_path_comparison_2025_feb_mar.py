from __future__ import annotations

import sys
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_corrected_engine_3month_report as report_utils
import run_main_candidate_filter_tests as filters
import run_manual_rule_v2_validation as manual_v2
from backtest.manual_state import (
    ManualStateConfig,
    decisions_to_frame,
    run_manual_state_backtest,
)
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import (
    compute_first30_range,
    lifecycles_to_frame,
    run_backtest,
    trades_to_frame,
)


START_DATE = pd.Timestamp("2025-02-01")
END_DATE = pd.Timestamp("2025-03-31")
REPORT_DIR = ROOT / "outputs" / "reports" / "engine_path_comparison_2025_feb_mar"
ARCHIVED_OLD_TRADES = ROOT / "outputs" / "reports" / "manual_rule_v2_validation" / "all_leg_trades.csv"
SYMBOLS = {
    "nq": ("DUKASCOPY_USATECHIDXUSD", "3m"),
    "spx": ("DUKASCOPY_USA500IDXUSD", "5m"),
}


def main() -> None:
    started = time.perf_counter()
    reset_report_dir()
    loaded = filters.load_data()
    configs = build_active_configs(loaded)
    business_dates = [item.date() for item in pd.bdate_range(START_DATE, END_DATE)]

    old_trades: list[pd.DataFrame] = []
    old_lifecycles: list[pd.DataFrame] = []
    updated_raw: list[pd.DataFrame] = []
    updated_control: list[pd.DataFrame] = []
    day_diagnostics: list[dict[str, object]] = []
    eligibility_rows: list[dict[str, object]] = []
    integrity_rows: list[dict[str, object]] = []

    for leg_key, (symbol, timeframe) in SYMBOLS.items():
        print(f"Running {leg_key.upper()} old and updated engines...")
        frame = loaded[(symbol, timeframe)].copy()
        config = configs[leg_key]
        selected = select_window(frame)

        old_result = run_backtest(selected, config)
        old_trade_frame = within_period(trades_to_frame(old_result.trades), "entry_time")
        old_lifecycle_frame = within_period(lifecycles_to_frame(old_result.lifecycles), "date")
        old_trades.append(tag(old_trade_frame, leg_key, "previous_challenge_core_v2"))
        old_lifecycles.append(tag(old_lifecycle_frame, leg_key, "previous_challenge_core_v2"))

        raw_state_config = ManualStateConfig(
            trade_window_start="09:30",
            trade_window_end="12:00",
            cisd_anchor_lookback=6,
            fvg_window_candles=config.fvg_window_candles,
            enforce_symbol_config_envelope=False,
        )
        raw_result = run_manual_state_backtest(selected, config, business_dates, raw_state_config)
        raw_decisions = decisions_to_frame(raw_result.days)
        updated_raw.append(tag(raw_decisions, leg_key, "updated_state_raw"))
        day_diagnostics.extend(build_day_diagnostics(raw_result.days, leg_key, "updated_state_raw"))

        eligibility = build_eligibility(frame, config, business_dates, leg_key)
        eligibility_rows.extend(eligibility)
        controlled_state_config = ManualStateConfig(
            trade_window_start=config.trade_window_start,
            trade_window_end=config.trade_window_end,
            cisd_anchor_lookback=6,
            fvg_window_candles=config.fvg_window_candles,
            enforce_symbol_config_envelope=True,
        )
        controlled_result = run_manual_state_backtest(
            selected,
            config,
            business_dates,
            controlled_state_config,
        )
        controlled_decisions = decisions_to_frame(controlled_result.days)
        updated_control.append(tag(controlled_decisions, leg_key, "updated_state_common_envelope"))
        day_diagnostics.extend(
            build_day_diagnostics(controlled_result.days, leg_key, "updated_state_common_envelope")
        )
        integrity_rows.extend(build_integrity_checks(raw_decisions, frame, config, leg_key))

    old_trade_frame = concat(old_trades)
    old_lifecycle_frame = concat(old_lifecycles)
    raw_decisions = concat(updated_raw)
    control_decisions = concat(updated_control)
    diagnostics = pd.DataFrame(day_diagnostics)
    eligibility = pd.DataFrame(eligibility_rows).drop(columns=["date_value"])
    integrity = pd.DataFrame(integrity_rows)

    old_filled = normalize_old_filled(old_trade_frame)
    raw_filled = normalize_updated_filled(raw_decisions, configs)
    control_filled = normalize_updated_filled(control_decisions, configs)
    pre_cap = {
        "previous_challenge_core_v2": old_filled,
        "updated_state_raw": raw_filled,
        "updated_state_common_envelope": control_filled,
    }
    capped = {
        "previous_challenge_core_v2": apply_pair_cap(old_filled),
        "updated_state_raw": apply_pair_cap(raw_filled),
        "updated_state_common_envelope": apply_pair_cap(control_filled),
    }
    pair_cap_summary = build_pair_cap_summary(pre_cap, capped)
    pair_cap_suppressed = build_pair_cap_suppressed(pre_cap, capped)
    summary = build_summary(
        old_trade_frame,
        old_lifecycle_frame,
        raw_decisions,
        control_decisions,
        capped,
        business_dates,
    )
    pre_cap_day_comparison = build_day_comparison(
        pre_cap, diagnostics, eligibility, old_lifecycle_frame
    )
    day_comparison = build_day_comparison(capped, diagnostics, eligibility, old_lifecycle_frame)
    raw_trade_comparison = match_filled_trades(
        capped["previous_challenge_core_v2"], capped["updated_state_raw"], "updated_state_raw"
    )
    control_trade_comparison = match_filled_trades(
        capped["previous_challenge_core_v2"],
        capped["updated_state_common_envelope"],
        "updated_state_common_envelope",
    )
    archived_check = compare_with_archived(old_trade_frame)

    old_trade_frame.to_csv(REPORT_DIR / "previous_engine_trades.csv", index=False)
    old_lifecycle_frame.to_csv(REPORT_DIR / "previous_engine_lifecycles.csv", index=False)
    raw_decisions.to_csv(REPORT_DIR / "updated_state_raw_decisions.csv", index=False)
    control_decisions.to_csv(REPORT_DIR / "updated_state_common_envelope_decisions.csv", index=False)
    diagnostics.to_csv(REPORT_DIR / "updated_state_day_diagnostics.csv", index=False)
    eligibility.to_csv(REPORT_DIR / "common_envelope_eligibility.csv", index=False)
    integrity.to_csv(REPORT_DIR / "updated_fill_integrity_checks.csv", index=False)
    summary.to_csv(REPORT_DIR / "comparison_summary.csv", index=False)
    pair_cap_summary.to_csv(REPORT_DIR / "pair_cap_summary.csv", index=False)
    pair_cap_suppressed.to_csv(REPORT_DIR / "pair_cap_suppressed_trades.csv", index=False)
    pre_cap_day_comparison.to_csv(REPORT_DIR / "daily_path_comparison_pre_cap.csv", index=False)
    day_comparison.to_csv(REPORT_DIR / "daily_path_comparison.csv", index=False)
    raw_trade_comparison.to_csv(REPORT_DIR / "raw_filled_trade_comparison.csv", index=False)
    control_trade_comparison.to_csv(REPORT_DIR / "common_envelope_filled_trade_comparison.csv", index=False)
    archived_check.to_csv(REPORT_DIR / "previous_engine_archive_check.csv", index=False)
    write_report(
        summary,
        pair_cap_summary,
        pair_cap_suppressed,
        pre_cap_day_comparison,
        day_comparison,
        raw_trade_comparison,
        control_trade_comparison,
        integrity,
        archived_check,
        time.perf_counter() - started,
    )
    print(f"Wrote: {REPORT_DIR}")
    print(f"Runtime seconds: {time.perf_counter() - started:.1f}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_active_configs(loaded: dict[tuple[str, str], pd.DataFrame]) -> dict[str, object]:
    # Active first-30 limits are loaded from the versioned pre-2025 artifact. The
    # supplied frames are execution data only and never recalibrate these limits.
    thresholds = filters.build_thresholds()
    lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}
    configs: dict[str, object] = {}
    for leg in filters.build_leg_specs():
        if leg.system != "phase_selected":
            continue
        variant = "first30_q60"
        spec = filters.VariantSpec(
            variant,
            variant,
            "engine",
            first30_quantile=manual_v2.variant_quantile(variant),
        )
        configs[leg.leg_key] = manual_v2.manual_v2_config(
            filters.build_variant_config(leg, spec, lookup)
        )
    if set(configs) != set(SYMBOLS):
        raise RuntimeError(f"Missing active configs: {set(SYMBOLS) - set(configs)}")
    return configs


def select_window(frame: pd.DataFrame) -> pd.DataFrame:
    start = START_DATE.tz_localize("America/New_York") - pd.Timedelta(days=7)
    end = (END_DATE + pd.Timedelta(days=4)).tz_localize("America/New_York")
    return frame[(frame["time"] >= start) & (frame["time"] < end)].copy()


def within_period(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    if column == "date":
        values = pd.to_datetime(frame[column])
    else:
        values = pd.to_datetime(frame[column], utc=True, format="mixed").dt.tz_convert("America/New_York").dt.tz_localize(None)
    mask = (values.dt.normalize() >= START_DATE) & (values.dt.normalize() <= END_DATE)
    return frame[mask].reset_index(drop=True)


def tag(frame: pd.DataFrame, leg_key: str, engine: str) -> pd.DataFrame:
    frame = frame.copy()
    if frame.empty:
        return frame
    frame.insert(0, "engine", engine)
    frame.insert(1, "leg_key", leg_key)
    return frame


def concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True) if nonempty else pd.DataFrame()


def build_eligibility(frame, config, dates, leg_key: str) -> list[dict[str, object]]:
    allowed = set(config.allowed_weekdays.split(",")) if config.allowed_weekdays != "all" else set()
    rows = []
    for trade_date in dates:
        timestamp = pd.Timestamp(trade_date)
        weekday_ok = not allowed or timestamp.day_name() in allowed
        first30 = compute_first30_range(frame, trade_date)
        first30_ok = (
            config.first30_range_filter != "live_safe_max"
            or first30 is None
            or float(first30) <= float(config.first30_range_max)
        )
        trade_start = pd.Timestamp(f"{trade_date} 09:30", tz="America/New_York")
        has_open_data = bool((frame["time"] == trade_start).any())
        rows.append(
            {
                "leg_key": leg_key,
                "symbol": config.symbol,
                "date": str(trade_date),
                "date_value": trade_date,
                "weekday": timestamp.day_name(),
                "weekday_ok": weekday_ok,
                "first30_range": None if first30 is None else round(float(first30), 6),
                "first30_max": config.first30_range_max,
                "first30_ok": first30_ok,
                "has_0930_data": has_open_data,
                "eligible_common_envelope": weekday_ok and first30_ok and has_open_data,
            }
        )
    return rows


def build_day_diagnostics(days, leg_key: str, engine: str) -> list[dict[str, object]]:
    rows = []
    symbol = SYMBOLS[leg_key][0]
    for day in days:
        decisions = day.decisions
        rows.append(
            {
                "engine": engine,
                "leg_key": leg_key,
                "symbol": symbol,
                "date": day.trade_date,
                "has_profile": day.vah is not None and day.val is not None,
                "context_count": len(day.context_triggers),
                "qualified_cisd_count": sum(
                    item.lifecycle_state != "INTERNAL_OPPOSITE" for item in day.cisd_qualifications
                ),
                "thesis_count": len(day.thesis_episodes),
                "decision_count": len(decisions),
                "filled_count": sum(item.order_state == "FILLED" for item in decisions),
                "cancelled_count": sum(item.order_state == "CANCELLED" for item in decisions),
                "cancel_reasons": "|".join(
                    sorted({item.terminal_reason for item in decisions if item.order_state == "CANCELLED"})
                ),
            }
        )
    return rows


def build_integrity_checks(decisions: pd.DataFrame, frame, config, leg_key: str) -> list[dict[str, object]]:
    if decisions.empty:
        return []
    filled = decisions[decisions["order_state"] == "FILLED"].copy()
    rows = []
    cost = config.spread_points / 2 + config.slippage_points
    indexed = frame.set_index("time")
    for _, item in filled.iterrows():
        entry_time = pd.Timestamp(item["entry_time"])
        cisd_time = pd.Timestamp(item["cisd_time"])
        fvg_time = pd.Timestamp(item["fvg_time"])
        terminal_time = pd.Timestamp(item["terminal_time"])
        candle = indexed.loc[entry_time] if entry_time in indexed.index else None
        if isinstance(candle, pd.DataFrame):
            candle = candle.iloc[0]
        adjusted_entry = float(item["entry_price"])
        raw_entry = adjusted_entry - cost if item["direction"] == "long" else adjusted_entry + cost
        candle_touch = (
            candle is not None
            and float(candle["low"]) - 1e-9 <= raw_entry <= float(candle["high"]) + 1e-9
        )
        entry = adjusted_entry
        stop = float(item["stop_price"])
        target = float(item["target_price"])
        price_order_ok = stop < entry < target if item["direction"] == "long" else target < entry < stop
        risk = abs(entry - stop)
        actual_r = abs(target - entry) / risk if risk else float("nan")
        reward_ok = abs(actual_r - float(config.reward_r)) <= 1e-5
        chronology_ok = cisd_time <= fvg_time < entry_time <= terminal_time
        rows.append(
            {
                "leg_key": leg_key,
                "symbol": item["symbol"],
                "date": item["date"],
                "direction": item["direction"],
                "cisd_time": item["cisd_time"],
                "fvg_time": item["fvg_time"],
                "entry_time": item["entry_time"],
                "raw_trigger_price": round(raw_entry, 6),
                "adjusted_entry_price": adjusted_entry,
                "entry_candle_low": None if candle is None else float(candle["low"]),
                "entry_candle_high": None if candle is None else float(candle["high"]),
                "entry_candle_touched_raw_trigger": candle_touch,
                "chronology_ok": chronology_ok,
                "price_order_ok": price_order_ok,
                "reward_r_actual": round(actual_r, 6),
                "reward_r_ok": reward_ok,
                "all_checks_ok": candle_touch and chronology_ok and price_order_ok and reward_ok,
            }
        )
    return rows


def normalize_old_filled(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    result["outcome"] = result["result"].map({"win": "TP", "loss": "SL", "loss_same_bar": "SL", "breakeven": "BE"}).fillna("OPEN")
    result["engine"] = "previous_challenge_core_v2"
    return result


def normalize_updated_filled(decisions: pd.DataFrame, configs: dict[str, object]) -> pd.DataFrame:
    if decisions.empty:
        return pd.DataFrame()
    result = decisions[decisions["order_state"] == "FILLED"].copy()
    if result.empty:
        return result
    del configs
    exact = pd.to_numeric(result["r_multiple"], errors="coerce")
    closed = result["outcome"].isin(["TP", "SL", "BE"])
    if exact[closed].isna().any():
        raise ValueError("Closed updated-engine decisions must carry exact R multiples.")
    result["mark_to_market_r_multiple"] = exact
    result["r_multiple"] = exact.where(closed)
    return result


def apply_pair_cap(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    capped = frame.copy()
    capped["group"] = capped["engine"]
    capped["label"] = capped["leg_key"].str.upper()
    exact = pd.to_numeric(capped["r_multiple"], errors="coerce")
    closed = capped["outcome"].isin(["TP", "SL", "BE"])
    if exact[closed].isna().any():
        raise ValueError("Closed comparison decisions must carry exact R multiples.")
    capped["_realized_r_multiple"] = exact.where(closed, 0.0)
    return apply_pair_risk_rule(capped, -1.0, r_col="_realized_r_multiple").drop(
        columns="_realized_r_multiple"
    )


def build_pair_cap_summary(pre_cap: dict[str, pd.DataFrame], post_cap: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for engine, before in pre_cap.items():
        after = post_cap[engine]
        rows.append(
            {
                "engine": engine,
                "filled_pre_cap": len(before),
                "filled_post_cap": len(after),
                "suppressed_by_pair_cap": len(before) - len(after),
                "nq_post_cap": int((after.get("leg_key", pd.Series(dtype=str)) == "nq").sum()),
                "spx_post_cap": int((after.get("leg_key", pd.Series(dtype=str)) == "spx").sum()),
            }
        )
    return pd.DataFrame(rows)


def build_pair_cap_suppressed(pre_cap: dict[str, pd.DataFrame], post_cap: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for engine, before in pre_cap.items():
        after = post_cap[engine]
        kept = set(zip(after.get("symbol", []), after.get("date", []), after.get("entry_time", [])))
        for _, item in before.iterrows():
            key = (item["symbol"], item["date"], item["entry_time"])
            if key in kept:
                continue
            rows.append(
                {
                    "engine": engine,
                    "symbol": item["symbol"],
                    "date": item["date"],
                    "direction": item["direction"],
                    "entry_time": item["entry_time"],
                    "outcome": item["outcome"],
                    "r_multiple": item["r_multiple"],
                    "reason": "SUPPRESSED_AFTER_EARLIER_PAIR_RESULT_REACHED_DAILY_MINUS_1R",
                }
            )
    return pd.DataFrame(rows)


def build_summary(old_trades, old_lifecycles, raw, control, capped, business_dates) -> pd.DataFrame:
    rows = []
    specs = [
        ("previous_challenge_core_v2", old_lifecycles, old_trades),
        ("updated_state_raw", raw, raw[raw.get("order_state", "") == "FILLED"] if not raw.empty else raw),
        (
            "updated_state_common_envelope",
            control,
            control[control.get("order_state", "") == "FILLED"] if not control.empty else control,
        ),
    ]
    for engine, orders, filled in specs:
        if engine == "previous_challenge_core_v2":
            cancelled = int((orders.get("order_state", pd.Series(dtype=str)) == "CANCELLED").sum())
        else:
            cancelled = int((orders.get("order_state", pd.Series(dtype=str)) == "CANCELLED").sum())
        post = capped[engine]
        rows.append(
            {
                "engine": engine,
                "calendar_business_days_per_symbol": len(business_dates),
                "valid_order_records": len(orders),
                "cancelled_orders": cancelled,
                "filled_pre_cap": len(filled),
                "filled_post_cap": len(post),
                "long_post_cap": int((post.get("direction", pd.Series(dtype=str)) == "long").sum()),
                "short_post_cap": int((post.get("direction", pd.Series(dtype=str)) == "short").sum()),
                "tp_observed": int((post.get("outcome", pd.Series(dtype=str)) == "TP").sum()),
                "sl_observed": int((post.get("outcome", pd.Series(dtype=str)) == "SL").sum()),
                "open_or_be_observed": int(post.get("outcome", pd.Series(dtype=str)).isin(["OPEN", "BE"]).sum()),
            }
        )
    return pd.DataFrame(rows)


def build_day_comparison(capped, diagnostics, eligibility, old_lifecycles) -> pd.DataFrame:
    grid = pd.MultiIndex.from_product(
        [[value[0] for value in SYMBOLS.values()], pd.bdate_range(START_DATE, END_DATE).strftime("%Y-%m-%d")],
        names=["symbol", "date"],
    ).to_frame(index=False)
    for engine, frame in capped.items():
        grouped = summarize_filled_days(frame, engine)
        grid = grid.merge(grouped, on=["symbol", "date"], how="left")
    eligibility_small = eligibility[["symbol", "date", "weekday_ok", "first30_ok", "eligible_common_envelope"]]
    grid = grid.merge(eligibility_small, on=["symbol", "date"], how="left")
    raw_diag = diagnostics[diagnostics["engine"] == "updated_state_raw"].drop(columns=["engine", "leg_key"])
    grid = grid.merge(raw_diag, on=["symbol", "date"], how="left")
    old_cancel = old_lifecycles[old_lifecycles.get("order_state", "") == "CANCELLED"].groupby(["symbol", "date"])["terminal_reason"].agg(lambda s: "|".join(sorted(set(s)))).rename("old_cancel_reasons").reset_index() if not old_lifecycles.empty else pd.DataFrame(columns=["symbol", "date", "old_cancel_reasons"])
    grid = grid.merge(old_cancel, on=["symbol", "date"], how="left")
    for engine in capped:
        count_col = f"{engine}_count"
        direction_col = f"{engine}_directions"
        grid[count_col] = grid[count_col].fillna(0).astype(int)
        grid[direction_col] = grid[direction_col].fillna("")
    grid["raw_relation"] = grid.apply(lambda row: relation(row, "updated_state_raw"), axis=1)
    grid["control_relation"] = grid.apply(lambda row: relation(row, "updated_state_common_envelope"), axis=1)
    grid["raw_difference_reason"] = grid.apply(classify_raw_difference, axis=1)
    return grid


def summarize_filled_days(frame: pd.DataFrame, engine: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "date", f"{engine}_count", f"{engine}_directions", f"{engine}_entry_times"])
    data = frame.copy()
    data["entry_clock"] = pd.to_datetime(data["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York").dt.strftime("%H:%M")
    return data.sort_values("entry_time").groupby(["symbol", "date"]).agg(
        **{
            f"{engine}_count": ("direction", "size"),
            f"{engine}_directions": ("direction", lambda s: "|".join(s)),
            f"{engine}_entry_times": ("entry_clock", lambda s: "|".join(s)),
        }
    ).reset_index()


def relation(row: pd.Series, updated_engine: str) -> str:
    old_count = int(row["previous_challenge_core_v2_count"])
    new_count = int(row[f"{updated_engine}_count"])
    if old_count and new_count:
        return "BOTH_SAME_DIRECTION" if row["previous_challenge_core_v2_directions"] == row[f"{updated_engine}_directions"] else "BOTH_PATH_DIFFERENT"
    if old_count:
        return "PREVIOUS_ONLY"
    if new_count:
        return "UPDATED_ONLY"
    return "NO_FILLED_TRADE"


def classify_raw_difference(row: pd.Series) -> str:
    relation_value = row["raw_relation"]
    if relation_value == "UPDATED_ONLY":
        if not bool(row.get("weekday_ok")):
            return "PREVIOUS_WEEKDAY_GATE"
        if not bool(row.get("first30_ok")):
            return "PREVIOUS_FIRST30_GATE"
        if str(row.get("old_cancel_reasons", "")) not in {"", "nan"}:
            return "PREVIOUS_ORDER_CANCELLED:" + str(row["old_cancel_reasons"])
        return "STRUCTURAL_PATH_DIFFERENCE"
    if relation_value == "PREVIOUS_ONLY":
        if int(row.get("cancelled_count") or 0):
            return "UPDATED_ORDER_CANCELLED:" + str(row.get("cancel_reasons", ""))
        if int(row.get("context_count") or 0) == 0:
            return "UPDATED_NO_CONTEXT_AUTHORITY"
        if int(row.get("thesis_count") or 0) == 0:
            return "UPDATED_NO_QUALIFIED_CISD_THESIS"
        if int(row.get("decision_count") or 0) == 0:
            return "UPDATED_NO_ENTRY_ARRAY_OR_INVALID_RISK"
        return "UPDATED_NO_FILL_OTHER"
    if relation_value == "BOTH_PATH_DIFFERENT":
        return "DIRECTION_COUNT_OR_SEQUENCE_DIFFERENT"
    return ""


def match_filled_trades(old: pd.DataFrame, updated: pd.DataFrame, updated_name: str) -> pd.DataFrame:
    left = add_trade_number(old)
    right = add_trade_number(updated)
    keys = ["symbol", "date", "trade_number"]
    left_cols = keys + ["direction", "entry_time", "entry_price", "stop_price", "target_price", "outcome"]
    right_cols = keys + ["direction", "entry_time", "entry_price", "stop_price", "target_price", "outcome"]
    merged = left[left_cols].merge(right[right_cols], on=keys, how="outer", suffixes=("_previous", "_updated"), indicator=True)
    merged["updated_engine"] = updated_name
    merged["direction_same"] = merged["direction_previous"] == merged["direction_updated"]
    previous_time = pd.to_datetime(merged["entry_time_previous"], utc=True, format="mixed", errors="coerce")
    updated_time = pd.to_datetime(merged["entry_time_updated"], utc=True, format="mixed", errors="coerce")
    merged["entry_time_delta_minutes"] = (updated_time - previous_time).dt.total_seconds().div(60)
    for field in ["entry_price", "stop_price", "target_price"]:
        merged[f"{field}_delta_points"] = merged[f"{field}_updated"] - merged[f"{field}_previous"]
    merged["relation"] = merged["_merge"].map({"both": "BOTH", "left_only": "PREVIOUS_ONLY", "right_only": "UPDATED_ONLY"})
    return merged.drop(columns=["_merge"])


def add_trade_number(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "date", "trade_number", "direction", "entry_time", "entry_price", "stop_price", "target_price", "outcome"])
    result = frame.sort_values(["symbol", "date", "entry_time"]).copy()
    result["trade_number"] = result.groupby(["symbol", "date"]).cumcount() + 1
    return result


def compare_with_archived(generated: pd.DataFrame) -> pd.DataFrame:
    archived = pd.read_csv(ARCHIVED_OLD_TRADES)
    archived = archived[
        (archived["system"] == "phase_selected")
        & (archived["manual_variant"] == "manual_v2")
        & (archived["date"] >= START_DATE.strftime("%Y-%m-%d"))
        & (archived["date"] <= END_DATE.strftime("%Y-%m-%d"))
    ].copy()
    keys = ["symbol", "date", "direction", "entry_time", "entry_price", "stop_price", "target_price"]
    generated_keys = generated[keys].round({"entry_price": 6, "stop_price": 6, "target_price": 6}).drop_duplicates()
    archived_keys = archived[keys].round({"entry_price": 6, "stop_price": 6, "target_price": 6}).drop_duplicates()
    merged = archived_keys.merge(generated_keys, on=keys, how="outer", indicator=True)
    return pd.DataFrame(
        [
            {
                "archived_trade_count": len(archived_keys),
                "generated_trade_count": len(generated_keys),
                "exact_matches": int((merged["_merge"] == "both").sum()),
                "archived_only": int((merged["_merge"] == "left_only").sum()),
                "generated_only": int((merged["_merge"] == "right_only").sum()),
                "archive_reproduction_exact": bool((merged["_merge"] == "both").all()),
            }
        ]
    )


def write_report(
    summary,
    pair_cap,
    pair_cap_suppressed,
    pre_cap_day_comparison,
    day_comparison,
    raw_compare,
    control_compare,
    integrity,
    archived,
    runtime,
) -> None:
    raw_relations_pre_cap = relation_counts(pre_cap_day_comparison, "raw_relation")
    control_relations_pre_cap = relation_counts(pre_cap_day_comparison, "control_relation")
    raw_relations = relation_counts(day_comparison, "raw_relation")
    control_relations = relation_counts(day_comparison, "control_relation")
    difference_reasons = (
        day_comparison[day_comparison["raw_difference_reason"] != ""]
        .groupby("raw_difference_reason")
        .size()
        .rename("symbol_days")
        .reset_index()
        .sort_values("symbol_days", ascending=False)
    )
    both_raw = raw_compare[raw_compare["relation"] == "BOTH"]
    both_control = control_compare[control_compare["relation"] == "BOTH"]
    integrity_ok = int(integrity["all_checks_ok"].sum()) if not integrity.empty else 0
    integrity_total = len(integrity)
    lines = [
        "# Previous Engine vs Updated State Engine — 2025-02-01 to 2025-03-31",
        "",
        "## Scope",
        "",
        "- Same DukasCopy OHLC data: NQ 3m and SPX 5m.",
        "- Previous engine: active CHALLENGE_CORE_V2 leg configs, including manual_v2 gates.",
        "- Updated raw engine: research state machine, 09:30-12:00, business-day calendar; it does not inherit the active candidate weekday/first-30 envelope automatically.",
        "- Common-envelope control: updated state machine restricted to the previous engine's weekday + first-30 eligibility and previous trade-window end.",
        "- NQ+SPX daily -1R pair cap is applied to filled trades in all three views.",
        "- TP/SL is reported only as an observed terminal state; it is not used as setup-quality evidence.",
        "",
        "## Engine Summary",
        "",
        report_utils.markdown_table(summary),
        "",
        "## Pair-cap effect",
        "",
        report_utils.markdown_table(pair_cap),
        "",
        "Suppressed fills:",
        "",
        report_utils.markdown_table(pair_cap_suppressed),
        "",
        "## Signal-level filled-day overlap before pair cap",
        "",
        "Previous vs updated raw:",
        "",
        report_utils.markdown_table(raw_relations_pre_cap),
        "",
        "Previous vs common-envelope control:",
        "",
        report_utils.markdown_table(control_relations_pre_cap),
        "",
        "## Executable filled-day overlap after pair cap: previous vs updated raw",
        "",
        report_utils.markdown_table(raw_relations),
        "",
        "## Executable filled-day overlap after pair cap: previous vs common-envelope control",
        "",
        report_utils.markdown_table(control_relations),
        "",
        "## Why paths differed (raw updated view)",
        "",
        report_utils.markdown_table(difference_reasons),
        "",
        "## Real-fill integrity",
        "",
        f"Updated raw filled trades passing candle-touch, chronology, direction-price ordering and configured-R checks: **{integrity_ok}/{integrity_total}**.",
        "",
        "The candle-touch check removes spread/slippage from the adjusted execution price and verifies that the underlying trigger price was inside the recorded entry candle's high-low range.",
        "",
        "## Same-day trade value comparison",
        "",
        f"Raw updated view had {len(both_raw)} sequence-matched same-symbol/same-day fills; {int(both_raw['direction_same'].sum()) if len(both_raw) else 0} had the same direction.",
        f"Common-envelope control had {len(both_control)} sequence-matched same-symbol/same-day fills; {int(both_control['direction_same'].sum()) if len(both_control) else 0} had the same direction.",
        "",
        "Entry/stop/target point deltas and entry-time deltas are in the two filled-trade comparison CSV files. A price delta is descriptive, not an error, because the engines form CISD/FVG and stops differently.",
        "",
        "## Previous-engine reproduction check",
        "",
        report_utils.markdown_table(archived),
        "",
        "## Interpretation",
        "",
        "The raw updated state machine is not a drop-in replacement for CHALLENGE_CORE_V2. Its research runner owns a wider calendar/window envelope and a different context/CISD/pending-order path. The common-envelope control isolates those envelope effects from structure-engine effects.",
        "",
        "A promotion decision should require: (1) 100% real-fill integrity, (2) deterministic rerun equality, (3) explicit ownership of weekday/first-30/max-trades/pair-cap gates, and (4) review of structural old-only and updated-only days. Profit is not a promotion criterion in this comparison.",
        "",
        f"Runtime: {runtime:.1f} seconds.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def relation_counts(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    return frame.groupby(column).size().rename("symbol_days").reset_index().sort_values("symbol_days", ascending=False)


if __name__ == "__main__":
    main()
