"""Fail-closed broker facts used by every production risk snapshot."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from numbers import Real
from typing import Any, Callable, Mapping

from .contracts import BrokerSnapshot, InstrumentContract
from .deal_ingestion import TerminalFactSnapshot


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
        terminal_fact_reader: Callable[[datetime], TerminalFactSnapshot],
        strategy_matcher: Callable[[Mapping[str, Any]], bool],
        mutex: Callable[[], Any] | None = None,
        contract_resolver: Callable[[str], InstrumentContract] | None = None,
        deal_out_values: set[Any] | None = None,
        deal_in_values: set[Any] | None = None,
        deal_inout_values: set[Any] | None = None,
        deal_out_by_values: set[Any] | None = None,
    ) -> None:
        self.read = read
        self.order_calc_profit = order_calc_profit
        self.order_calc_margin = order_calc_margin
        self.halt_reader = halt_reader
        self.strategy_health_reader = strategy_health_reader
        self.terminal_fact_reader = terminal_fact_reader
        self.strategy_matcher = strategy_matcher
        self.mutex = mutex or (lambda: nullcontext())
        self.contract_resolver = contract_resolver
        self.deal_out_values = deal_out_values or {1, "1", "OUT", "DEAL_ENTRY_OUT"}
        self.deal_in_values = deal_in_values or {0, "0", "IN", "DEAL_ENTRY_IN"}
        self.deal_inout_values = deal_inout_values or {2, "2", "INOUT", "DEAL_ENTRY_INOUT"}
        self.deal_out_by_values = deal_out_by_values or {3, "3", "OUT_BY", "DEAL_ENTRY_OUT_BY"}

    def _collection(self, operation: str, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        value = self.read(operation, *args, **kwargs)
        if value is None:
            raise BrokerFactsError(f"broker {operation} returned unknown state")
        if not isinstance(value, (tuple, list)):
            raise BrokerFactsError(f"broker {operation} returned a non-collection")
        return tuple(value)

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
        approval: bool | None = None,
        policy_hash: str = "",
    ) -> BrokerSnapshot:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        with self.mutex():
            account = self.read("account_info")
            if account is None:
                raise BrokerFactsError("broker account snapshot is unknown")
            positions_raw = self._collection("positions_get")
            pending_raw = self._collection("orders_get")
            terminal = self.terminal_fact_reader(current)
            if not isinstance(terminal, TerminalFactSnapshot):
                raise BrokerFactsError("canonical terminal fact snapshot is missing")
            if terminal.as_of > current or current - terminal.reconciliation_at > timedelta(hours=24):
                raise BrokerFactsError("canonical terminal fact snapshot is stale")
            account_login = _value(account, "login")
            equity = _number(_value(account, "equity"), "account equity", positive=True)
            margin_free = _number(_value(account, "margin_free"), "account margin_free", nonnegative=True)
            raw_leverage = _value(account, "leverage")
            leverage = None if raw_leverage is None else _number(raw_leverage, "account leverage", positive=True)
            positions = tuple(_row(item) for item in positions_raw)
            pending = tuple(_row(item) for item in pending_raw)
            deals = tuple(dict(item) for item in terminal.closed_episodes)
            owned_positions = tuple(item for item in positions if self.strategy_matcher(item))
            owned_pending = tuple(item for item in pending if self.strategy_matcher(item))
            daily_realized_r = float(terminal.daily_realized_r)
            total_stop_risk = self._stop_risk(positions + pending, contract)
            pair_exposure_percent, concentration_percent = self._exposure_metrics(positions + pending, contract, equity)
            halt = self.halt_reader()
            if not isinstance(halt, bool):
                raise BrokerFactsError("canonical HALT state is unknown")
            health = self.strategy_health_reader()
            if not isinstance(health, str) or health not in {"UNKNOWN", "WARMUP", "ACTIVE", "MONITORING", "DECAYED", "DISABLED"}:
                raise BrokerFactsError("canonical strategy health state is unknown")
            def instrument_id(row: Mapping[str, Any]) -> str:
                symbol = str(row.get("symbol") or "")
                resolved = contract if symbol == contract.broker_symbol else (self.contract_resolver(symbol) if self.contract_resolver else None)
                if resolved is None:
                    raise BrokerFactsError(f"no signed contract for owned symbol {symbol}")
                return resolved.instrument_id

            owned_deal_ids = list(terminal.owned_deal_ids)
            last_entry: datetime | None = None
            last_loss: datetime | None = None
            entry_counts = dict(terminal.entry_counts_by_instrument)
            position_counts: dict[str, int] = {}
            pending_counts: dict[str, int] = {}
            for row in owned_positions: position_counts[instrument_id(row)] = position_counts.get(instrument_id(row), 0) + 1
            for row in owned_pending: pending_counts[instrument_id(row)] = pending_counts.get(instrument_id(row), 0) + 1
            payload = {
                "as_of": current.isoformat(), "account_login": account_login, "equity": equity,
                "margin_free": margin_free, "positions": positions, "pending_orders": pending,
                "deals": deals, "candidate_hash": candidate_hash, "daily_realized_r": daily_realized_r,
                "total_stop_risk": total_stop_risk, "halt": halt, "strategy_health": health,
                "approval": approval,
                "leverage": leverage,
                "pair_exposure_percent": pair_exposure_percent,
                "concentration_percent": concentration_percent,
                "total_entry_count": terminal.total_entry_count, "entry_counts_by_instrument": entry_counts,
                "open_position_counts_by_instrument": position_counts, "pending_order_counts_by_instrument": pending_counts,
                "last_accepted_entry_at": None if last_entry is None else last_entry.isoformat(),
                "last_terminal_loss_at": None if last_loss is None else last_loss.isoformat(),
                "owned_deal_ids": owned_deal_ids, "policy_hash": policy_hash,
                "instrument_contract_hash": contract.contract_hash,
                "deal_facts_hash": terminal.deal_facts_hash,
                "deal_reconciliation_at": terminal.reconciliation_at.isoformat(),
                "deal_watermark": [terminal.high_water_time_msc, terminal.high_water_ticket],
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
                total_entry_count=terminal.total_entry_count, entry_counts_by_instrument=entry_counts,
                open_position_counts_by_instrument=position_counts, pending_order_counts_by_instrument=pending_counts,
                last_accepted_entry_at=last_entry, last_terminal_loss_at=last_loss,
                owned_deal_ids=tuple(owned_deal_ids), policy_hash=policy_hash,
                instrument_contract_hash=contract.contract_hash,
                deal_facts_hash=terminal.deal_facts_hash,
                deal_reconciliation_at=terminal.reconciliation_at,
                deal_watermark_time_msc=terminal.high_water_time_msc,
                deal_watermark_ticket=terminal.high_water_ticket,
            )
