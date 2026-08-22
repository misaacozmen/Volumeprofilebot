from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any, Iterable
from uuid import uuid4

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.config import SymbolConfig
from backtest.data_inspector import infer_symbol_timeframe, normalize_columns, parse_timeframe_minutes
from backtest.engine_pipeline import (
    EngineLeg,
    feed_name,
    run_canonical_pair_pipeline,
    source_code_hash,
    stable_frame_hash,
)
from backtest.manual_state import (
    ManualStateConfig,
    context_authority_to_frame,
    context_triggers_to_frame,
    cisd_qualifications_to_frame,
    decisions_to_frame,
    htf_arrays_to_frame,
    liquidity_selections_to_frame,
    premarket_contexts_to_frame,
    run_manual_state_day,
    thesis_episodes_to_frame,
)
from backtest.state_audit import state_snapshot


TZ = "America/New_York"
LOCK_DIR = ROOT / "forward_shadow"
BASELINE_LOCK = LOCK_DIR / "baseline_lock.json"
FROZEN_CONFIG = LOCK_DIR / "frozen_config.json"
DEFAULT_OUTPUT = ROOT / "outputs" / "forward_shadow" / "nq3m_spx5m"
OHLCV = ["open", "high", "low", "close", "volume"]
LEG_ORDER = {"nq": 0, "spx": 1}


