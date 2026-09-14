"""Closed canonical-JSON protocol for deterministic live signal evaluation."""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .config import SymbolConfig
from .engine_pipeline import source_code_hash, stable_frame_hash
from .manual_state import ManualStateConfig


SCHEMA_VERSION = 1
LEG_KEYS = ("nq", "spx")
BAR_FIELDS = ("time", "open", "high", "low", "close", "volume", "known_time", "source_minute_count")
REQUEST_FIELDS = frozenset({
    "schema_version", "request_hash", "candidate_artifact_hash", "candidate_file_hash",
    "engine_source_hash", "calendar_hash", "trade_date", "market_data_asof",
    "knowledge_asof", "cutoffs", "configs", "state_config", "legs",
    "scheduled_closed_ranges",
})
RESPONSE_FIELDS = frozenset({
    "schema_version", "request_hash", "decisions", "events", "lifecycle", "days",
    "state_snapshots", "graph_features", "semantic_output_hash",
})


class LiveSignalProtocolError(RuntimeError):
    pass


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=str).encode("utf-8")


def canonical_hash(value: object) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def _frame_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    missing = [name for name in BAR_FIELDS if name not in frame.columns]
    if missing:
        raise LiveSignalProtocolError(f"live signal bar metadata missing: {', '.join(missing)}")
    if set(frame.columns) - set(BAR_FIELDS):
        frame = frame[list(BAR_FIELDS)]
    timestamps = pd.to_datetime(frame["time"], utc=True, errors="coerce", format="mixed")
    known = pd.to_datetime(frame["known_time"], utc=True, errors="coerce", format="mixed")
    if timestamps.isna().any() or known.isna().any() or timestamps.duplicated().any() or not timestamps.is_monotonic_increasing:
        raise LiveSignalProtocolError("live signal bars have invalid, duplicate, or unordered timestamps")
    if (known < timestamps).any():
        raise LiveSignalProtocolError("live signal knowledge time precedes market time")
    rows: list[dict[str, object]] = []
    for position, (_, row) in enumerate(frame.iterrows()):
        item: dict[str, object] = {"time": timestamps.iloc[position].isoformat(), "known_time": known.iloc[position].isoformat()}
        for name in ("open", "high", "low", "close", "volume"):
            value = row[name]
            if isinstance(value, bool):
                raise LiveSignalProtocolError("live signal numeric bar field is invalid")
            number = float(value)
            if not math.isfinite(number):
                raise LiveSignalProtocolError("live signal numeric bar field is non-finite")
            item[name] = number
        count = row["source_minute_count"]
        if isinstance(count, bool) or int(count) != float(count) or int(count) <= 0:
            raise LiveSignalProtocolError("source-minute count is invalid")
        item["source_minute_count"] = int(count)
        rows.append(item)
    return rows


def build_request(
    *, frames: Mapping[str, pd.DataFrame], configs: Mapping[str, SymbolConfig],
    state_config: ManualStateConfig, trade_date: date, market_data_asof: object,
    knowledge_asof: object, cutoffs: Mapping[str, object], candidate_artifact_hash: str,
    candidate_file_hash: str, calendar_hash: str,
    scheduled_closed_ranges: Mapping[str, list[Mapping[str, object]]] | None = None,
) -> dict[str, object]:
    if set(frames) != set(LEG_KEYS) or set(configs) != set(LEG_KEYS) or set(cutoffs) != set(LEG_KEYS):
        raise LiveSignalProtocolError("live signal request requires exact nq/spx legs")
    records = {key: _frame_records(frames[key]) for key in LEG_KEYS}
    request: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "candidate_artifact_hash": candidate_artifact_hash,
        "candidate_file_hash": candidate_file_hash,
        "engine_source_hash": source_code_hash(),
        "calendar_hash": calendar_hash,
        "trade_date": str(trade_date),
        "market_data_asof": pd.Timestamp(market_data_asof).tz_convert("UTC").isoformat(),
        "knowledge_asof": pd.Timestamp(knowledge_asof).tz_convert("UTC").isoformat(),
        "cutoffs": {key: pd.Timestamp(cutoffs[key]).isoformat() for key in LEG_KEYS},
        "configs": {key: asdict(configs[key]) for key in LEG_KEYS},
        "state_config": asdict(state_config),
        "scheduled_closed_ranges": {
            key: [dict(item) for item in (scheduled_closed_ranges or {}).get(key, [])]
            for key in LEG_KEYS
        },
        "legs": {key: {
            "bars": records[key],
            "data_hash": stable_frame_hash(pd.DataFrame(records[key])[list(BAR_FIELDS)]),
        } for key in LEG_KEYS},
    }
    request["request_hash"] = canonical_hash(request)
    validate_request(request)
    return request


