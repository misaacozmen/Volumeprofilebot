"""Durable, replay-safe ingestion of immutable terminal deal facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo


class TerminalDealIngestionError(RuntimeError):
    pass


DECIMAL_FIELDS = ("volume", "price", "profit", "commission", "swap", "fee")
IMMUTABLE_FIELDS = (
    "account_key", "campaign_id", "candidate_hash", "deal_ticket", "order_ticket",
    "position_id", "symbol", "magic", "comment", "deal_type", "entry_type", "reason",
    "time_msc", *DECIMAL_FIELDS, "source_window_hash",
)
ENTRY_IN = {0, "0", "IN", "DEAL_ENTRY_IN"}
ENTRY_OUT = {1, "1", "OUT", "DEAL_ENTRY_OUT"}
ENTRY_INOUT = {2, "2", "INOUT", "DEAL_ENTRY_INOUT"}
ENTRY_OUT_BY = {3, "3", "OUT_BY", "DEAL_ENTRY_OUT_BY"}


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _row_mapping(row: Any) -> dict[str, object]:
    if isinstance(row, Mapping):
        return {str(key): _json_safe(value) for key, value in row.items()}
    if hasattr(row, "_asdict"):
        return {str(key): _json_safe(value) for key, value in row._asdict().items()}
    try:
        return {str(key): _json_safe(value) for key, value in vars(row).items()}
    except TypeError as exc:
        raise TerminalDealIngestionError("terminal query row is not inspectable") from exc


def _row_identity(row: Any) -> tuple[object, ...]:
    values = _row_mapping(row)
    canonical_ticket = values.get("ticket")
    if canonical_ticket in (None, "", 0):
        canonical_ticket = values.get("deal_id")
    # Keep the canonical ticket explicit while retaining every provider field
    # so an auxiliary query cannot attest a different immutable deal row.
    values["_canonical_ticket"] = canonical_ticket
    return (_canonical_json(values),)


def _multiset_hash(rows: Iterable[Any]) -> str:
    encoded = sorted(_canonical_json(_row_mapping(row)) for row in rows)
    return _hash(encoded)


def _parse_utc(value: datetime | str, label: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TerminalDealIngestionError(f"{label} is not ISO-8601") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TerminalDealIngestionError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


class TerminalQueryBatch:
    """Complete, attested result for one closed terminal history window."""

    __slots__ = ("rows", "complete", "start_utc", "end_utc", "queried_at_utc", "query_fingerprint", "attestation_fingerprint", "evidence")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.pop("_factory_token", None) is not _TERMINAL_BATCH_FACTORY_TOKEN:
            raise TypeError("TerminalQueryBatch must be created by build_terminal_query_batch")
        names = ("rows", "complete", "start_utc", "end_utc", "queried_at_utc", "query_fingerprint", "attestation_fingerprint", "evidence")
        values = dict(zip(names, args))
        values.update(kwargs)
        self.rows = tuple(values["rows"])
        self.complete = values["complete"]
        self.start_utc = values["start_utc"]
        self.end_utc = values["end_utc"]
        self.queried_at_utc = values["queried_at_utc"]
        self.query_fingerprint = values["query_fingerprint"]
        self.attestation_fingerprint = values["attestation_fingerprint"]
        self.evidence = dict(values.get("evidence") or {})
        self._validate()

    def _validate(self) -> None:
        if self.complete is not True:
            raise TerminalDealIngestionError("terminal query batch is not complete")
        object.__setattr__(self, "rows", tuple(self.rows))
        for name in ("start_utc", "end_utc", "queried_at_utc"):
            object.__setattr__(self, name, _parse_utc(getattr(self, name), name))
        if self.start_utc >= self.end_utc:
            raise TerminalDealIngestionError("terminal query batch has an invalid range")
        for name in ("query_fingerprint", "attestation_fingerprint"):
            value = str(getattr(self, name) or "")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", value) or len(set(value.lower())) == 1:
                raise TerminalDealIngestionError(f"{name} is not a SHA-256 digest")
            setattr(self, name, value.lower())
        expected_query = _hash(self.evidence.get("query_context", {}))
        if self.evidence and expected_query != self.query_fingerprint:
            raise TerminalDealIngestionError("terminal query fingerprint does not match evidence")
        expected_attestation = _hash(self.evidence.get("attestation", {}))
        if self.evidence and expected_attestation != self.attestation_fingerprint:
            raise TerminalDealIngestionError("terminal attestation fingerprint does not match evidence")

    def assert_range(self, start: datetime, end: datetime) -> None:
        if self.start_utc != _parse_utc(start, "requested start") or self.end_utc != _parse_utc(end, "requested end"):
            raise TerminalDealIngestionError("terminal query batch range differs from requested window")


def build_terminal_query_batch(
    *,
    rows: Iterable[Any],
    repeated_rows: Iterable[Any],
    orders: Iterable[Any],
    ticket_deals: Iterable[Any],
    position_deals: Iterable[Any],
    local_intents: Iterable[Any] | bool = (),
    local_snapshot: Mapping[str, Any] | None = None,
    query_context: Mapping[str, Any] | None = None,
    start_utc: datetime,
    end_utc: datetime,
    queried_at_utc: datetime,
) -> TerminalQueryBatch:
    """Construct a batch only after all range/ticket/position attestations pass."""
    first = tuple(rows)
    second = tuple(repeated_rows)
    order_rows = tuple(orders)
    ticket_rows = tuple(ticket_deals)
    position_rows = tuple(position_deals)
    start = _parse_utc(start_utc, "start_utc")
    end = _parse_utc(end_utc, "end_utc")
    if start >= end:
        raise TerminalDealIngestionError("terminal query batch has an invalid range")
    for collection in (first, second, ticket_rows, position_rows):
        for item in collection:
            timestamp = _row_mapping(item).get("time_msc")
            if timestamp in (None, ""):
                continue
            try:
                observed_time = datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OverflowError, OSError) as exc:
                raise TerminalDealIngestionError("terminal query row has an invalid time_msc") from exc
            if not start <= observed_time <= end:
                raise TerminalDealIngestionError("terminal query row is outside its attested range")
    first_hash = _multiset_hash(first)
    second_hash = _multiset_hash(second)
    if first_hash != second_hash:
        raise TerminalDealIngestionError("terminal history range changed between repeated queries")
    range_counts = Counter(_row_identity(item) for item in first)
    for collection in (ticket_rows, position_rows):
        remaining = Counter(range_counts)
        for item in collection:
            key = _row_identity(item)
            if remaining[key] <= 0:
                raise TerminalDealIngestionError("ticket/position deal is absent from the attested range")
            remaining[key] -= 1
    local_rows = tuple(local_intents) if not isinstance(local_intents, bool) else ()
    snapshot = dict(local_snapshot or {"counts": {}, "hashes": {}})
    has_local_intents = bool(local_intents) if isinstance(local_intents, bool) else bool(local_rows)
    active_snapshot = any(int(value) > 0 for value in dict(snapshot.get("counts") or {}).values())
    if not first and (second or order_rows or has_local_intents or active_snapshot):
        raise TerminalDealIngestionError("empty terminal range conflicts with orders or local risk intent")
    queried = _parse_utc(queried_at_utc, "queried_at_utc")
    context = dict(query_context or {})
    context.setdefault("query_method_contract_version", "TERMINAL_DEAL_QUERY_V5")
    context.update({"start_utc": start.isoformat(), "end_utc": end.isoformat(), "reconciliation_type": context.get("reconciliation_type", "FULL"), "account_key": context.get("account_key", "unknown"), "campaign_id": context.get("campaign_id", "unknown"), "candidate_hash": context.get("candidate_hash", "unknown"), "magic": context.get("magic", 0), "comment_prefix": context.get("comment_prefix", ""), "private_terminal_binding_hash": context.get("private_terminal_binding_hash", "unknown")})
    query_fingerprint = _hash(context)
    evidence = {
        "query_context": context,
        "attestation": {
            "query_fingerprint": query_fingerprint,
        "range_hash": first_hash,
        "repeated_range_hash": second_hash,
        "orders_hash": _multiset_hash(order_rows),
        "ticket_deals_hash": _multiset_hash(ticket_rows),
        "position_deals_hash": _multiset_hash(position_rows),
        "local_intents": has_local_intents,
            "local_snapshot": snapshot,
        },
    }
    attestation_fingerprint = _hash(evidence["attestation"])
    return TerminalQueryBatch(first, True, start, end, queried, query_fingerprint, attestation_fingerprint, evidence, _factory_token=_TERMINAL_BATCH_FACTORY_TOKEN)


_TERMINAL_BATCH_FACTORY_TOKEN = object()


def _value(row: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _decimal_text(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> str:
    if isinstance(value, bool):
        raise TerminalDealIngestionError(f"{field} is not numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TerminalDealIngestionError(f"{field} is not numeric") from exc
    if not number.is_finite() or (positive and number <= 0) or (nonnegative and number < 0):
        raise TerminalDealIngestionError(f"{field} is outside its numeric contract")
    return format(number.normalize(), "f")


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise TerminalDealIngestionError(f"{field} is invalid")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TerminalDealIngestionError(f"{field} is invalid") from exc
    if str(value).strip() not in {str(number), f"{number}.0"} and not isinstance(value, int):
        raise TerminalDealIngestionError(f"{field} is not an integer")
    if positive and number <= 0:
        raise TerminalDealIngestionError(f"{field} must be positive")
    return number


@dataclass(frozen=True, slots=True)
class TerminalFactSnapshot:
    as_of: datetime
    deal_facts_hash: str
    reconciliation_at: datetime
    high_water_time_msc: int
    high_water_ticket: int
    daily_realized_r: Decimal
    total_entry_count: int
    entry_counts_by_instrument: Mapping[str, int]
    owned_deal_ids: tuple[str, ...]
    closed_episodes: tuple[Mapping[str, Any], ...]


class TerminalDealIngestor:
    """Reads deterministic time windows first, then commits all derived facts atomically."""

    def __init__(
        self, path: str | Path, *, read_deals: Callable[[datetime, datetime], Iterable[Any]],
        account_key: str, campaign_id: str, candidate_hash: str, campaign_start: datetime,
        magic: int, comment_prefix: str = "", now: Callable[[], datetime] | None = None,
        terminal_history_days: int = 365, private_terminal_binding_hash: str = "unknown",
    ) -> None:
        self.path = Path(path)
        self.read_deals = read_deals
        self.account_key = str(account_key)
        self.campaign_id = str(campaign_id)
        self.candidate_hash = str(candidate_hash)
        self.campaign_start = self._utc(campaign_start)
        self.magic = _integer(magic, "magic")
        self.comment_prefix = str(comment_prefix)
        self.terminal_history_days = int(terminal_history_days)
        self.private_terminal_binding_hash = str(private_terminal_binding_hash)
        if self.terminal_history_days < 365:
            raise TerminalDealIngestionError("terminal history must cover at least 365 days")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.private_terminal_binding_hash) or len(set(self.private_terminal_binding_hash.lower())) == 1:
            raise TerminalDealIngestionError("private terminal binding hash is incomplete")
        self.now = now or (lambda: datetime.now(timezone.utc))
        if not self.account_key or not self.campaign_id or len(self.candidate_hash) != 64:
            raise TerminalDealIngestionError("terminal ingestion identity binding is incomplete")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._schema(connection)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TerminalDealIngestionError("terminal ingestion timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
            connection.close()
            raise TerminalDealIngestionError("terminal ingestion requires SQLite WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS terminal_deals(
              account_key TEXT NOT NULL, campaign_id TEXT NOT NULL, candidate_hash TEXT NOT NULL,
              deal_ticket INTEGER NOT NULL, order_ticket INTEGER NOT NULL, position_id TEXT NOT NULL,
              symbol TEXT NOT NULL, magic INTEGER NOT NULL, comment TEXT NOT NULL, deal_type TEXT NOT NULL,
              entry_type TEXT NOT NULL, reason TEXT NOT NULL, time_msc INTEGER NOT NULL,
              volume TEXT NOT NULL, price TEXT NOT NULL, profit TEXT NOT NULL, commission TEXT NOT NULL,
              swap TEXT NOT NULL, fee TEXT NOT NULL, source_window_hash TEXT NOT NULL,
              first_seen_at_utc TEXT NOT NULL, canonical_fact_hash TEXT NOT NULL,
              PRIMARY KEY(account_key,deal_ticket));
            CREATE TABLE IF NOT EXISTS terminal_query_windows(
              query_hash TEXT PRIMARY KEY, account_key TEXT NOT NULL, start_utc TEXT NOT NULL,
              end_utc TEXT NOT NULL, result_hash TEXT NOT NULL, deal_count INTEGER NOT NULL,
              queried_at_utc TEXT NOT NULL, reconciliation INTEGER NOT NULL,
              attestation_fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS terminal_ingestion_cursor(
              account_key TEXT PRIMARY KEY, high_water_time_msc INTEGER NOT NULL,
              high_water_ticket INTEGER NOT NULL, last_full_reconciliation_utc TEXT NOT NULL,
              updated_at_utc TEXT NOT NULL, last_attestation_fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entry_risk_intents(
              account_key TEXT NOT NULL, proposal_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
              candidate_hash TEXT NOT NULL, instrument_id TEXT NOT NULL, request_hash TEXT NOT NULL,
              approved_volume TEXT NOT NULL, approved_risk_cash TEXT NOT NULL, created_at_utc TEXT NOT NULL,
              canonical_intent_hash TEXT NOT NULL, PRIMARY KEY(account_key,proposal_id));
            CREATE TABLE IF NOT EXISTS broker_order_bindings(
              account_key TEXT NOT NULL, order_ticket INTEGER NOT NULL, proposal_id TEXT NOT NULL,
              campaign_id TEXT NOT NULL, candidate_hash TEXT NOT NULL, bound_at_utc TEXT NOT NULL,
              PRIMARY KEY(account_key,order_ticket));
            CREATE TABLE IF NOT EXISTS position_entry_allocations(
              account_key TEXT NOT NULL, position_id TEXT NOT NULL, deal_ticket INTEGER NOT NULL,
              proposal_id TEXT NOT NULL, episode INTEGER NOT NULL, filled_volume TEXT NOT NULL, allocated_risk_cash TEXT NOT NULL,
              PRIMARY KEY(account_key,deal_ticket));
            CREATE TABLE IF NOT EXISTS closed_position_episodes(
              account_key TEXT NOT NULL, position_id TEXT NOT NULL, episode INTEGER NOT NULL,
              candidate_hash TEXT NOT NULL, opened_at_msc INTEGER NOT NULL, closed_at_msc INTEGER NOT NULL,
              starting_risk_cash TEXT NOT NULL, net_cash TEXT NOT NULL, net_r TEXT NOT NULL,
              deal_tickets_json TEXT NOT NULL, canonical_episode_hash TEXT NOT NULL,
              PRIMARY KEY(account_key,position_id,episode));
            CREATE TABLE IF NOT EXISTS daily_risk_slots(
              account_key TEXT NOT NULL, campaign_id TEXT NOT NULL, session_date TEXT NOT NULL,
              instrument_id TEXT NOT NULL, proposal_id TEXT NOT NULL, candidate_hash TEXT NOT NULL,
              request_hash TEXT NOT NULL, state TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
              PRIMARY KEY(account_key,proposal_id));
            CREATE TABLE IF NOT EXISTS strategy_health_state(
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), state TEXT NOT NULL,
              baseline_candidate_hash TEXT NOT NULL, baseline_json TEXT NOT NULL,
              metrics_json TEXT NOT NULL, updated_at_utc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_outbox(
              outbox_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
              payload_hash TEXT NOT NULL, created_at_utc TEXT NOT NULL, delivered_at_utc TEXT);
            """
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(terminal_query_windows)")}
        if "attestation_fingerprint" not in columns:
            connection.execute(
                "ALTER TABLE terminal_query_windows ADD COLUMN attestation_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        cursor_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(terminal_ingestion_cursor)")}
        if "last_attestation_fingerprint" not in cursor_columns:
            connection.execute(
                "ALTER TABLE terminal_ingestion_cursor ADD COLUMN last_attestation_fingerprint TEXT NOT NULL DEFAULT ''"
            )

    def record_entry_risk_intent(
        self, *, proposal_id: str, instrument_id: str, request_hash: str,
        approved_volume: Any, approved_risk_cash: Any, created_at: datetime | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        row = {
            "account_key": self.account_key, "proposal_id": str(proposal_id),
            "campaign_id": self.campaign_id, "candidate_hash": self.candidate_hash,
            "instrument_id": str(instrument_id), "request_hash": str(request_hash),
            "approved_volume": _decimal_text(approved_volume, "approved volume", positive=True),
            "approved_risk_cash": _decimal_text(approved_risk_cash, "approved risk", positive=True),
            "created_at_utc": self._utc(created_at or self.now()).isoformat(),
        }
        if not row["proposal_id"] or not row["instrument_id"] or len(row["request_hash"]) != 64:
            raise TerminalDealIngestionError("entry risk intent binding is incomplete")
        fact_hash = _hash(row)
        values = (*row.values(), fact_hash)
        owns = connection is None
        target = connection or self._connect()
        try:
            if owns: target.execute("BEGIN IMMEDIATE")
            existing = target.execute(
                "SELECT account_key,proposal_id,campaign_id,candidate_hash,instrument_id,request_hash,approved_volume,approved_risk_cash,created_at_utc,canonical_intent_hash FROM entry_risk_intents WHERE account_key=? AND proposal_id=?",
                (self.account_key, row["proposal_id"]),
            ).fetchone()
            if existing is not None and tuple(existing) != values:
                raise TerminalDealIngestionError("entry risk intent immutable conflict")
            if existing is None:
                target.execute("INSERT INTO entry_risk_intents VALUES(?,?,?,?,?,?,?,?,?,?)", values)
            if owns: target.commit()
        except Exception:
            if owns and target.in_transaction: target.rollback()
            raise
        finally:
            if owns: target.close()

    def bind_order(self, *, order_ticket: Any, proposal_id: str, bound_at: datetime | None = None, connection: sqlite3.Connection | None = None) -> None:
        ticket = _integer(order_ticket, "order ticket", positive=True)
        values = (self.account_key, ticket, str(proposal_id), self.campaign_id, self.candidate_hash, self._utc(bound_at or self.now()).isoformat())
        owns = connection is None
        target = connection or self._connect()
        try:
            if owns: target.execute("BEGIN IMMEDIATE")
            intent = target.execute("SELECT 1 FROM entry_risk_intents WHERE account_key=? AND proposal_id=?", (self.account_key, proposal_id)).fetchone()
            if intent is None:
                raise TerminalDealIngestionError("broker order has no canonical entry risk intent")
            existing = target.execute("SELECT * FROM broker_order_bindings WHERE account_key=? AND order_ticket=?", (self.account_key, ticket)).fetchone()
            if existing is not None and tuple(existing) != values:
                raise TerminalDealIngestionError("broker order binding immutable conflict")
            if existing is None: target.execute("INSERT INTO broker_order_bindings VALUES(?,?,?,?,?,?)", values)
            if owns: target.commit()
        except Exception:
            if owns and target.in_transaction: target.rollback()
            raise
        finally:
            if owns: target.close()

    def reserve_daily_slot(
        self, *, session_date: str, instrument_id: str, proposal_id: str, request_hash: str,
        max_total: int, max_instrument: int, observed_at: datetime | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if max_total <= 0 or max_instrument <= 0:
            raise TerminalDealIngestionError("daily slot limits are invalid")
        observed = self._utc(observed_at or self.now())
        owns = connection is None
        target = connection or self._connect()
        try:
            if owns: target.execute("BEGIN IMMEDIATE")
            existing = target.execute("SELECT account_key,campaign_id,session_date,instrument_id,proposal_id,candidate_hash,request_hash,state FROM daily_risk_slots WHERE account_key=? AND proposal_id=?", (self.account_key, proposal_id)).fetchone()
            immutable = (self.account_key, self.campaign_id, session_date, instrument_id, proposal_id, self.candidate_hash, request_hash)
            if existing is not None:
                if tuple(existing[:7]) != immutable:
                    raise TerminalDealIngestionError("daily risk slot immutable conflict")
                if str(existing[7]) in {"SEND_ARMED", "WRITE_CLAIMED", "SUBMITTED", "UNKNOWN"}:
                    return
                raise TerminalDealIngestionError("released daily risk slot cannot be reused")
            states = ("SEND_ARMED", "WRITE_CLAIMED", "SUBMITTED", "UNKNOWN")
            placeholders = ",".join("?" for _ in states)
            reservations = target.execute(
                f"SELECT instrument_id,COUNT(*) FROM daily_risk_slots s WHERE account_key=? AND campaign_id=? AND session_date=? AND state IN ({placeholders}) AND NOT EXISTS(SELECT 1 FROM position_entry_allocations a WHERE a.account_key=s.account_key AND a.proposal_id=s.proposal_id) GROUP BY instrument_id",
                (self.account_key, self.campaign_id, session_date, *states),
            ).fetchall()
            reserved = {str(row[0]): int(row[1]) for row in reservations}
            start = datetime.fromisoformat(session_date).replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
            end = start + timedelta(days=1)
            entries = target.execute(
                "SELECT i.instrument_id,COUNT(*) FROM position_entry_allocations a JOIN entry_risk_intents i ON i.account_key=a.account_key AND i.proposal_id=a.proposal_id JOIN terminal_deals d ON d.account_key=a.account_key AND d.deal_ticket=a.deal_ticket WHERE a.account_key=? AND d.time_msc>=? AND d.time_msc<? GROUP BY i.instrument_id",
                (self.account_key, int(start.timestamp() * 1000), int(end.timestamp() * 1000)),
            ).fetchall()
            confirmed = {str(row[0]): int(row[1]) for row in entries}
            total = sum(reserved.values()) + sum(confirmed.values())
            instrument_total = reserved.get(instrument_id, 0) + confirmed.get(instrument_id, 0)
            if total >= max_total or instrument_total >= max_instrument:
                raise TerminalDealIngestionError("daily risk slot limit is active")
            target.execute("INSERT INTO daily_risk_slots VALUES(?,?,?,?,?,?,?,?,?)", (*immutable, "SEND_ARMED", observed.isoformat()))
            if owns: target.commit()
        except Exception:
            if owns and target.in_transaction: target.rollback()
            raise
        finally:
            if owns: target.close()

    def transition_daily_slot(self, *, proposal_id: str, state: str, broker_terminal: bool = False) -> None:
        allowed = {"WRITE_CLAIMED", "SUBMITTED", "UNKNOWN", "RELEASED_REJECTED", "RELEASED_CANCELLED"}
        if state not in allowed or state.startswith("RELEASED_") and not broker_terminal:
            raise TerminalDealIngestionError("daily risk slot transition lacks terminal broker evidence")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM daily_risk_slots WHERE account_key=? AND proposal_id=?", (self.account_key, proposal_id)).fetchone()
            if row is None: raise TerminalDealIngestionError("daily risk slot is missing")
            current = str(row[0])
            transitions = {
                "SEND_ARMED": {"WRITE_CLAIMED", "SUBMITTED", "UNKNOWN", "RELEASED_REJECTED", "RELEASED_CANCELLED"},
                "WRITE_CLAIMED": {"SUBMITTED", "UNKNOWN", "RELEASED_REJECTED", "RELEASED_CANCELLED"},
                "SUBMITTED": {"UNKNOWN", "RELEASED_REJECTED", "RELEASED_CANCELLED"},
                "UNKNOWN": {"SUBMITTED", "RELEASED_REJECTED", "RELEASED_CANCELLED"},
            }
            if state != current and state not in transitions.get(current, set()):
                raise TerminalDealIngestionError("daily risk slot transition is invalid")
            connection.execute("UPDATE daily_risk_slots SET state=?,updated_at_utc=? WHERE account_key=? AND proposal_id=?", (state, self._utc(self.now()).isoformat(), self.account_key, proposal_id))
            connection.commit()

    def _windows(self, observed: datetime, cursor: sqlite3.Row | None, earliest_local_intent: datetime | None = None) -> tuple[list[tuple[datetime, datetime]], bool]:
        full = cursor is None
        if cursor is not None:
            last_full_text = str(cursor[2] or "")
            last_full = datetime.fromisoformat(last_full_text) if last_full_text else observed
            full = observed - last_full >= timedelta(hours=24)
        current_ny_start = observed.astimezone(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        required_start = min(earliest_local_intent or observed, observed - timedelta(days=self.terminal_history_days), current_ny_start)
        start = required_start if full else max(required_start, datetime.fromtimestamp(int(cursor[0]) / 1000, tz=timezone.utc) - timedelta(days=7))
        windows = []
        point = start
        while point < observed:
            end = min(point + timedelta(days=7), observed)
            windows.append((point, end))
            point = end
        return windows or [(start, observed)], full

    def _normalize(self, item: Any, source_window_hash: str) -> dict[str, Any]:
        entry = _value(item, "entry")
        if entry not in ENTRY_IN | ENTRY_OUT | ENTRY_INOUT | ENTRY_OUT_BY:
            raise TerminalDealIngestionError("terminal deal entry type is unknown")
        time_msc = _integer(_value(item, "time_msc"), "deal time_msc", positive=True)
        event_time = datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc)
        bucket_number = max(0, int((event_time - self.campaign_start) // timedelta(days=7)))
        stable_source_hash = _hash({
            "account_key": self.account_key,
            "bucket_start": (self.campaign_start + timedelta(days=7 * bucket_number)).isoformat(),
            "bucket_days": 7,
        })
        row = {
            "account_key": self.account_key, "campaign_id": self.campaign_id,
            "candidate_hash": self.candidate_hash,
            "deal_ticket": _integer(
                _value(item, "ticket") if _value(item, "ticket") not in (None, "", 0) else _value(item, "deal_id"),
                "deal ticket",
                positive=True,
            ),
            "order_ticket": _integer(_value(item, "order"), "deal order ticket", positive=True),
            "position_id": str(_value(item, "position_id") or "").strip(),
            "symbol": str(_value(item, "symbol") or "").strip(),
            "magic": _integer(_value(item, "magic"), "deal magic"),
            "comment": str(_value(item, "comment") or ""), "deal_type": str(_value(item, "type")),
            "entry_type": str(entry), "reason": str(_value(item, "reason") or ""),
            "time_msc": time_msc,
            **{name: _decimal_text(_value(item, name), f"deal {name}", positive=name == "volume") for name in DECIMAL_FIELDS},
            "source_window_hash": stable_source_hash,
        }
        if not row["position_id"] or not row["symbol"]:
            raise TerminalDealIngestionError("terminal deal position/symbol binding is missing")
        return row

    def ingest(self, *, observed_at: datetime | None = None) -> TerminalFactSnapshot:
        observed = self._utc(observed_at or self.now())
        with self._connect() as probe:
            cursor = probe.execute("SELECT high_water_time_msc,high_water_ticket,last_full_reconciliation_utc,last_attestation_fingerprint FROM terminal_ingestion_cursor WHERE account_key=?", (self.account_key,)).fetchone()
            intent_row = probe.execute("SELECT MIN(created_at_utc) FROM entry_risk_intents WHERE account_key=? AND campaign_id=? AND candidate_hash=?", (self.account_key, self.campaign_id, self.candidate_hash)).fetchone()
        earliest_intent = self._utc(datetime.fromisoformat(str(intent_row[0]))) if intent_row and intent_row[0] else None
        windows, full = self._windows(observed, cursor, earliest_intent)
        batches: list[tuple[datetime, datetime, TerminalQueryBatch]] = []
        for start, end in windows:
            try:
                result = self.read_deals(start, end)
                if not isinstance(result, TerminalQueryBatch):
                    raise TerminalDealIngestionError("terminal deal query must return TerminalQueryBatch")
                result.assert_range(start, end)
                if result.evidence:
                    if _hash(result.evidence.get("query_context", {})) != result.query_fingerprint or _hash(result.evidence.get("attestation", {})) != result.attestation_fingerprint:
                        raise TerminalDealIngestionError("terminal query evidence fingerprint mismatch")
                    context = result.evidence.get("query_context", {})
                    if (
                        context.get("account_key") != self.account_key
                        or context.get("campaign_id") != self.campaign_id
                        or context.get("candidate_hash") != self.candidate_hash
                        or int(context.get("magic", self.magic)) != self.magic
                        or context.get("comment_prefix") != self.comment_prefix
                        or context.get("private_terminal_binding_hash") != self.private_terminal_binding_hash
                    ):
                        raise TerminalDealIngestionError("terminal query identity binding mismatch")
                batches.append((start, end, result))
            except Exception as exc:
                    raise TerminalDealIngestionError(f"terminal deal query failed; cursor was not advanced: {exc}") from exc
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            max_pair = (int(cursor[0]), int(cursor[1])) if cursor is not None else (0, 0)
            for start, end, batch in batches:
                query_binding = {"account_key": self.account_key, "start": start.isoformat(), "end": end.isoformat(), "reconciliation": full}
                source_hash = _hash(query_binding)
                rows = [self._normalize(item, source_hash) for item in batch.rows]
                rows.sort(key=lambda row: (row["time_msc"], row["deal_ticket"]))
                result_hash = _hash([{key: row[key] for key in IMMUTABLE_FIELDS} for row in rows])
                for row in rows:
                    self._verify_owned(connection, row)
                    fact_hash = _hash({key: row[key] for key in IMMUTABLE_FIELDS})
                    values = tuple(row[key] for key in IMMUTABLE_FIELDS) + (observed.isoformat(), fact_hash)
                    existing = connection.execute(
                        "SELECT account_key,campaign_id,candidate_hash,deal_ticket,order_ticket,position_id,symbol,magic,comment,deal_type,entry_type,reason,time_msc,volume,price,profit,commission,swap,fee,source_window_hash,first_seen_at_utc,canonical_fact_hash FROM terminal_deals WHERE account_key=? AND deal_ticket=?",
                        (self.account_key, row["deal_ticket"]),
                    ).fetchone()
                    if existing is not None:
                        immutable_existing = tuple(existing[:20])
                        if immutable_existing != tuple(row[key] for key in IMMUTABLE_FIELDS):
                            raise TerminalDealIngestionError("terminal deal immutable conflict")
                    else:
                        connection.execute("INSERT INTO terminal_deals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
                    self._allocate_entry(connection, row)
                    max_pair = max(max_pair, (row["time_msc"], row["deal_ticket"]))
                query_hash = _hash({**query_binding, "result_hash": result_hash})
                connection.execute(
                    "INSERT OR IGNORE INTO terminal_query_windows VALUES(?,?,?,?,?,?,?,?,?)",
                    (query_hash, self.account_key, start.isoformat(), end.isoformat(), result_hash,
                     len(rows), batch.queried_at_utc.isoformat(), int(full), batch.attestation_fingerprint),
                )
            self._rebuild_episodes(connection)
            last_full = observed.isoformat() if full else str(cursor[2])
            attestation_fingerprint = _hash([batch.attestation_fingerprint for _, _, batch in batches])
            connection.execute(
                "INSERT INTO terminal_ingestion_cursor VALUES(?,?,?,?,?,?) ON CONFLICT(account_key) DO UPDATE SET high_water_time_msc=excluded.high_water_time_msc,high_water_ticket=excluded.high_water_ticket,last_full_reconciliation_utc=excluded.last_full_reconciliation_utc,updated_at_utc=excluded.updated_at_utc,last_attestation_fingerprint=excluded.last_attestation_fingerprint",
                (self.account_key, max_pair[0], max_pair[1], last_full, observed.isoformat(), attestation_fingerprint),
            )
            outbox_payload = {"account_key": self.account_key, "deal_facts_hash": self._facts_hash(connection), "watermark": list(max_pair), "observed_at": observed.isoformat()}
            outbox_hash = _hash(outbox_payload)
            connection.execute("INSERT OR IGNORE INTO audit_outbox VALUES(?,?,?,?,?,NULL)", (outbox_hash, "TERMINAL_DEALS_INGESTED", _canonical_json(outbox_payload), outbox_hash, observed.isoformat()))
            connection.commit()
            return self.snapshot(observed_at=observed)
        except Exception:
            if connection.in_transaction: connection.rollback()
            raise
        finally:
            connection.close()

    def _verify_owned(self, connection: sqlite3.Connection, row: Mapping[str, Any]) -> None:
        if row["magic"] != self.magic:
            raise TerminalDealIngestionError("terminal deal magic does not match campaign")
        entry = row["entry_type"]
        order = connection.execute("SELECT campaign_id,candidate_hash FROM broker_order_bindings WHERE account_key=? AND order_ticket=?", (self.account_key, row["order_ticket"])).fetchone()
        position = connection.execute("SELECT 1 FROM position_entry_allocations WHERE account_key=? AND position_id=?", (self.account_key, row["position_id"])).fetchone()
        if entry in {str(value) for value in ENTRY_IN} | {str(value) for value in ENTRY_INOUT}:
            if order is None or tuple(order) != (self.campaign_id, self.candidate_hash):
                raise TerminalDealIngestionError("entry deal has no canonical campaign/order binding")
        elif position is None:
            raise TerminalDealIngestionError("exit deal ownership is unresolved")
        if self.comment_prefix and row["comment"] and not str(row["comment"]).startswith(self.comment_prefix):
            raise TerminalDealIngestionError("terminal deal comment conflicts with canonical binding")

    def _allocate_entry(self, connection: sqlite3.Connection, row: Mapping[str, Any]) -> None:
        entry_in = {str(value) for value in ENTRY_IN}
        entry_inout = {str(value) for value in ENTRY_INOUT}
        if row["entry_type"] not in entry_in | entry_inout:
            return
        prior = connection.execute(
            "SELECT entry_type,volume FROM terminal_deals WHERE account_key=? AND position_id=? AND (time_msc<? OR (time_msc=? AND deal_ticket<?)) ORDER BY time_msc,deal_ticket",
            (self.account_key, row["position_id"], row["time_msc"], row["time_msc"], row["deal_ticket"]),
        ).fetchall()
        open_volume = Decimal(0)
        episode = 1
        for item in prior:
            amount = Decimal(str(item[1]))
            if str(item[0]) in entry_in:
                open_volume += amount
            elif str(item[0]) in {str(value) for value in ENTRY_OUT | ENTRY_OUT_BY}:
                open_volume -= amount
                if open_volume == 0: episode += 1
            elif str(item[0]) in entry_inout:
                if amount < open_volume:
                    raise TerminalDealIngestionError("INOUT volume does not close the prior exposure")
                open_volume = amount - open_volume
                episode += 1
        filled = Decimal(str(row["volume"]))
        if row["entry_type"] in entry_inout:
            if open_volume <= 0 or filled <= open_volume:
                raise TerminalDealIngestionError("INOUT has no deterministically bound new exposure")
            filled -= open_volume
            episode += 1
        binding = connection.execute("SELECT proposal_id FROM broker_order_bindings WHERE account_key=? AND order_ticket=?", (self.account_key, row["order_ticket"])).fetchone()
        intent = connection.execute("SELECT approved_volume,approved_risk_cash FROM entry_risk_intents WHERE account_key=? AND proposal_id=?", (self.account_key, binding[0])).fetchone()
        if intent is None: raise TerminalDealIngestionError("entry fill has no immutable risk intent")
        risk = Decimal(str(intent[1])) * filled / Decimal(str(intent[0]))
        values = (self.account_key, row["position_id"], row["deal_ticket"], binding[0], episode, format(filled.normalize(), "f"), format(risk.normalize(), "f"))
        existing = connection.execute("SELECT * FROM position_entry_allocations WHERE account_key=? AND deal_ticket=?", (self.account_key, row["deal_ticket"])).fetchone()
        if existing is not None and tuple(existing) != values: raise TerminalDealIngestionError("position risk allocation immutable conflict")
        if existing is None: connection.execute("INSERT INTO position_entry_allocations VALUES(?,?,?,?,?,?,?)", values)

    def _rebuild_episodes(self, connection: sqlite3.Connection) -> None:
        positions = [str(row[0]) for row in connection.execute("SELECT DISTINCT position_id FROM position_entry_allocations WHERE account_key=?", (self.account_key,))]
        for position_id in positions:
            deals = connection.execute("SELECT deal_ticket,entry_type,time_msc,volume,profit,commission,swap,fee FROM terminal_deals WHERE account_key=? AND position_id=? ORDER BY time_msc,deal_ticket", (self.account_key, position_id)).fetchall()
            allocations = {int(row[0]): (int(row[1]), Decimal(str(row[2]))) for row in connection.execute("SELECT deal_ticket,episode,allocated_risk_cash FROM position_entry_allocations WHERE account_key=? AND position_id=?", (self.account_key, position_id))}
            open_volume = Decimal(0); net = Decimal(0); tickets = []; opened = 0; episode = 1; risk = Decimal(0)
            for deal in deals:
                ticket, entry, when, volume = int(deal[0]), str(deal[1]), int(deal[2]), Decimal(str(deal[3]))
                components = [Decimal(str(deal[index])) for index in range(4, 8)]
                tickets.append(ticket)
                if entry in {str(value) for value in ENTRY_IN}:
                    if open_volume == 0: opened = when
                    open_volume += volume; net += sum(components); risk += allocations[ticket][1]
                elif entry in {str(value) for value in ENTRY_OUT | ENTRY_OUT_BY}:
                    open_volume -= volume; net += sum(components)
                else:
                    if open_volume <= 0 or volume <= open_volume:
                        raise TerminalDealIngestionError("INOUT volume does not deterministically reverse exposure")
                    old_volume = open_volume
                    close_ratio = old_volume / volume
                    net += components[0] + components[2] + (components[1] + components[3]) * close_ratio
                    self._store_episode(connection, position_id, episode, opened, when, risk, net, tickets)
                    episode += 1
                    open_volume = volume - old_volume
                    opened = when
                    net = (components[1] + components[3]) * (Decimal(1) - close_ratio)
                    risk = allocations[ticket][1]
                    tickets = [ticket]
                if open_volume < 0: raise TerminalDealIngestionError("terminal position volume became negative")
                if open_volume == 0:
                    self._store_episode(connection, position_id, episode, opened, when, risk, net, tickets)
                    episode += 1; net = Decimal(0); risk = Decimal(0); tickets = []; opened = 0

    def _store_episode(self, connection: sqlite3.Connection, position_id: str, episode: int, opened: int, closed: int, risk: Decimal, net: Decimal, tickets: list[int]) -> None:
            if risk <= 0: raise TerminalDealIngestionError("closed position has zero starting risk")
            payload = {"account_key": self.account_key, "position_id": position_id, "episode": episode, "candidate_hash": self.candidate_hash, "opened_at_msc": opened, "closed_at_msc": closed, "starting_risk_cash": format(risk.normalize(), "f"), "net_cash": format(net.normalize(), "f"), "net_r": format((net / risk).normalize(), "f"), "deal_tickets": tickets}
            episode_hash = _hash(payload)
            values = (self.account_key, position_id, episode, self.candidate_hash, opened, closed, payload["starting_risk_cash"], payload["net_cash"], payload["net_r"], _canonical_json(tickets), episode_hash)
            existing = connection.execute("SELECT * FROM closed_position_episodes WHERE account_key=? AND position_id=? AND episode=?", (self.account_key, position_id, episode)).fetchone()
            if existing is not None and tuple(existing) != values: raise TerminalDealIngestionError("closed position episode immutable conflict")
            if existing is None: connection.execute("INSERT INTO closed_position_episodes VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)

    def _facts_hash(self, connection: sqlite3.Connection) -> str:
        rows = [dict(row) for row in connection.execute("SELECT * FROM terminal_deals WHERE account_key=? ORDER BY time_msc,deal_ticket", (self.account_key,))]
        return _hash(rows)

    def snapshot(self, *, observed_at: datetime | None = None) -> TerminalFactSnapshot:
        observed = self._utc(observed_at or self.now())
        with self._connect() as connection:
            cursor = connection.execute("SELECT high_water_time_msc,high_water_ticket,last_full_reconciliation_utc,last_attestation_fingerprint FROM terminal_ingestion_cursor WHERE account_key=?", (self.account_key,)).fetchone()
            if cursor is None: raise TerminalDealIngestionError("terminal facts have not been reconciled")
            if not re.fullmatch(r"[0-9a-f]{64}", str(cursor[3] or "")):
                raise TerminalDealIngestionError("terminal facts lack a complete query attestation")
            reconciliation = datetime.fromisoformat(str(cursor[2]))
            if observed - reconciliation > timedelta(hours=24): raise TerminalDealIngestionError("terminal fact reconciliation is stale")
            start_msc = int(observed.astimezone(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).timestamp() * 1000)
            episodes = [dict(row) for row in connection.execute("SELECT * FROM closed_position_episodes WHERE account_key=? ORDER BY closed_at_msc,position_id,episode", (self.account_key,))]
            daily = sum((Decimal(row["net_cash"]) / Decimal(row["starting_risk_cash"]) for row in episodes if int(row["closed_at_msc"]) >= start_msc), Decimal(0))
            entries = connection.execute("SELECT i.instrument_id,COUNT(*) FROM position_entry_allocations a JOIN entry_risk_intents i ON i.account_key=a.account_key AND i.proposal_id=a.proposal_id JOIN terminal_deals d ON d.account_key=a.account_key AND d.deal_ticket=a.deal_ticket WHERE a.account_key=? AND d.time_msc>=? GROUP BY i.instrument_id", (self.account_key, start_msc)).fetchall()
            counts = {str(row[0]): int(row[1]) for row in entries}
            reservations = connection.execute("SELECT instrument_id,COUNT(*) FROM daily_risk_slots s WHERE account_key=? AND session_date=? AND state IN ('SEND_ARMED','WRITE_CLAIMED','SUBMITTED','UNKNOWN') AND NOT EXISTS(SELECT 1 FROM position_entry_allocations a WHERE a.account_key=s.account_key AND a.proposal_id=s.proposal_id) GROUP BY instrument_id", (self.account_key, observed.astimezone(ZoneInfo("America/New_York")).date().isoformat())).fetchall()
            for row in reservations: counts[str(row[0])] = counts.get(str(row[0]), 0) + int(row[1])
            ids = tuple(str(row[0]) for row in connection.execute("SELECT deal_ticket FROM terminal_deals WHERE account_key=? ORDER BY time_msc,deal_ticket", (self.account_key,)))
            return TerminalFactSnapshot(observed, self._facts_hash(connection), reconciliation, int(cursor[0]), int(cursor[1]), daily, sum(counts.values()), counts, ids, tuple(episodes))
