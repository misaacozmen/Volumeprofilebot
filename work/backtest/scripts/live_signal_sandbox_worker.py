"""Fixed live-signal worker; accepts no paths, modules, commands, or callable names."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from backtest.config import SymbolConfig
from backtest.data_inspector import parse_timeframe_minutes
from backtest.engine_pipeline import EngineLeg, run_canonical_pair_pipeline
from backtest.integrity import DayDataIntegrity, assess_manual_state_day as baseline_assess, find_gap_events
import backtest.manual_state as manual_state_module
from backtest.live_signal_protocol import LEG_KEYS, SCHEMA_VERSION, canonical_hash, canonical_json_bytes, validate_request
from backtest.manual_state import (
    ManualStateConfig, cisd_qualifications_to_frame, context_authority_to_frame,
    context_triggers_to_frame, htf_arrays_to_frame, liquidity_selections_to_frame,
    premarket_contexts_to_frame, thesis_episodes_to_frame,
)
from backtest.state_audit import state_snapshot
from backtest.strategy import cluster_swings


def _deny(event, args):
    if event == "import" and str(args[0]).startswith(("backtest.live", "MetaTrader5", "run_capital_forward", "run_xm_mt5_forward", "run_super1_xm_mt5_forward", "backtest.cli")):
        raise PermissionError("live/broker/CLI import denied")
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "ctypes.dlopen"}:
        raise PermissionError("network/native/child-process operation denied")
    if ((event == "compile" and len(args) > 1 and str(args[1]) == "<string>") or
            (event == "exec" and getattr(args[0], "co_filename", "") == "<string>")):
        caller = Path(sys._getframe(1).f_code.co_filename).resolve()
        if caller == Path(__file__).resolve() or PROJECT_ROOT == caller or PROJECT_ROOT in caller.parents:
            raise PermissionError("dynamic code execution denied")


def _records(frame: pd.DataFrame) -> list[dict[str, object]]:
    if frame.empty:
        return []
    clean = frame.astype(object).where(pd.notna(frame), None)
    return [{str(key): value for key, value in row.items()} for row in clean.to_dict("records")]


def _no_trade_reason(day: object) -> str:
    if day.data_state == "INVALID":
        return "DATA_INVALID:" + "|".join(day.data_reasons)
    if day.decisions:
        return ""
    blocked = [item for item in day.pipeline if item.status == "BLOCKED"]
    source = blocked[-1] if blocked else (day.pipeline[-1] if day.pipeline else None)
    return "NO_DECISION" if source is None else f"{source.stage}:{source.reason}"


def _decision_rows(result) -> list[dict[str, object]]:
    rows = _records(result.decisions)
    for row in rows:
        pair_state = str(row.get("pair_risk_state") or "NOT_APPLICABLE")
        row["pair_cap_state"] = pair_state
        row["shadow_outcome"] = "AMBIGUOUS" if row.get("intrabar_ambiguity") else (row.get("outcome") or None)
        row["shadow_action"] = "WATCH" if row.get("intrabar_ambiguity") else ("SUPPRESSED" if pair_state == "SUPPRESSED_DAILY_CAP" else row.get("final_decision"))
    return rows


def _events(result, configs: dict[str, SymbolConfig]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for leg_key in LEG_KEYS:
        day = result.leg_results[leg_key].days[0]
        duration = pd.Timedelta(minutes=int(parse_timeframe_minutes(configs[leg_key].timeframe) or 0))
        for decision in day.decisions:
            setup_bar = decision.fvg_time or decision.cisd_time
            setup_known = decision.fvg_known_time or decision.cisd_known_time
            terminal_bar = decision.terminal_time or setup_bar
            terminal_known = decision.terminal_known_time or setup_known
            for kind, bar, known, state, reason in (
                ("SETUP", setup_bar, setup_known, decision.setup_state, decision.fvg_kind or decision.terminal_reason),
                ("ORDER", setup_bar, setup_known, decision.order_state, decision.terminal_reason),
                ("FINAL", terminal_bar, terminal_known, decision.final_decision, decision.terminal_reason or "NO_TERMINAL"),
            ):
                if not bar or not known or pd.Timestamp(known) < pd.Timestamp(bar):
                    raise ValueError("decision event has invalid causal times")
                rows.append({"leg_key": leg_key, "event_type": kind, "bar_time": pd.Timestamp(bar).isoformat(), "known_time": pd.Timestamp(known).isoformat(), "entity_id": decision.order_id, "state": state, "direction": decision.direction, "reason": reason or ""})
        trade_start = pd.Timestamp(f"{day.trade_date} {configs[leg_key].trade_window_start}", tz="America/New_York")
        rows.append({"leg_key": leg_key, "event_type": "VALUE_AREA", "bar_time": trade_start.isoformat(), "known_time": trade_start.isoformat(), "entity_id": "", "state": day.data_state, "direction": "", "reason": ""})
    order = {"nq": 0, "spx": 1}
    rows.sort(key=lambda row: (pd.Timestamp(row["known_time"]), order[row["leg_key"]], row["event_type"], row["entity_id"]))
    for sequence, row in enumerate(rows, 1):
        row["sequence"] = sequence
    return rows


def _causal_swing_touch_evidence(frame: pd.DataFrame, trade_date, config: SymbolConfig) -> dict[str, int]:
    trade_start = pd.Timestamp(f"{trade_date} {config.trade_window_start}", tz="America/New_York")
    history = frame[frame["time"] < trade_start].tail(config.swing_lookback_candles).reset_index(drop=True)
    if len(history) < 5:
        return {}
    output: dict[str, int] = {}
    for side, price_field, comparator in (("high", "high", "max"), ("low", "low", "min")):
        swings: list[tuple[pd.Timestamp, float]] = []
        for index in range(2, len(history) - 2):
            candle = history.iloc[index]
            left, right = history.iloc[index - 2:index], history.iloc[index + 1:index + 3]
            price = float(candle[price_field])
            selected = (price > float(left[price_field].max()) and price >= float(right[price_field].max())) if comparator == "max" else (price < float(left[price_field].min()) and price <= float(right[price_field].min()))
            if selected:
                swings.append((pd.Timestamp(candle.time), price))
        clustered = cluster_swings(swings, config.equal_swing_tolerance)
        if config.swing_liquidity_mode == "strong_only":
            clustered = [item for item in clustered if item[2] >= config.strong_swing_min_touches]
        for number, (_, _, touches) in enumerate(clustered[-5:], start=1):
            output[f"swing_{side}_{number}"] = int(touches)
    return output


def evaluate(request: dict[str, object]) -> dict[str, object]:
    validate_request(request)
    configs = {key: SymbolConfig(**request["configs"][key]) for key in LEG_KEYS}
    state = ManualStateConfig(**request["state_config"])
    frames: dict[str, pd.DataFrame] = {}
    for key in LEG_KEYS:
        frame = pd.DataFrame(request["legs"][key]["bars"])
        frame["time"] = pd.to_datetime(frame["time"], utc=True).dt.tz_convert("America/New_York")
        frame["known_time"] = pd.to_datetime(frame["known_time"], utc=True).dt.tz_convert("America/New_York")
        frames[key] = frame
    trade_date = pd.Timestamp(request["trade_date"]).date()
    allowed_by_timeframe = {
        configs[key].timeframe: [
            (pd.Timestamp(item["first"]), pd.Timestamp(item["last"]))
            for item in request["scheduled_closed_ranges"][key]
        ]
        for key in LEG_KEYS
    }

    def scheduled_assess(frame, day, timeframe, profile_start, trade_end):
        result = baseline_assess(frame, day, timeframe, profile_start, trade_end)
        if result.valid:
            return result
        minutes = int(parse_timeframe_minutes(timeframe) or 5)
        relevant = frame[(frame["time"] >= profile_start) & (frame["time"] <= trade_end)]
        gaps = {event.time.isoformat(): event for event in find_gap_events(relevant, expected_minutes=minutes)}
        ranges = allowed_by_timeframe.get(timeframe, [])
        retained = []
        for issue in result.issues:
            event = gaps.get(issue.time)
            if issue.code != "MISSING_BARS" or event is None:
                retained.append(issue)
                continue
            missing = pd.date_range(event.prev_time + pd.Timedelta(minutes=minutes), event.time - pd.Timedelta(minutes=minutes), freq=f"{minutes}min")
            if not len(missing) or not all(any(first <= timestamp <= last for first, last in ranges) for timestamp in missing):
                retained.append(issue)
        return DayDataIntegrity(result.trade_date, not retained, tuple(retained))

    manual_state_module.assess_manual_state_day = scheduled_assess
    result = run_canonical_pair_pipeline([EngineLeg(key, frames[key], configs[key]) for key in LEG_KEYS], [trade_date], state_config=state)
    lifecycle: dict[str, object] = {}
    days: list[dict[str, object]] = []
    snapshots: dict[str, object] = {}
    for key in LEG_KEYS:
        leg_days = result.leg_results[key].days
        day = leg_days[0]
        lifecycle[key] = {
            "cisd": _records(cisd_qualifications_to_frame(leg_days)),
            "htf_arrays": _records(htf_arrays_to_frame(leg_days)),
            "context_triggers": _records(context_triggers_to_frame(leg_days)),
            "premarket": _records(premarket_contexts_to_frame(leg_days)),
            "liquidity": _records(liquidity_selections_to_frame(leg_days)),
            "context_authority": _records(context_authority_to_frame(leg_days)),
            "theses": _records(thesis_episodes_to_frame(leg_days)),
        }
        days.append({
            "leg_key": key, "date": day.trade_date, "data_state": day.data_state,
            "data_reasons": list(day.data_reasons), "vah": day.vah, "val": day.val,
            "premarket_context": None if day.premarket_context is None else asdict(day.premarket_context),
            "liquidity": None if day.liquidity_selection is None else asdict(day.liquidity_selection),
            "controlling_array_id": None if day.premarket_context is None else (day.premarket_context.controlling_array_id or None),
            "no_trade_reason": _no_trade_reason(day),
        })
        snapshots[key] = state_snapshot(day, pd.Timestamp(request["cutoffs"][key]))
    response: dict[str, object] = {
        "schema_version": SCHEMA_VERSION, "request_hash": request["request_hash"],
        "decisions": _decision_rows(result), "events": _events(result, configs),
        "lifecycle": lifecycle, "days": days, "state_snapshots": snapshots,
        "graph_features": {key: _causal_swing_touch_evidence(frames[key], trade_date, configs[key]) for key in LEG_KEYS},
    }
    response["semantic_output_hash"] = canonical_hash(response)
    return response


def main() -> int:
    try:
        envelope = json.loads(sys.stdin.buffer.read(16 * 1024 * 1024 + 1))
        if not isinstance(envelope, dict) or set(envelope) != {"action", "request", "limits"} or envelope.get("action") != "live_signal":
            raise ValueError("worker envelope schema is closed")
        sys.addaudithook(_deny)
        result = evaluate(envelope["request"])
        outer = {"ok": True, "result": result}
        outer["response_hash"] = sha256(canonical_json_bytes(outer)).hexdigest()
        encoded = canonical_json_bytes(outer)
        if len(encoded) > 16 * 1024 * 1024:
            raise ValueError("live signal response exceeds 16 MB")
        sys.stdout.buffer.write(encoded)
        return 0
    except BaseException as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
