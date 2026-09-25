"""Signed, exact-match instrument contracts."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import re
from numbers import Real
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import InstrumentContract


class InstrumentContractError(RuntimeError):
    pass


REQUIRED_FIELDS = (
    "instrument_id", "asset_class", "broker", "expected_server", "broker_symbol",
    "timezone", "base_currency", "profit_currency", "margin_currency", "digits",
    "point", "tick_size", "contract_size", "volume_min", "volume_max", "volume_step",
    "pip_size", "cost_unit", "trade_calc_mode",
)
NUMERIC_FIELDS = ("point", "tick_size", "contract_size", "volume_min", "volume_max", "volume_step", "pip_size")
SEMANTIC_FIELDS = (
    "canonical_underlying", "expected_company", "broker_path_regex",
    "broker_description_regex", "session_calendar_id",
    "expected_one_tick_value_at_min_volume", "economic_value_tolerance",
)


def contract_from_mapping(value: Mapping[str, Any]) -> InstrumentContract:
    missing = [field for field in REQUIRED_FIELDS if field not in value]
    if missing:
        raise InstrumentContractError(f"instrument contract missing fields: {', '.join(missing)}")
    normalized: dict[str, Any] = {}
    for field in NUMERIC_FIELDS:
        number = value[field]
        try:
            converted = float(number)
        except (TypeError, ValueError, OverflowError) as exc:
            raise InstrumentContractError(f"instrument contract field is invalid: {field}") from exc
        if isinstance(number, bool) or not isinstance(number, Real) or not math.isfinite(converted) or converted <= 0:
            raise InstrumentContractError(f"instrument contract field is invalid: {field}")
        normalized[field] = converted
    digits = value["digits"]
    if isinstance(digits, bool) or not isinstance(digits, int) or digits < 0:
        raise InstrumentContractError("instrument contract digits are invalid")
    if normalized["volume_max"] < normalized["volume_min"]:
        raise InstrumentContractError("instrument contract range is invalid")
    normalized["digits"] = digits
    calc_mode = value["trade_calc_mode"]
    if isinstance(calc_mode, bool) or not isinstance(calc_mode, Real) or not math.isfinite(float(calc_mode)) or float(calc_mode) < 0:
        raise InstrumentContractError("instrument contract field is invalid: trade_calc_mode")
    semantic_present = any(field in value for field in SEMANTIC_FIELDS)
    if semantic_present:
        missing_semantic = [field for field in SEMANTIC_FIELDS if field not in value]
        if missing_semantic:
            raise InstrumentContractError(f"instrument semantic contract missing fields: {', '.join(missing_semantic)}")
    kwargs = {field: normalized.get(field, value[field]) for field in REQUIRED_FIELDS}
    if semantic_present:
        kwargs.update({field: value[field] for field in SEMANTIC_FIELDS})
    try:
        return InstrumentContract(**kwargs)
    except (TypeError, ValueError) as exc:
        raise InstrumentContractError("instrument semantic contract is invalid") from exc


def validate_symbol_info(requested: str, info: Mapping[str, Any] | object) -> None:
    observed = info.get("name") if isinstance(info, Mapping) else getattr(info, "name", None)
    if not isinstance(observed, str) or observed != requested:
        raise InstrumentContractError("symbol_info name must be an exact, case-sensitive match")


def validate_metadata(expected: InstrumentContract, metadata: Mapping[str, Any] | object) -> None:
    if not isinstance(metadata, Mapping) and not hasattr(metadata, "tick_size"):
        metadata = map_mt5_symbol_info(metadata)
    elif isinstance(metadata, Mapping) and "tick_size" not in metadata and "trade_tick_size" in metadata:
        metadata = map_mt5_symbol_info(metadata)
    validate_symbol_info(expected.broker_symbol, metadata)
    fields = ("digits", "point", "tick_size", "contract_size", "volume_min", "volume_max", "volume_step", "base_currency", "profit_currency", "margin_currency", "trade_calc_mode")
    for field in fields:
        missing = field not in metadata if isinstance(metadata, Mapping) else not hasattr(metadata, field)
        if missing:
            raise InstrumentContractError(f"instrument metadata missing: {field}")
        observed = metadata.get(field) if isinstance(metadata, Mapping) else getattr(metadata, field, None)
        expected_value = getattr(expected, field)
        if isinstance(expected_value, (int, float)):
            try:
                if isinstance(observed, bool) or not math.isfinite(float(observed)) or float(observed) != float(expected_value):
                    raise InstrumentContractError(f"instrument metadata mismatch: {field}")
            except (TypeError, ValueError):
                raise InstrumentContractError(f"instrument metadata mismatch: {field}")
        elif observed != expected_value:
            raise InstrumentContractError(f"instrument metadata mismatch: {field}")
    optional_identity = {
        "asset_class": expected.asset_class,
        "server": expected.expected_server,
        "company": expected.expected_company,
    }
    for field, expected_value in optional_identity.items():
        observed = metadata.get(field) if isinstance(metadata, Mapping) else getattr(metadata, field, None)
        if observed is not None and str(observed) != expected_value:
            raise InstrumentContractError(f"instrument metadata mismatch: {field}")
    if expected.broker_path_regex:
        for field, pattern in (("path", expected.broker_path_regex), ("description", expected.broker_description_regex)):
            observed = metadata.get(field) if isinstance(metadata, Mapping) else getattr(metadata, field, None)
            if not isinstance(observed, str) or re.fullmatch(pattern, observed) is None:
                raise InstrumentContractError(f"instrument metadata mismatch: {field}")


def validate_economic_semantics(
    expected: InstrumentContract,
    order_calc_profit: Any,
    *,
    reference_price: Any = 1000.0,
) -> None:
    """Confirm the signed one-tick BUY/SELL economic fingerprint."""
    if not expected.canonical_underlying or not callable(order_calc_profit):
        raise InstrumentContractError("economic semantic probe is unavailable")
    try:
        reference = float(reference_price)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InstrumentContractError("economic semantic reference price is invalid") from exc
    if isinstance(reference_price, bool) or not math.isfinite(reference) or reference <= 0:
        raise InstrumentContractError("economic semantic reference price is invalid")
    probes = (
        order_calc_profit(0, expected.broker_symbol, expected.volume_min, reference, reference + expected.tick_size),
        order_calc_profit(1, expected.broker_symbol, expected.volume_min, reference, reference - expected.tick_size),
    )
    target = float(expected.expected_one_tick_value_at_min_volume)
    tolerance = float(expected.economic_value_tolerance)
    for value in probes:
        if isinstance(value, bool):
            raise InstrumentContractError("economic semantic probe returned invalid value")
        try:
            observed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise InstrumentContractError("economic semantic probe returned invalid value") from exc
        if not math.isfinite(observed) or observed <= 0 or abs(observed - target) > tolerance:
            raise InstrumentContractError("economic semantic probe mismatch")


def validate_current_tick(expected: InstrumentContract, tick: Mapping[str, Any] | object) -> float:
    """Validate a live bid/ask quote before using it for economic probes."""
    def get(name: str) -> Any:
        return tick.get(name) if isinstance(tick, Mapping) else getattr(tick, name, None)

    bid, ask = get("bid"), get("ask")
    if any(isinstance(value, bool) or not isinstance(value, Real) for value in (bid, ask)):
        raise InstrumentContractError("current broker tick is missing bid/ask")
    bid_value, ask_value = float(bid), float(ask)
    if not all(math.isfinite(value) and value > 0 for value in (bid_value, ask_value)) or bid_value > ask_value:
        raise InstrumentContractError("current broker tick is invalid")
    timestamp = get("time_msc") if get("time_msc") is not None else get("time")
    if isinstance(timestamp, bool) or not isinstance(timestamp, Real) or not math.isfinite(float(timestamp)) or float(timestamp) <= 0:
        raise InstrumentContractError("current broker tick timestamp is invalid")
    return (bid_value + ask_value) / 2.0


def map_mt5_symbol_info(info: Mapping[str, Any] | object) -> dict[str, Any]:
    """Map the MT5 names explicitly; no broker metadata is inferred."""
    def get(name: str) -> Any:
        return info.get(name) if isinstance(info, Mapping) else getattr(info, name, None)

    mapping = {
        "name": "name", "digits": "digits", "point": "point",
        "trade_tick_size": "tick_size", "trade_contract_size": "contract_size",
        "volume_min": "volume_min", "volume_max": "volume_max", "volume_step": "volume_step",
        "currency_base": "base_currency", "currency_profit": "profit_currency",
        "currency_margin": "margin_currency", "trade_calc_mode": "trade_calc_mode",
        "path": "path", "description": "description",
    }
    result = {target: get(source) for source, target in mapping.items()}
    if any(value is None for value in result.values()):
        missing = [key for key, value in result.items() if value is None]
        raise InstrumentContractError(f"MT5 symbol metadata missing: {', '.join(missing)}")
    return result


class InstrumentRegistry:
    def __init__(self, contracts: Iterable[InstrumentContract] = ()) -> None:
        self._by_id: dict[str, InstrumentContract] = {}
        self._by_symbol: dict[str, InstrumentContract] = {}
        self._folded_ids: set[str] = set()
        self._folded_symbols: set[str] = set()
        for contract in contracts:
            self.register(contract)

    def register(self, contract: InstrumentContract) -> None:
        folded_id = contract.instrument_id.casefold()
        folded_symbol = contract.broker_symbol.casefold()
        if (contract.instrument_id in self._by_id or contract.broker_symbol in self._by_symbol or
            folded_id in self._folded_ids or folded_symbol in self._folded_symbols):
            raise InstrumentContractError("duplicate instrument contract")
        if any(contract.broker_symbol.casefold().startswith(existing.casefold() + ".") or existing.casefold().startswith(contract.broker_symbol.casefold() + ".") for existing in self._by_symbol):
            raise InstrumentContractError("instrument symbol suffix collision")
        self._by_id[contract.instrument_id] = contract
        self._by_symbol[contract.broker_symbol] = contract
        self._folded_ids.add(folded_id)
        self._folded_symbols.add(folded_symbol)

    def get(self, instrument_id: str) -> InstrumentContract:
        try:
            return self._by_id[instrument_id]
        except KeyError as exc:
            raise InstrumentContractError(f"unknown instrument: {instrument_id}") from exc

    def symbol_info(self, requested: str, info: Mapping[str, Any] | object) -> InstrumentContract:
        # No substring/fallback discovery in order mode.
        contract = self._by_symbol.get(requested)
        if contract is None:
            raise InstrumentContractError(f"unknown exact broker symbol: {requested}")
        validate_metadata(contract, info)
        return contract

    def discover_read_only(self, requested: str, candidates: Iterable[Mapping[str, Any] | object]) -> list[Mapping[str, Any] | object]:
        return [candidate for candidate in candidates if str(candidate.get("name") if isinstance(candidate, Mapping) else getattr(candidate, "name", "")) == requested]

    @classmethod
    def from_signed_json(cls, path: str | Path, expected_sha256: str) -> "InstrumentRegistry":
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise InstrumentContractError("signed instrument registry hash mismatch")
        payload = json.loads(raw.decode("utf-8"))
        rows = payload.get("instruments") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise InstrumentContractError("instrument registry must contain instruments")
        return cls(contract_from_mapping(row) for row in rows)