class CriticalShadowError(RuntimeError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def object_hash(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_new_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        handle.write("\n")


def write_new_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")


def normalized_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    if frame.empty:
        return []
    clean = frame.astype(object).where(pd.notna(frame), None)
    return [
        {str(key): value for key, value in row.items()}
        for row in clean.to_dict(orient="records")
    ]


def frozen_objects() -> tuple[dict[str, SymbolConfig], ManualStateConfig, dict[str, object]]:
    payload = read_json(FROZEN_CONFIG)
    configs = {key: SymbolConfig(**value) for key, value in payload["legs"].items()}
    return configs, ManualStateConfig(**payload["state"]), payload


def frozen_config_hash(payload: dict[str, object]) -> str:
    return sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def harness_hash() -> str:
    digest = sha256()
    for path in [Path(__file__).resolve(), BASELINE_LOCK.resolve(), FROZEN_CONFIG.resolve()]:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def verify_baseline() -> tuple[dict[str, object], dict[str, SymbolConfig], ManualStateConfig]:
    lock = read_json(BASELINE_LOCK)
    baseline_path = ROOT / str(lock["baseline_manifest_path"])
    if not baseline_path.exists():
        raise CriticalShadowError(f"Baseline manifest missing: {baseline_path}")
    if file_hash(baseline_path) != lock["baseline_manifest_sha256"]:
        raise CriticalShadowError("Baseline manifest file hash changed; test is invalid.")
    manifest = read_json(baseline_path)
    if manifest != lock["engine_manifest"]:
        raise CriticalShadowError("Baseline manifest content changed; test is invalid.")
    if source_code_hash() != manifest["code_hash"]:
        raise CriticalShadowError("Engine code hash differs from frozen baseline; test is invalid.")
    configs, state_config, payload = frozen_objects()
    if frozen_config_hash(payload) != manifest["config_hash"]:
        raise CriticalShadowError("Frozen config hash differs from baseline; test is invalid.")
    expected = {
        "nq": ("DUKASCOPY_USATECHIDXUSD", "3m"),
        "spx": ("DUKASCOPY_USA500IDXUSD", "5m"),
    }
    actual = {key: (value.symbol, value.timeframe) for key, value in configs.items()}
    if actual != expected or state_config.htf_timeframe_minutes != 15:
        raise CriticalShadowError(f"Frozen symbol/timeframe contract changed: {actual}")
    return lock, configs, state_config


def campaign_lock(output_root: Path) -> dict[str, object]:
    baseline, _, _ = verify_baseline()
    path = output_root / "campaign_lock.json"
    expected = {
        "schema_version": 1,
        "created_at": None,
        "baseline_manifest_sha256": baseline["baseline_manifest_sha256"],
        "engine_code_hash": baseline["engine_manifest"]["code_hash"],
        "config_hash": baseline["engine_manifest"]["config_hash"],
        "harness_hash": harness_hash(),
        "execution": "SHADOW_ONLY_NO_ORDER_TRANSPORT",
    }
    if not path.exists():
        expected["created_at"] = utc_now().isoformat()
        write_new_json(path, expected)
        return expected
    current = read_json(path)
    comparable = {**current, "created_at": None}
    if comparable != expected:
        raise CriticalShadowError("Campaign code/config lock changed; clean forward period is required.")
    return current


def parse_asof(value: str | None) -> pd.Timestamp:
    timestamp = utc_now() if not value else pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(TZ)
    return timestamp.tz_convert(TZ)


def data_issue(code: str, message: str, **details: object) -> dict[str, object]:
    return {"code": code, "message": message, "details": details}


def read_leg_data(
    paths: list[Path],
    leg_key: str,
    config: SymbolConfig,
    trade_date: date,
    as_of: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, object]]:
    issues: list[dict[str, object]] = []
    frames: list[pd.DataFrame] = []
    observed_feeds: set[str] = set()
    observed_timeframes: set[str] = set()
    observed_symbols: set[str] = set()
    for path in paths:
        if not path.exists() or not path.is_file():
            issues.append(data_issue("MISSING_DATA_FILE", str(path)))
            continue
        symbol, timeframe = infer_symbol_timeframe(path)
        observed_symbols.add(symbol)
        observed_timeframes.add(timeframe)
        observed_feeds.add(feed_name(symbol))
        raw = normalize_columns(pd.read_csv(path))
        missing = sorted({"time", *OHLCV} - set(raw.columns))
        if missing:
            issues.append(data_issue("MISSING_COLUMNS", f"{path.name}: {','.join(missing)}"))
            continue
        frame = raw[["time", *OHLCV]].copy()
        frame["source_file"] = str(path.resolve())
        frame["time"] = pd.to_datetime(frame["time"], errors="coerce", utc=True, format="mixed").dt.tz_convert(TZ)
        for column in OHLCV:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frames.append(frame)
    if not frames:
        return pd.DataFrame(), {
            "leg_key": leg_key,
            "state": "DATA_INVALID",
            "issues": issues or [data_issue("EMPTY_FEED", "No readable rows.")],
            "excluded_unclosed_bars": 0,
        }

    frame = pd.concat(frames, ignore_index=True)
    invalid_time = int(frame["time"].isna().sum())
    invalid_ohlcv = int(frame[OHLCV].isna().any(axis=1).sum())
    if invalid_time:
        issues.append(data_issue("INVALID_TIME", f"{invalid_time} rows have invalid timestamps."))
    if invalid_ohlcv:
        issues.append(data_issue("INVALID_OHLCV", f"{invalid_ohlcv} rows have null OHLCV."))
    minutes = int(parse_timeframe_minutes(config.timeframe) or 0)
    history_start = pd.Timestamp(trade_date, tz=TZ) - pd.Timedelta(days=3)
    trade_end = pd.Timestamp(f"{trade_date} {config.trade_window_end}", tz=TZ)
    required_close = trade_end + pd.Timedelta(minutes=minutes)
    if as_of < required_close:
        issues.append(
            data_issue(
                "SESSION_NOT_CLOSED",
                "Configured trade window has not fully closed.",
                required_close=required_close.isoformat(),
                as_of=as_of.isoformat(),
            )
        )
    scope = frame[(frame["time"] >= history_start) & (frame["time"] <= trade_end)].copy()
    range_bad = (
        (scope["high"] < scope["low"])
        | (scope["open"] > scope["high"])
        | (scope["open"] < scope["low"])
        | (scope["close"] > scope["high"])
        | (scope["close"] < scope["low"])
    )
    if range_bad.any():
        issues.append(data_issue("INVALID_OHLC_RANGE", f"{int(range_bad.sum())} rows violate OHLC range."))
    duplicates = scope[scope.duplicated("time", keep=False)]
    if not duplicates.empty:
        issues.append(data_issue("DUPLICATE_BAR", f"{len(duplicates)} rows share timestamps."))
        conflicting = 0
        for _, group in duplicates.groupby("time", sort=True):
            if len(group[OHLCV].drop_duplicates()) > 1:
                conflicting += 1
        if conflicting:
            issues.append(
                data_issue("CONFLICTING_DUPLICATE_BAR", f"{conflicting} timestamps have conflicting OHLCV.")
            )

    if observed_symbols != {config.symbol}:
        issues.append(
            data_issue("SYMBOL_MISMATCH", f"Expected {config.symbol}; observed {sorted(observed_symbols)}.")
        )
    if observed_timeframes != {config.timeframe}:
        issues.append(
            data_issue(
                "TIMEFRAME_MISMATCH",
                f"Expected {config.timeframe}; observed {sorted(observed_timeframes)}.",
            )
        )
    if observed_feeds != {"DUKASCOPY"}:
        issues.append(data_issue("FEED_MISMATCH", f"Expected DUKASCOPY; observed {sorted(observed_feeds)}."))

    bar_close = frame["time"] + pd.Timedelta(minutes=minutes)
    closed = frame[bar_close <= as_of].copy()
    excluded = int((bar_close > as_of).sum())
    closed = closed.dropna(subset=["time", *OHLCV])
    closed = closed.sort_values(["time", "source_file"], kind="mergesort")
    closed = closed.drop_duplicates("time", keep="last").reset_index(drop=True)
    closed["date"] = closed["time"].dt.date

    profile_start = pd.Timestamp(trade_date, tz=TZ) - pd.Timedelta(hours=6)
    expected = pd.date_range(profile_start, trade_end, freq=f"{minutes}min")
    actual = set(closed.loc[(closed["time"] >= profile_start) & (closed["time"] <= trade_end), "time"])
    missing = [timestamp for timestamp in expected if timestamp not in actual]
    if missing:
        issues.append(
            data_issue(
                "MISSING_BARS",
                f"{len(missing)} required bars are missing.",
                first=missing[0].isoformat(),
                last=missing[-1].isoformat(),
            )
        )
    profile_actual = closed[(closed["time"] >= profile_start) & (closed["time"] < pd.Timestamp(f"{trade_date} 09:15", tz=TZ))]
    premarket_actual = closed[
        (closed["time"] >= pd.Timestamp(f"{trade_date} 09:15", tz=TZ))
        & (closed["time"] < pd.Timestamp(f"{trade_date} 09:30", tz=TZ))
    ]
    if profile_actual.empty:
        issues.append(data_issue("MISSING_PROFILE_DATA", "Profile window is empty."))
    if premarket_actual.empty:
        issues.append(data_issue("MISSING_PREMARKET_DATA", "09:15-09:30 window is empty."))

    report = {
        "leg_key": leg_key,
        "symbol": config.symbol,
        "timeframe": config.timeframe,
        "feed": "DUKASCOPY",
        "timezone": TZ,
        "as_of": as_of.isoformat(),
        "closed_bar_only": True,
        "excluded_unclosed_bars": excluded,
        "state": "VALID" if not issues else "DATA_INVALID",
        "issues": issues,
        "data_hash": stable_frame_hash(closed[["time", *OHLCV]]),
        "first_closed_bar": None if closed.empty else closed["time"].min().isoformat(),
        "last_closed_bar": None if closed.empty else closed["time"].max().isoformat(),
    }
    return closed, report


def all_feeds_match(reports: list[dict[str, object]]) -> bool:
    return {report.get("feed") for report in reports} == {"DUKASCOPY"}


def causal_pair_cap(decisions: list[dict[str, object]], configs: dict[str, SymbolConfig]) -> list[dict[str, object]]:
    rows = [dict(row) for row in decisions]
    del configs  # Exact realized R is part of each canonical decision.
    order_keys = [(str(row.get("leg_key", "")), str(row.get("order_id", ""))) for row in rows]
    if any(not all(key) for key in order_keys) or len(order_keys) != len(set(order_keys)):
        raise CriticalShadowError("Decision (leg_key, order_id) keys must be non-empty and unique.")
    filled = [row for row in rows if row["order_state"] == "FILLED"]
    filled.sort(key=lambda row: (pd.Timestamp(row["entry_known_time"]), LEG_ORDER[row["leg_key"]], row["order_id"]))
    prior: list[dict[str, object]] = []
    states: dict[str, str] = {}
    for row in filled:
        entry = pd.Timestamp(row["entry_known_time"])
        definitely_known = [
            item for item in prior
            if item["terminal_known_time"] and pd.Timestamp(item["terminal_known_time"]) < entry
        ]
        simultaneous = [
            item for item in prior
            if item["terminal_known_time"] and pd.Timestamp(item["terminal_known_time"]) == entry
        ]
        unresolved = [item for item in definitely_known if item.get("intrabar_ambiguity")]
        realized = [
            item for item in definitely_known
            if not item.get("intrabar_ambiguity") and item.get("outcome") in {"TP", "SL", "BE"}
        ]
        realized_values = pd.to_numeric(
            pd.Series([item.get("r_multiple") for item in realized], dtype="object"),
            errors="coerce",
        )
        if realized_values.isna().any():
            raise CriticalShadowError("Closed shadow decisions must carry exact realized R.")
        realized_r = float(realized_values.sum())
        if unresolved or simultaneous:
            state = "WATCH_UNORDERED_OR_AMBIGUOUS_PRIOR"
        elif realized_r <= -1.0:
            state = "SUPPRESSED_DAILY_CAP"
        else:
            state = "ALLOWED"
        states[(str(row["leg_key"]), str(row["order_id"]))] = state
        prior.append(row)
    for row in rows:
        row["pair_cap_state"] = states.get(
            (str(row["leg_key"]), str(row["order_id"])), "NOT_APPLICABLE"
        )
        row["shadow_outcome"] = "AMBIGUOUS" if row.get("intrabar_ambiguity") else (row.get("outcome") or None)
        row["shadow_action"] = (
            "WATCH"
            if row.get("intrabar_ambiguity") or str(row["pair_cap_state"]).startswith("WATCH")
            else ("SUPPRESSED" if row["pair_cap_state"] == "SUPPRESSED_DAILY_CAP" else row["final_decision"])
        )
    return rows


def no_trade_reason(day: object) -> str:
    if getattr(day, "data_state") == "INVALID":
        return "DATA_INVALID:" + "|".join(getattr(day, "data_reasons"))
    if getattr(day, "decisions"):
        return ""
    pipeline = getattr(day, "pipeline")
    blocked = [item for item in pipeline if item.status == "BLOCKED"]
    source = blocked[-1] if blocked else (pipeline[-1] if pipeline else None)
    return "NO_DECISION" if source is None else f"{source.stage}:{source.reason}"


def day_rows(result: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for leg_key, leg_result in result.leg_results.items():
        day = leg_result.days[0]
        rows.append(
            {
                "leg_key": leg_key,
                "date": day.trade_date,
                "data_state": day.data_state,
                "data_reasons": list(day.data_reasons),
                "vah": day.vah,
                "val": day.val,
                "premarket_context": None if day.premarket_context is None else asdict(day.premarket_context),
                "liquidity": None if day.liquidity_selection is None else asdict(day.liquidity_selection),
                "controlling_array_id": (
                    None
                    if day.premarket_context is None
                    else (day.premarket_context.controlling_array_id or None)
                ),
                "no_trade_reason": no_trade_reason(day),
            }
        )
    return rows


def add_event(
    events: list[dict[str, object]],
    leg_key: str,
    event_type: str,
    bar_time: object,
    known_time: object,
    **fields: object,
) -> None:
    if not bar_time or not known_time:
        raise CriticalShadowError(f"{leg_key}/{event_type}: bar_time and known_time are mandatory.")
    bar = pd.Timestamp(bar_time)
    known = pd.Timestamp(known_time)
    if known < bar:
        raise CriticalShadowError(f"{leg_key}/{event_type}: known_time precedes bar_time.")
    events.append(
        {
            "leg_key": leg_key,
            "event_type": event_type,
            "bar_time": bar.isoformat(),
            "known_time": known.isoformat(),
            **fields,
        }
    )


def build_event_ledger(result: object, configs: dict[str, SymbolConfig]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for leg_key, leg_result in result.leg_results.items():
        day = leg_result.days[0]
        config = configs[leg_key]
        duration = pd.Timedelta(minutes=int(parse_timeframe_minutes(config.timeframe) or 0))
        trade_start = pd.Timestamp(f"{day.trade_date} {config.trade_window_start}", tz=TZ)
        add_event(
            events,
            leg_key,
            "VALUE_AREA",
            trade_start,
            trade_start,
            vah=day.vah,
            val=day.val,
            state=day.data_state,
        )
        if day.liquidity_selection is not None:
            item = asdict(day.liquidity_selection)
            add_event(events, leg_key, "LIQUIDITY", item["asof_time"], item["asof_time"], details=item)
        if day.premarket_context is not None:
            item = asdict(day.premarket_context)
            add_event(
                events,
                leg_key,
                "CONTROLLING_ARRAY",
                item["asof_time"],
                item["asof_time"],
                entity_id=item["controlling_array_id"] or None,
                details=item,
            )
        for array in day.htf_arrays:
            array_known = pd.Timestamp(array.known_time)
            add_event(
                events,
                leg_key,
                "HTF_ARRAY",
                array_known - pd.Timedelta(minutes=15),
                array_known,
                entity_id=array.array_id,
                state=array.current_state,
                details=asdict(array),
            )
            for transition in array.transitions:
                transition_known = pd.Timestamp(transition.time)
                add_event(
                    events,
                    leg_key,
                    "HTF_ARRAY_TRANSITION",
                    transition_known - pd.Timedelta(minutes=15),
                    transition_known,
                    entity_id=array.array_id,
                    state=transition.state,
                    reason="BODY_CLOSE_STATE_TRANSITION",
                )
        for qualification in day.cisd_qualifications:
            event = qualification.event
            add_event(
                events,
                leg_key,
                "CISD",
                event.confirm_time,
                event.confirm_known_time or pd.Timestamp(event.confirm_time) + duration,
                state=qualification.lifecycle_state,
                direction=event.direction,
                reason=qualification.reason,
                details=asdict(qualification),
            )
        for trigger in day.context_triggers:
            add_event(
                events,
                leg_key,
                "CONTEXT_AUTHORITY",
                trigger.time,
                trigger.known_time or pd.Timestamp(trigger.time) + duration,
                entity_id=trigger.event_id,
                direction=trigger.preferred_direction,
                state=trigger.gate,
                reason=trigger.source,
            )
        for episode in day.thesis_episodes:
            add_event(
                events,
                leg_key,
                "THESIS",
                episode.cisd.confirm_time,
                episode.cisd.confirm_known_time or pd.Timestamp(episode.cisd.confirm_time) + duration,
                entity_id=episode.thesis_id,
                direction=episode.direction,
                state=episode.status,
                reason=episode.terminal_reason or None,
            )
        for decision in day.decisions:
            setup_bar = decision.fvg_time or decision.cisd_time
            setup_known = decision.fvg_known_time or decision.cisd_known_time
            add_event(
                events,
                leg_key,
                "SETUP",
                setup_bar,
                setup_known,
                entity_id=decision.thesis_id,
                order_id=decision.order_id,
                state=decision.setup_state,
                direction=decision.direction,
                reason=decision.fvg_kind or decision.terminal_reason,
            )
            add_event(
                events,
                leg_key,
                "ORDER",
                setup_bar,
                setup_known,
                entity_id=decision.order_id,
                state=decision.order_state,
                direction=decision.direction,
                reason=decision.terminal_reason or None,
            )
            terminal_bar = decision.terminal_time or setup_bar
            terminal_known = decision.terminal_known_time or setup_known
            add_event(
                events,
                leg_key,
                "FINAL",
                terminal_bar,
                terminal_known,
                entity_id=decision.order_id,
                state=decision.final_decision,
                direction=decision.direction,
                reason=decision.terminal_reason or "NO_TERMINAL",
                ambiguity=decision.intrabar_ambiguity or None,
            )
            if decision.fvg_time:
                add_event(
                    events,
                    leg_key,
                    "FVG",
                    decision.fvg_time,
                    decision.fvg_known_time,
                    entity_id=decision.thesis_id,
                    state=decision.fvg_kind,
                    direction=decision.direction,
                )
    events.sort(
        key=lambda row: (
            pd.Timestamp(row["known_time"]),
            LEG_ORDER[row["leg_key"]],
            str(row["event_type"]),
            str(row.get("entity_id") or ""),
        )
    )
    for sequence, event in enumerate(events, 1):
        event["sequence"] = sequence
    return events


def decision_invariant_errors(rows: list[dict[str, object]], events: list[dict[str, object]]) -> list[str]:
    errors: list[str] = []
    order_ids = [str(row.get("order_id") or "") for row in rows]
    if any(not value for value in order_ids):
        errors.append("MISSING_ORDER_ID")
    if len(order_ids) != len(set(order_ids)):
        errors.append("DUPLICATE_ORDER_ID")
    required = ["setup_state", "order_state", "final_decision", "direction", "thesis_id", "trace"]
    for row in rows:
        missing = [field for field in required if not row.get(field)]
        if missing:
            errors.append(f"UNEXPLAINABLE_DECISION:{row.get('order_id')}:{','.join(missing)}")
        if row.get("order_state") in {"FILLED", "CANCELLED"} and not row.get("terminal_reason"):
            errors.append(f"MISSING_TERMINAL_REASON:{row.get('order_id')}")
    if any(not row.get("bar_time") or not row.get("known_time") for row in events):
        errors.append("EVENT_WITHOUT_CAUSAL_TIMES")
    return errors


def prefix_checks(
    frames: dict[str, pd.DataFrame],
    configs: dict[str, SymbolConfig],
    state_config: ManualStateConfig,
    trade_date: date,
) -> tuple[int, list[dict[str, object]]]:
    checks = 0
    violations: list[dict[str, object]] = []
    for leg_key, frame in frames.items():
        config = configs[leg_key]
        minutes = int(parse_timeframe_minutes(config.timeframe) or 0)
        duration = pd.Timedelta(minutes=minutes)
        full = run_manual_state_day(frame, config, trade_date, state_config)
        start = pd.Timestamp(f"{trade_date} {config.trade_window_start}", tz=TZ)
        end = pd.Timestamp(f"{trade_date} {config.trade_window_end}", tz=TZ)
        bar_times = list(pd.date_range(start, end, freq=f"{minutes}min", inclusive="left"))
        for bar_time in bar_times:
            cutoff = bar_time + duration
            prefix_frame = frame[(frame["time"] + duration) <= cutoff].copy()
            prefix_config = replace(config, trade_window_end=cutoff.strftime("%H:%M"))
            prefix_state = replace(state_config, reject_invalid_data=False)
            prefix = run_manual_state_day(prefix_frame, prefix_config, trade_date, prefix_state)
            full_snapshot = state_snapshot(full, cutoff)
            prefix_snapshot = state_snapshot(prefix, cutoff)
            checks += 1
            for component in full_snapshot:
                if full_snapshot[component] != prefix_snapshot[component]:
                    violations.append(
                        {
                            "leg_key": leg_key,
                            "cutoff": cutoff.isoformat(),
                            "component": component,
                            "full_hash": object_hash(full_snapshot[component]),
                            "prefix_hash": object_hash(prefix_snapshot[component]),
                        }
                    )
    return checks, violations


def engine_payload(
    result: object,
    configs: dict[str, SymbolConfig],
) -> dict[str, object]:
    raw = normalized_records(result.decisions)
    decisions = causal_pair_cap(raw, configs)
    events = build_event_ledger(result, configs)
    for decision in decisions:
        if decision["pair_cap_state"] == "NOT_APPLICABLE":
            continue
        add_event(
            events,
            decision["leg_key"],
            "PAIR_CAP",
            decision["entry_time"],
            decision["entry_known_time"],
            entity_id=decision["order_id"],
            state=decision["pair_cap_state"],
            direction=decision["direction"],
            reason="-1R_REALIZED_TERMINALS_ONLY",
        )
    events.sort(
        key=lambda row: (
            pd.Timestamp(row["known_time"]),
            LEG_ORDER[row["leg_key"]],
            str(row["event_type"]),
            str(row.get("entity_id") or ""),
        )
    )
    for sequence, event in enumerate(events, 1):
        event["sequence"] = sequence
    lifecycle: dict[str, object] = {}
    for leg_key, leg_result in result.leg_results.items():
        days = leg_result.days
        lifecycle[leg_key] = {
            "cisd": normalized_records(cisd_qualifications_to_frame(days)),
            "htf_arrays": normalized_records(htf_arrays_to_frame(days)),
            "context_triggers": normalized_records(context_triggers_to_frame(days)),
            "premarket": normalized_records(premarket_contexts_to_frame(days)),
            "liquidity": normalized_records(liquidity_selections_to_frame(days)),
            "context_authority": normalized_records(context_authority_to_frame(days)),
            "theses": normalized_records(thesis_episodes_to_frame(days)),
        }
    return {
        "days": day_rows(result),
        "decisions": decisions,
        "events": events,
        "lifecycle": lifecycle,
    }


def add_data_gate_trace(
    payload: dict[str, object],
    gates: list[dict[str, object]],
    trade_date: date,
    as_of: pd.Timestamp,
) -> None:
    events = payload["events"]
    if not payload["days"]:
        payload["days"] = [
            {
                "leg_key": gate["leg_key"],
                "date": str(trade_date),
                "data_state": "INVALID",
                "data_reasons": [issue["code"] for issue in gate.get("issues", [])],
                "vah": None,
                "val": None,
                "premarket_context": None,
                "liquidity": None,
                "controlling_array_id": None,
                "no_trade_reason": "DATA_INVALID:"
                + "|".join(issue["code"] for issue in gate.get("issues", [])),
            }
            for gate in gates
            if gate["leg_key"] in LEG_ORDER
        ]
    for gate in gates:
        leg_key = gate["leg_key"]
        if leg_key not in LEG_ORDER:
            continue
        issues = gate.get("issues", [])
        add_event(
            events,
            leg_key,
            "DATA_GATE",
            as_of,
            as_of,
            state=gate["state"],
            reason="PASSED" if not issues else "|".join(issue["code"] for issue in issues),
            details={"closed_bar_only": gate.get("closed_bar_only"), "issues": issues},
        )
    events.sort(
        key=lambda row: (
            pd.Timestamp(row["known_time"]),
            LEG_ORDER[row["leg_key"]],
            str(row["event_type"]),
            str(row.get("entity_id") or ""),
        )
    )
    for sequence, event in enumerate(events, 1):
        event["sequence"] = sequence


def manual_path(output_root: Path, trade_date: date) -> Path:
    return output_root / "manual" / f"{trade_date}.json"


def load_manual(output_root: Path, trade_date: date, run_started: pd.Timestamp) -> dict[str, object]:
    path = manual_path(output_root, trade_date)
    if not path.exists():
        raise CriticalShadowError("Blind manual decision is missing; record it before running the engine.")
    payload = read_json(path)
    claimed = payload.pop("record_hash", None)
    if object_hash(payload) != claimed:
        raise CriticalShadowError("Manual decision seal is invalid.")
    payload["record_hash"] = claimed
    if payload.get("date") != str(trade_date):
        raise CriticalShadowError("Manual decision date does not match run date.")
    if pd.Timestamp(payload["recorded_at"]) >= run_started:
        raise CriticalShadowError("Manual decision was not sealed before engine start.")
    return payload


def comparison_rows(
    decisions: list[dict[str, object]],
    manual: dict[str, object],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    by_leg = {key: [] for key in LEG_ORDER}
    for decision in decisions:
        by_leg[decision["leg_key"]].append(decision)
    for leg_key in LEG_ORDER:
        record = manual["legs"][leg_key]
        engine_rows = by_leg[leg_key]
        engine_final = "TAKE" if any(row["final_decision"] == "TAKE" for row in engine_rows) else "SKIP"
        directions = sorted({row["direction"].upper() for row in engine_rows if row.get("direction")})
        engine_direction = directions[0] if len(directions) == 1 else None
        manual_final = record.get("final")
        manual_direction = record.get("direction")
        final_scorable = manual_final in {"TAKE", "SKIP"}
        direction_scorable = manual_direction in {"LONG", "SHORT"} and engine_direction is not None
        final_match = None if not final_scorable else manual_final == engine_final
        direction_match = None if not direction_scorable else manual_direction == engine_direction
        mismatch = final_match is False or direction_match is False
        rows.append(
            {
                "leg_key": leg_key,
                "manual_final": manual_final,
                "engine_final": engine_final,
                "final_scorable": final_scorable,
                "final_match": final_match,
                "manual_direction": manual_direction,
                "engine_direction": engine_direction,
                "direction_scorable": direction_scorable,
                "direction_match": direction_match,
                "explanation": record.get("explanation"),
                "unexplained_mismatch": bool(mismatch and not record.get("explanation")),
                "outcome_excluded_from_scoring": True,
            }
        )
    return rows


def record_manual(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    campaign_lock(output_root)
    trade_date = date.fromisoformat(args.date)
    legs = {}
    for leg_key in LEG_ORDER:
        final = getattr(args, f"{leg_key}_final")
        direction = getattr(args, f"{leg_key}_direction")
        legs[leg_key] = {
            "final": None if final == "UNKNOWN" else final,
            "direction": None if direction == "UNKNOWN" else direction,
            "explanation": getattr(args, f"{leg_key}_explanation") or None,
        }
    payload: dict[str, object] = {
        "schema_version": 1,
        "date": str(trade_date),
        "recorded_at": utc_now().isoformat(),
        "author": args.author,
        "blind_to_engine_result": True,
        "legs": legs,
    }
    payload["record_hash"] = object_hash(payload)
    write_new_json(manual_path(output_root, trade_date), payload)
    print(f"SEALED {manual_path(output_root, trade_date)}")


def create_attempt_dir(output_root: Path, trade_date: date) -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
    path = output_root / "sessions" / str(trade_date) / f"{stamp}_{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def record_failure(output_root: Path, trade_date_text: str, error: Exception) -> None:
    path = output_root / "technical_failures" / (
        f"{utc_now().strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid4().hex[:8]}.json"
    )
    write_new_json(
        path,
        {
            "recorded_at": utc_now().isoformat(),
            "date": trade_date_text,
            "state": "CRITICAL_STOP",
            "error_type": type(error).__name__,
            "error": str(error),
        },
    )


def run_session(args: argparse.Namespace) -> int:
    output_root = Path(args.output_root).resolve()
    trade_date = date.fromisoformat(args.date)
    run_started = utc_now()
    try:
        baseline, configs, state_config = verify_baseline()
        lock = campaign_lock(output_root)
        manual = load_manual(output_root, trade_date, run_started)
        as_of = parse_asof(args.as_of)
        frames: dict[str, pd.DataFrame] = {}
        gates: list[dict[str, object]] = []
        for leg_key, values in {"nq": args.nq, "spx": args.spx}.items():
            frame, gate = read_leg_data(
                [Path(value).resolve() for value in values],
                leg_key,
                configs[leg_key],
                trade_date,
                as_of,
            )
            frames[leg_key] = frame
            gates.append(gate)
        if not all_feeds_match(gates):
            gates.append(
                {
                    "leg_key": "pair",
                    "state": "DATA_INVALID",
                    "issues": [data_issue("PAIR_FEED_MISMATCH", "Both legs must use one DUKASCOPY feed.")],
                }
            )

        valid_data = all(gate["state"] == "VALID" for gate in gates)
        prefix_count = 0
        prefix_violations: list[dict[str, object]] = []
        invariant_errors: list[str] = []
        if valid_data:
            legs = [EngineLeg(key, frames[key], configs[key]) for key in ["nq", "spx"]]
            first = run_canonical_pair_pipeline(legs, [trade_date], state_config=state_config)
            second = run_canonical_pair_pipeline(legs, [trade_date], state_config=state_config)
            first_payload = engine_payload(first, configs)
            second_payload = engine_payload(second, configs)
            deterministic = object_hash(first_payload) == object_hash(second_payload)
            prefix_count, prefix_violations = prefix_checks(frames, configs, state_config, trade_date)
            invariant_errors = decision_invariant_errors(
                first_payload["decisions"],
                first_payload["events"],
            )
            payload = first_payload
        else:
            deterministic = True
            payload = {"days": [], "decisions": [], "events": [], "lifecycle": {}}

        add_data_gate_trace(payload, gates, trade_date, as_of)
        if not valid_data and payload["decisions"]:
            invariant_errors.append("DATA_INVALID_WITH_DECISIONS")
        comparisons = comparison_rows(payload["decisions"], manual)
        critical = (not deterministic) or bool(prefix_violations) or bool(invariant_errors)
        session_state = "CRITICAL_STOP" if critical else ("VALID" if valid_data else "DATA_INVALID")
        result_hash = object_hash(payload)
        attempt_dir = create_attempt_dir(output_root, trade_date)
        write_new_json(attempt_dir / "manual_snapshot.json", manual)
        write_new_json(attempt_dir / "data_gate.json", gates)
        write_new_jsonl(attempt_dir / "chronological_trace.jsonl", payload["events"])
        write_new_jsonl(attempt_dir / "decisions.jsonl", payload["decisions"])
        write_new_json(attempt_dir / "lifecycle.json", payload["lifecycle"])
        write_new_json(attempt_dir / "day_summary.json", payload["days"])
        write_new_json(attempt_dir / "manual_engine_alignment.json", comparisons)
        write_new_json(
            attempt_dir / "causality_checks.json",
            {
                "deterministic_rerun": deterministic,
                "prefix_checks": prefix_count,
                "prefix_violations": prefix_violations,
                "invariant_errors": invariant_errors,
            },
        )
        session_record = {
            "schema_version": 1,
            "date": str(trade_date),
            "run_started": run_started.isoformat(),
            "run_completed": utc_now().isoformat(),
            "state": session_state,
            "execution": "SHADOW_ONLY_NO_ORDER_TRANSPORT",
            "order_transport_present": False,
            "timezone": TZ,
            "feed": "DUKASCOPY",
            "timeframes": {"nq": "3m", "spx": "5m", "htf": "15m"},
            "hashes": {
                "baseline_manifest": baseline["baseline_manifest_sha256"],
                "code": lock["engine_code_hash"],
                "config": lock["config_hash"],
                "harness": lock["harness_hash"],
                "data": {gate["leg_key"]: gate.get("data_hash") for gate in gates if gate["leg_key"] in LEG_ORDER},
                "manual": manual["record_hash"],
                "result": result_hash,
            },
            "data_valid": valid_data,
            "decision_count": len(payload["decisions"]),
            "scorable_engine_decisions": sum(
                1
                for decision in payload["decisions"]
                if next(
                    row["final_scorable"]
                    for row in comparisons
                    if row["leg_key"] == decision["leg_key"]
                )
            ),
            "ambiguous_decisions": sum(bool(row.get("intrabar_ambiguity")) for row in payload["decisions"]),
            "deterministic_rerun": deterministic,
            "prefix_checks": prefix_count,
            "prefix_violation_count": len(prefix_violations),
            "invariant_error_count": len(invariant_errors),
            "trace_explainable_pct": 100.0 if not invariant_errors else 0.0,
            "unexplained_manual_engine_mismatches": sum(
                bool(row["unexplained_mismatch"]) for row in comparisons
            ),
            "final_alignment": {
                "scorable": sum(bool(row["final_scorable"]) for row in comparisons),
                "matches": sum(row["final_match"] is True for row in comparisons),
            },
            "direction_alignment": {
                "scorable": sum(bool(row["direction_scorable"]) for row in comparisons),
                "matches": sum(row["direction_match"] is True for row in comparisons),
            },
        }
        write_new_json(attempt_dir / "session.json", session_record)
        print(f"{session_state} {attempt_dir}")
        return 2 if critical else (3 if not valid_data else 0)
    except Exception as exc:
        record_failure(output_root, str(trade_date), exc)
        raise


def session_records(output_root: Path) -> list[dict[str, object]]:
    records = []
    for path in sorted((output_root / "sessions").glob("*/*/session.json")):
        record = read_json(path)
        record["_path"] = str(path)
        records.append(record)
    return records


def campaign_status(args: argparse.Namespace) -> int:
    output_root = Path(args.output_root).resolve()
    campaign_lock(output_root)
    records = session_records(output_root)
    failures = sorted((output_root / "technical_failures").glob("*.json"))
    by_date: dict[str, list[dict[str, object]]] = {}
    for record in records:
        by_date.setdefault(str(record["date"]), []).append(record)
    cross_attempt_mismatch = []
    selected: list[dict[str, object]] = []
    for trade_date, items in sorted(by_date.items()):
        by_input: dict[str, set[str]] = {}
        for item in items:
            input_hash = object_hash(item["hashes"]["data"])
            by_input.setdefault(input_hash, set()).add(item["hashes"]["result"])
        if any(len(result_hashes) > 1 for result_hashes in by_input.values()):
            cross_attempt_mismatch.append(trade_date)
        selected.append(items[0])
    valid = [item for item in selected if item["state"] == "VALID"]
    scorable = sum(int(item["scorable_engine_decisions"]) for item in valid)
    code_hashes = {item["hashes"]["code"] for item in records}
    config_hashes = {item["hashes"]["config"] for item in records}
    harness_hashes = {item["hashes"]["harness"] for item in records}
    gates = {
        "minimum_30_valid_sessions": len(valid) >= 30,
        "minimum_20_scorable_engine_decisions": scorable >= 20,
        "zero_lookahead_prefix_violations": all(item["prefix_violation_count"] == 0 for item in records),
        "zero_illegal_state_or_duplicate_order": all(item["invariant_error_count"] == 0 for item in records),
        "fully_explainable_trace": all(item["trace_explainable_pct"] == 100.0 for item in records),
        "zero_trades_on_data_invalid": all(
            item["decision_count"] == 0 for item in records if item["state"] == "DATA_INVALID"
        ),
        "fully_deterministic_reruns": all(item["deterministic_rerun"] for item in records)
        and not cross_attempt_mismatch,
        "unchanged_code_config_hash": len(code_hashes) <= 1
        and len(config_hashes) <= 1
        and len(harness_hashes) <= 1,
        "zero_unexplained_manual_engine_difference": sum(
            int(item["unexplained_manual_engine_mismatches"]) for item in records
        )
        == 0,
        "zero_critical_technical_failure": not failures
        and all(item["state"] != "CRITICAL_STOP" for item in records),
    }
    eligible = gates["minimum_30_valid_sessions"] and gates["minimum_20_scorable_engine_decisions"]
    passed = eligible and all(gates.values())
    final_scorable = sum(int(item["final_alignment"]["scorable"]) for item in valid)
    final_matches = sum(int(item["final_alignment"]["matches"]) for item in valid)
    direction_scorable = sum(int(item["direction_alignment"]["scorable"]) for item in valid)
    direction_matches = sum(int(item["direction_alignment"]["matches"]) for item in valid)
    status = {
        "generated_at": utc_now().isoformat(),
        "state": "PASS_TO_PAPER_FORWARD" if passed else ("COLLECTING" if not eligible else "FAIL_RESTART_CLEAN"),
        "valid_sessions": len(valid),
        "scorable_engine_decisions": scorable,
        "remaining_valid_sessions": max(30 - len(valid), 0),
        "remaining_scorable_engine_decisions": max(20 - scorable, 0),
        "gates": gates,
        "cross_attempt_result_mismatch_dates": cross_attempt_mismatch,
        "final_alignment": {
            "matches": final_matches,
            "scorable": final_scorable,
            "rate": None if not final_scorable else final_matches / final_scorable,
        },
        "direction_alignment": {
            "matches": direction_matches,
            "scorable": direction_scorable,
            "rate": None if not direction_scorable else direction_matches / direction_scorable,
        },
        "paper_order_authorized": passed,
    }
    snapshot = output_root / "status_snapshots" / (
        f"{utc_now().strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid4().hex[:8]}.json"
    )
    write_new_json(snapshot, status)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if passed else 1


def initialize(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    lock = campaign_lock(output_root)
    print(json.dumps(lock, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="NQ 3m + SPX 5m causal, no-order forward shadow runner.")
    root.set_defaults(output_root=str(DEFAULT_OUTPUT))
    sub = root.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    init.set_defaults(func=initialize)

    manual = sub.add_parser("manual")
    manual.add_argument("--date", required=True)
    manual.add_argument("--author", required=True)
    for leg in LEG_ORDER:
        manual.add_argument(f"--{leg}-final", choices=["TAKE", "SKIP", "UNKNOWN"], required=True)
        manual.add_argument(f"--{leg}-direction", choices=["LONG", "SHORT", "UNKNOWN"], required=True)
        manual.add_argument(f"--{leg}-explanation", default="")
    manual.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    manual.set_defaults(func=record_manual)

    run = sub.add_parser("run")
    run.add_argument("--date", required=True)
    run.add_argument("--nq", action="append", required=True, help="Repeat for each NQ 3m CSV.")
    run.add_argument("--spx", action="append", required=True, help="Repeat for each SPX 5m CSV.")
    run.add_argument("--as-of", help="ISO timestamp; naive values are America/New_York.")
    run.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    run.set_defaults(func=run_session)

    status = sub.add_parser("status")
    status.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    status.set_defaults(func=campaign_status)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        result = args.func(args)
    except (CriticalShadowError, FileExistsError, ValueError) as exc:
        print(f"CRITICAL_STOP: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(result if isinstance(result, int) else 0)


if __name__ == "__main__":
    main()
