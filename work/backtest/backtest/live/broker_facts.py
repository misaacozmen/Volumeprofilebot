"""Fail-closed broker facts used by every production risk snapshot."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import math
from numbers import Real
from typing import Any, Callable, Mapping

from .contracts import BrokerSnapshot, InstrumentContract


class BrokerFactsError(RuntimeError):
    pass


def _value(item: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)


def _number(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise BrokerFactsError(f"{field} is missing or has the wrong type")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise BrokerFactsError(f"{field} is missing or non-finite")
    return result


def _row(item: Mapping[str, Any] | object) -> dict[str, Any]:
    fields = (
        "ticket", "order", "deal_id", "position_id", "symbol", "magic", "comment", "type",
        "entry", "volume", "volume_initial", "volume_current", "price", "price_open", "sl", "tp",
        "time", "time_msc", "profit", "commission", "swap", "fee", "r_multiple", "candidate_hash",
    )
    result = {field: _value(item, field) for field in fields if _value(item, field) is not None}
    for key, value in tuple(result.items()):
        if isinstance(value, Real) and not isinstance(value, bool) and not math.isfinite(float(value)):
            raise BrokerFactsError(f"broker row {key} is non-finite")
    return result


def _direction(value: Any) -> str:
    text = str(value).upper()
    if text in {"BUY", "BUY_LIMIT", "BUY_STOP", "0", "2", "4"}:
        return "BUY"
    if text in {"SELL", "SELL_LIMIT", "SELL_STOP", "1", "3", "5"}:
        return "SELL"
    raise BrokerFactsError("exposure direction is missing or unknown")


class BrokerFactsBuilder:
    """Read account/exposure/deal facts under the same broker mutex.

    The builder intentionally receives a retrying ``read`` callback.  It never
    converts a missing broker value into a healthy default.
    """

    def __init__(
        self,
        *,
        read: Callable[..., Any],
        order_calc_profit: Callable[..., Any],
        order_calc_margin: Callable[..., Any],
        halt_reader: Callable[[], bool],
        strategy_health_reader: Callable[[], str],
        starting_risk_reader: Callable[[str], float | None],
        strategy_matcher: Callable[[Mapping[str, Any]], bool],
        mutex: Callable[[], Any] | None = None,
        contract_resolver: Callable[[str], InstrumentContract] | None = None,
        deal_out_values: set[Any] | None = None,
    ) -> None:
        self.read = read
        self.order_calc_profit = order_calc_profit
        self.order_calc_margin = order_calc_margin
        self.halt_reader = halt_reader
        self.strategy_health_reader = strategy_health_reader
        self.starting_risk_reader = starting_risk_reader
        self.strategy_matcher = strategy_matcher
        self.mutex = mutex or (lambda: nullcontext())
        self.contract_resolver = contract_resolver
        self.deal_out_values = deal_out_values or {1, "1", "OUT", "DEAL_ENTRY_OUT"}

    def _collection(self, operation: str, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        value = self.read(operation, *args, **kwargs)
        if value is None:
            raise BrokerFactsError(f"broker {operation} returned unknown state")
        if not isinstance(value, (tuple, list)):
            raise BrokerFactsError(f"broker {operation} returned a non-collection")
        return tuple(value)

    def _daily_realized_r(self, deals: tuple[dict[str, Any], ...]) -> float:
        grouped: dict[str, float] = {}
        seen: set[str] = set()
        for deal in deals:
            deal_id = str(deal.get("deal_id") or deal.get("ticket") or "").strip()
            if not deal_id or deal_id in seen:
                raise BrokerFactsError("strategy broker deals contain a missing or duplicate deal ID")
            seen.add(deal_id)
            if deal.get("entry") not in self.deal_out_values:
                continue
            position_id = str(deal.get("position_id") or "").strip()
            if not position_id:
                raise BrokerFactsError("strategy exit deal has no position ID")
            risk = self.starting_risk_reader(position_id)
            risk_value = _number(risk, f"starting risk for position {position_id}", positive=True)
            components = []
            for field in ("profit", "commission", "swap", "fee"):
                if field not in deal:
                    raise BrokerFactsError(f"strategy deal {deal_id} is missing {field}")
                components.append(_number(deal[field], f"deal {deal_id} {field}"))
            grouped[position_id] = grouped.get(position_id, 0.0) + sum(components) / risk_value
        return float(sum(grouped.values()))

    def _stop_risk(self, rows: tuple[dict[str, Any], ...], default_contract: InstrumentContract) -> float:
        total = 0.0
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if not symbol:
                raise BrokerFactsError("exposure symbol is missing")
            contract = default_contract if symbol == default_contract.broker_symbol else None
            if contract is None and self.contract_resolver is not None:
                contract = self.contract_resolver(symbol)
            if contract is None:
                raise BrokerFactsError(f"no signed contract for exposure symbol {symbol}")
            sl = row.get("sl")
            if sl is None:
                raise BrokerFactsError(f"exposure {symbol} has no exact stop loss")
            stop = _number(sl, f"{symbol} stop loss", positive=True)
            volume = row.get("volume_current", row.get("volume", row.get("volume_initial")))
            amount = _number(volume, f"{symbol} exposure volume", positive=True)
            entry = row.get("price_open", row.get("price"))
            entry_value = _number(entry, f"{symbol} exposure entry price", positive=True)
            result = self.order_calc_profit(_direction(row.get("type")), symbol, amount, entry_value, stop)
            total += abs(_number(result, f"{symbol} exact stop risk"))
        return float(total)

    def _exposure_metrics(
        self,
        rows: tuple[dict[str, Any], ...],
        default_contract: InstrumentContract,
        equity: float,
    ) -> tuple[float, float]:
        notionals: dict[str, float] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if not symbol:
                raise BrokerFactsError("exposure symbol is missing")
            contract = default_contract if symbol == default_contract.broker_symbol else None
            if contract is None and self.contract_resolver is not None:
                contract = self.contract_resolver(symbol)
            if contract is None:
                raise BrokerFactsError(f"no signed contract for exposure symbol {symbol}")
            volume = _number(row.get("volume_current", row.get("volume", row.get("volume_initial"))), f"{symbol} exposure volume", positive=True)
            entry = _number(row.get("price_open", row.get("price")), f"{symbol} exposure entry price", positive=True)
            notional = abs(volume * entry * contract.contract_size)
            notionals[symbol] = notionals.get(symbol, 0.0) + notional
        if not notionals:
            return 0.0, 0.0
        total = sum(notionals.values())
        pair = notionals.get(default_contract.broker_symbol, 0.0) / equity * 100.0
        concentration = max(notionals.values()) / total * 100.0
        return float(pair), float(concentration)

    def build(
        self,
        *,
        now: datetime,
        contract: InstrumentContract,
        candidate_hash: str,
        deals_start: datetime,
        deals_end: datetime,
        approval: bool | None = None,
    ) -> BrokerSnapshot:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        with self.mutex():
            account = self.read("account_info")
            if account is None:
                raise BrokerFactsError("broker account snapshot is unknown")
            positions_raw = self._collection("positions_get")
            pending_raw = self._collection("orders_get")
            daily_start = current.astimezone(ZoneInfo("America/New_York")).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(timezone.utc)
            deals_raw = self._collection("history_deals_get", daily_start, current)
            account_login = _value(account, "login")
            equity = _number(_value(account, "equity"), "account equity", positive=True)
            margin_free = _number(_value(account, "margin_free"), "account margin_free", nonnegative=True)
            raw_leverage = _value(account, "leverage")
            leverage = None if raw_leverage is None else _number(raw_leverage, "account leverage", positive=True)
            positions = tuple(_row(item) for item in positions_raw)
            pending = tuple(_row(item) for item in pending_raw)
            deals = tuple(_row(item) for item in deals_raw)
            owned_deals = tuple(item for item in deals if self.strategy_matcher(item))
            daily_realized_r = self._daily_realized_r(owned_deals)
            total_stop_risk = self._stop_risk(positions + pending, contract)
            pair_exposure_percent, concentration_percent = self._exposure_metrics(positions + pending, contract, equity)
            halt = self.halt_reader()
            if not isinstance(halt, bool):
                raise BrokerFactsError("canonical HALT state is unknown")
            health = self.strategy_health_reader()
            if not isinstance(health, str) or health not in {"UNKNOWN", "WARMUP", "ACTIVE", "MONITORING", "DECAYED", "DISABLED"}:
                raise BrokerFactsError("canonical strategy health state is unknown")
            payload = {
                "as_of": current.isoformat(), "account_login": account_login, "equity": equity,
                "margin_free": margin_free, "positions": positions, "pending_orders": pending,
                "deals": deals, "candidate_hash": candidate_hash, "daily_realized_r": daily_realized_r,
                "total_stop_risk": total_stop_risk, "halt": halt, "strategy_health": health,
                "approval": approval,
                "leverage": leverage,
                "pair_exposure_percent": pair_exposure_percent,
                "concentration_percent": concentration_percent,
            }
            return BrokerSnapshot(
                current, equity, margin_free, open_positions=positions, pending_orders=pending,
                daily_realized_r=daily_realized_r, strategy_health=health, approval=approval, halt=halt,
                instrument_contract=contract, total_stop_risk=total_stop_risk, deals=deals,
                leverage=leverage,
                pair_exposure_percent=pair_exposure_percent,
                concentration_percent=concentration_percent,
                snapshot_hash=hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode("utf-8")).hexdigest(),
                order_calc_profit=self.order_calc_profit, order_calc_margin=self.order_calc_margin,
            )
