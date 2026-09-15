"""Fail-closed broker facts used by every production risk snapshot."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import math
from numbers import Real
from threading import Lock
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

from .contracts import BrokerSnapshot, InstrumentContract
from .deal_ingestion import DealIngestionError, daily_deals, last_account_entry_time, realized_r


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
        deal_in_values: set[Any] | None = None,
        deal_inout_values: set[Any] | None = None,
        deal_out_by_values: set[Any] | None = None,
        whitelist_instrument_ids: Sequence[str] | None = None,
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
        self.deal_in_values = deal_in_values or {0, "0", "IN", "DEAL_ENTRY_IN"}
        self.deal_inout_values = deal_inout_values or {2, "2", "INOUT", "DEAL_ENTRY_INOUT"}
        self.deal_out_by_values = deal_out_by_values or {3, "3", "OUT_BY", "DEAL_ENTRY_OUT_BY"}
        self.whitelist_instrument_ids = tuple(dict.fromkeys(str(value) for value in (whitelist_instrument_ids or ())))

    def _collection(self, operation: str, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        value = self.read(operation, *args, **kwargs)
        if value is None:
            raise BrokerFactsError(f"broker {operation} returned unknown state")
        if not isinstance(value, (tuple, list)):
            raise BrokerFactsError(f"broker {operation} returned a non-collection")
        return tuple(value)

    def _daily_realized_r(self, deals: tuple[dict[str, Any], ...]) -> float:
        try:
            return realized_r(
                deals,
                exit_values=self.deal_out_values,
                inout_values=self.deal_inout_values,
                out_by_values=self.deal_out_by_values,
                known_values=self.deal_in_values | self.deal_out_values | self.deal_inout_values | self.deal_out_by_values,
                starting_risk_reader=self.starting_risk_reader,
            )
        except DealIngestionError as exc:
            raise BrokerFactsError(str(exc)) from exc
    def _stop_risk(self, rows: tuple[dict[str, Any], ...], default_contract: InstrumentContract) -> float:
        total = 0.0
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if not symbol:
                raise BrokerFactsError("exposure symbol is missing")
            contract = self._resolve_contract(symbol, default_contract)
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
            contract = self._resolve_contract(symbol, default_contract)
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

    def _resolve_contract(self, symbol: str, default_contract: InstrumentContract) -> InstrumentContract:
        if symbol == default_contract.broker_symbol:
            return default_contract
        if self.contract_resolver is None:
            raise BrokerFactsError(f"no signed contract for broker symbol {symbol}")
        try:
            resolved = self.contract_resolver(symbol)
        except Exception as exc:
            raise BrokerFactsError(f"no signed contract for broker symbol {symbol}") from exc
        if not isinstance(resolved, InstrumentContract):
            raise BrokerFactsError(f"broker symbol {symbol} resolved to an invalid contract")
        return resolved

    def _account_entry_counts(
        self,
        deals: tuple[dict[str, Any], ...],
        positions: tuple[dict[str, Any], ...],
        pending: tuple[dict[str, Any], ...],
        default_contract: InstrumentContract,
    ) -> tuple[int, dict[str, int], dict[str, int], dict[str, int]]:
        """Count account-day entries and all exposure reservations.

        This deliberately does not apply the strategy/campaign matcher.  A
        second campaign on the same account must consume the same daily slots,
        and an unknown exposure is a broker-facts failure rather than zero.
        """
        known = self.deal_in_values | self.deal_inout_values | self.deal_out_values | self.deal_out_by_values
        entry_keys: set[tuple[str, str]] = set()
        instrument_ids = set(self.whitelist_instrument_ids) | {default_contract.instrument_id}
        entry_counts: dict[str, int] = {instrument_id: 0 for instrument_id in instrument_ids}
        for deal in deals:
            entry = deal.get("entry")
            if entry not in known:
                raise BrokerFactsError("account deal has an unknown entry type")
            if entry not in self.deal_in_values | self.deal_inout_values:
                continue
            symbol = str(deal.get("symbol") or "")
            if not symbol:
                raise BrokerFactsError("account entry deal has no symbol")
            resolved = self._resolve_contract(symbol, default_contract)
            identity = str(deal.get("order") or deal.get("position_id") or deal.get("deal_id") or "").strip()
            if not identity:
                raise BrokerFactsError("account entry deal has no order/position identity")
            key = (identity, resolved.instrument_id)
            if key in entry_keys:
                continue
            entry_keys.add(key)
            entry_counts[resolved.instrument_id] = entry_counts.get(resolved.instrument_id, 0) + 1

        def exposure_counts(rows: tuple[dict[str, Any], ...], field: str) -> dict[str, int]:
            result: dict[str, int] = {instrument_id: 0 for instrument_id in instrument_ids}
            for row in rows:
                symbol = str(row.get("symbol") or "")
                resolved = self._resolve_contract(symbol, default_contract)
                result[resolved.instrument_id] = result.get(resolved.instrument_id, 0) + 1
            return result

        return len(entry_keys), entry_counts, exposure_counts(positions, "positions"), exposure_counts(pending, "pending")

    def build(
        self,
        *,
        now: datetime,
        contract: InstrumentContract,
        candidate_hash: str,
        deals_start: datetime,
        deals_end: datetime,
        approval: bool | None = None,
        policy_hash: str = "",
    ) -> BrokerSnapshot:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if not isinstance(deals_start, datetime) or not isinstance(deals_end, datetime):
            raise BrokerFactsError("broker history interval is missing")
        start = deals_start if deals_start.tzinfo is not None else deals_start.replace(tzinfo=timezone.utc)
        end = deals_end if deals_end.tzinfo is not None else deals_end.replace(tzinfo=timezone.utc)
        if start > end:
            raise BrokerFactsError("broker history interval is invalid")
        with self.mutex():
            query_started_at = datetime.now(timezone.utc)
            query_sequence = self._next_query_sequence()
            account = self.read("account_info")
            if account is None:
                raise BrokerFactsError("broker account snapshot is unknown")
            positions_raw = self._collection("positions_get")
            pending_raw = self._collection("orders_get")
            daily_start = current.astimezone(ZoneInfo("America/New_York")).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(timezone.utc)
            history_start = min(start, daily_start)
            history_end = max(end, current)
            deals_raw = self._collection("history_deals_get", history_start, history_end)
            account_login = _value(account, "login")
            if isinstance(account_login, bool) or not isinstance(account_login, (str, int)) or not str(account_login).strip():
                raise BrokerFactsError("account login is missing")
            equity = _number(_value(account, "equity"), "account equity", positive=True)
            margin_free = _number(_value(account, "margin_free"), "account margin_free", nonnegative=True)
            raw_leverage = _value(account, "leverage")
            leverage = None if raw_leverage is None else _number(raw_leverage, "account leverage", positive=True)
            positions = tuple(_row(item) for item in positions_raw)
            pending = tuple(_row(item) for item in pending_raw)
            deals = tuple(_row(item) for item in deals_raw)
            try:
                daily_deal_rows = daily_deals(deals, day_start=daily_start, day_end=current)
            except DealIngestionError as exc:
                raise BrokerFactsError(str(exc)) from exc
            owned_deals = tuple(item for item in deals if self.strategy_matcher(item))
            owned_positions = tuple(item for item in positions if self.strategy_matcher(item))
            owned_pending = tuple(item for item in pending if self.strategy_matcher(item))
            # Validate every account closing deal, not only today's slice.
            # A malformed historical deal must never disappear behind a
            # campaign filter or become a healthy zero.
            self._daily_realized_r(deals)
            daily_realized_r = self._daily_realized_r(daily_deal_rows)
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
                return self._resolve_contract(symbol, contract).instrument_id

            owned_deal_ids: list[str] = []
            for deal in owned_deals:
                owned_deal_ids.append(str(deal.get("deal_id") or deal.get("ticket") or ""))
            try:
                last_entry = last_account_entry_time(
                    deals,
                    in_values=self.deal_in_values,
                    inout_values=self.deal_inout_values,
                    exit_values=self.deal_out_values,
                    out_by_values=self.deal_out_by_values,
                )
            except DealIngestionError as exc:
                raise BrokerFactsError(str(exc)) from exc
            total_entry_count, entry_counts, position_counts, pending_counts = self._account_entry_counts(
                daily_deal_rows, positions, pending, contract
            )
            query_completed_at = datetime.now(timezone.utc)
            query_id = uuid4().hex
            deal_facts_hash = hashlib.sha256(
                json.dumps(deals, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode("utf-8")
            ).hexdigest()
            watermark_time_msc = 0
            watermark_ticket = 0
            for deal in deals:
                timestamp = deal.get("time_msc")
                if timestamp is not None:
                    watermark_time_msc = max(watermark_time_msc, int(timestamp))
                ticket = deal.get("ticket", deal.get("deal_id"))
                if isinstance(ticket, int) and not isinstance(ticket, bool):
                    watermark_ticket = max(watermark_ticket, ticket)
            payload = {
                "as_of": current.isoformat(), "account_login": account_login, "equity": equity,
                "margin_free": margin_free, "positions": positions, "pending_orders": pending,
                "deals": deals, "candidate_hash": candidate_hash, "daily_realized_r": daily_realized_r,
                "total_stop_risk": total_stop_risk, "halt": halt, "strategy_health": health,
                "approval": approval,
                "leverage": leverage,
                "pair_exposure_percent": pair_exposure_percent,
                "concentration_percent": concentration_percent,
                "total_entry_count": total_entry_count, "entry_counts_by_instrument": entry_counts,
                "open_position_counts_by_instrument": position_counts, "pending_order_counts_by_instrument": pending_counts,
                "last_accepted_entry_at": None if last_entry is None else last_entry.isoformat(),
                "owned_deal_ids": owned_deal_ids, "policy_hash": policy_hash,
                "instrument_contract_hash": contract.contract_hash,
                "account_login": str(account_login), "broker_query_id": query_id,
                "query_started_at": query_started_at.isoformat(), "query_completed_at": query_completed_at.isoformat(),
                "broker_query_sequence": query_sequence, "broker_read_operations": ("account_info", "positions_get", "orders_get", "history_deals_get"),
                "history_start": history_start.isoformat(), "history_end": history_end.isoformat(),
                "deal_facts_hash": deal_facts_hash,
                "deal_reconciliation_at": current.isoformat(),
                "deal_watermark": [watermark_time_msc, watermark_ticket],
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
                total_entry_count=total_entry_count, entry_counts_by_instrument=entry_counts,
                open_position_counts_by_instrument=position_counts, pending_order_counts_by_instrument=pending_counts,
                last_accepted_entry_at=last_entry,
                owned_deal_ids=tuple(owned_deal_ids), policy_hash=policy_hash,
                instrument_contract_hash=contract.contract_hash,
                account_login=str(account_login), broker_query_id=query_id,
                deal_facts_hash=deal_facts_hash,
                deal_reconciliation_at=current,
                deal_watermark_time_msc=watermark_time_msc,
                deal_watermark_ticket=watermark_ticket,
                query_started_at=query_started_at, query_completed_at=query_completed_at,
                broker_query_sequence=query_sequence,
                broker_read_operations=("account_info", "positions_get", "orders_get", "history_deals_get"),
            )

    _query_sequence_lock = Lock()
    _query_sequence = 0

    @classmethod
    def _next_query_sequence(cls) -> int:
        with cls._query_sequence_lock:
            cls._query_sequence += 1
            return cls._query_sequence