def validate_request(request: Mapping[str, object]) -> None:
    if set(request) != REQUEST_FIELDS or request.get("schema_version") != SCHEMA_VERSION:
        raise LiveSignalProtocolError("live signal request schema is closed")
    claimed = str(request.get("request_hash") or "")
    unsigned = {key: value for key, value in request.items() if key != "request_hash"}
    if claimed != canonical_hash(unsigned):
        raise LiveSignalProtocolError("live signal request hash mismatch")
    if str(request.get("engine_source_hash")) != source_code_hash():
        raise LiveSignalProtocolError("live signal engine source hash mismatch")
    for name in ("candidate_artifact_hash", "candidate_file_hash", "calendar_hash"):
        value = str(request.get(name) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
            raise LiveSignalProtocolError(f"{name} is not SHA-256")
    try:
        trade_date = pd.Timestamp(str(request["trade_date"])).date()
        market = pd.Timestamp(str(request["market_data_asof"]))
        knowledge = pd.Timestamp(str(request["knowledge_asof"]))
    except (TypeError, ValueError) as exc:
        raise LiveSignalProtocolError("live signal time binding is invalid") from exc
    if market.tzinfo is None or knowledge.tzinfo is None or market > knowledge:
        raise LiveSignalProtocolError("live signal causal timestamps are invalid")
    configs, state, legs, cutoffs = request["configs"], request["state_config"], request["legs"], request["cutoffs"]
    closed_ranges = request["scheduled_closed_ranges"]
    if not isinstance(configs, Mapping) or set(configs) != set(LEG_KEYS) or not isinstance(legs, Mapping) or set(legs) != set(LEG_KEYS) or not isinstance(cutoffs, Mapping) or set(cutoffs) != set(LEG_KEYS):
        raise LiveSignalProtocolError("live signal leg maps are invalid")
    if not isinstance(state, Mapping) or set(state) != {field.name for field in fields(ManualStateConfig)}:
        raise LiveSignalProtocolError("state config fields differ from the fixed schema")
    if not isinstance(closed_ranges, Mapping) or set(closed_ranges) != set(LEG_KEYS):
        raise LiveSignalProtocolError("scheduled closed-range map is invalid")
    for key in LEG_KEYS:
        config = configs[key]
        leg = legs[key]
        if not isinstance(config, Mapping) or set(config) != {field.name for field in fields(SymbolConfig)}:
            raise LiveSignalProtocolError("symbol config fields differ from the fixed schema")
        if not isinstance(leg, Mapping) or set(leg) != {"bars", "data_hash"} or not isinstance(leg["bars"], list) or not leg["bars"]:
            raise LiveSignalProtocolError("live signal leg payload is invalid")
        if any(not isinstance(row, Mapping) or set(row) != set(BAR_FIELDS) for row in leg["bars"]):
            raise LiveSignalProtocolError("live signal bar schema is closed")
        frame = pd.DataFrame(leg["bars"])
        if stable_frame_hash(frame[list(BAR_FIELDS)]) != leg["data_hash"]:
            raise LiveSignalProtocolError("live signal leg data hash mismatch")
        _frame_records(frame)
        if pd.Timestamp(str(cutoffs[key])).date() != trade_date:
            raise LiveSignalProtocolError("live signal cutoff date mismatch")
        for interval in closed_ranges[key]:
            if not isinstance(interval, Mapping) or set(interval) != {"bar_count", "first", "last"}:
                raise LiveSignalProtocolError("scheduled closed-range schema is invalid")
            first, last = pd.Timestamp(str(interval["first"])), pd.Timestamp(str(interval["last"]))
            if first.tzinfo is None or last.tzinfo is None or first > last or int(interval["bar_count"]) <= 0:
                raise LiveSignalProtocolError("scheduled closed range is invalid")


def validate_response(response: Mapping[str, object], request_hash: str) -> None:
    if set(response) != RESPONSE_FIELDS or response.get("schema_version") != SCHEMA_VERSION or response.get("request_hash") != request_hash:
        raise LiveSignalProtocolError("live signal response schema/binding mismatch")
    unsigned = {key: value for key, value in response.items() if key != "semantic_output_hash"}
    if response.get("semantic_output_hash") != canonical_hash(unsigned):
        raise LiveSignalProtocolError("live signal semantic output hash mismatch")
    for name in ("decisions", "events", "days"):
        if not isinstance(response.get(name), list):
            raise LiveSignalProtocolError("live signal response collection is invalid")
    for name in ("lifecycle", "state_snapshots", "graph_features"):
        if not isinstance(response.get(name), Mapping):
            raise LiveSignalProtocolError("live signal response map is invalid")


def evaluate_live_signal_twice(request: Mapping[str, object]) -> dict[str, object]:
    from .sandbox import run_isolated_worker
    validate_request(request)
    worker = Path(__file__).resolve().parents[1] / "scripts" / "live_signal_sandbox_worker.py"
    outputs: list[dict[str, object]] = []
    for _ in range(2):
        envelope = run_isolated_worker(worker, {"action": "live_signal", "request": dict(request)}, profile="live-signal")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise LiveSignalProtocolError("live signal worker result is missing")
        validate_response(result, str(request["request_hash"]))
        outputs.append(result)
    if outputs[0]["semantic_output_hash"] != outputs[1]["semantic_output_hash"]:
        raise LiveSignalProtocolError("SIGNAL_SANDBOX_NONDETERMINISTIC")
    return outputs[0]
