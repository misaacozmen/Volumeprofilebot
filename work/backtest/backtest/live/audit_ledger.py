"""Append-only tamper-evident audit ledger backed by SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping
from uuid import uuid4


PREFIX = b"VPB-AUDIT-V2\0"
AUDIT_SCHEMA_VERSION = 2
GENESIS_HASH = "0" * 64


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _atomic_anchor(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        with temporary.open("r+b") as stream:
            import os
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def event_hash(previous_hash: str, event: Mapping[str, Any]) -> str:
    required = {"schema_version", "sequence", "event_id", "occurred_at_utc", "campaign_id", "account_key", "entity_type", "entity_id", "event_type", "payload", "previous_hash"}
    if not required.issubset(event) or str(event.get("previous_hash")) != previous_hash:
        raise ValueError("audit event hash envelope is incomplete")
    try:
        previous = bytes.fromhex(previous_hash)
    except ValueError as exc:
        raise ValueError("previous event hash must be 64 hexadecimal characters") from exc
    if len(previous) != 32:
        raise ValueError("previous event hash must be 32 bytes")
    return hashlib.sha256(PREFIX + previous + canonical_json(event).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEvent:
    sequence: int
    event_id: str
    campaign_id: str
    account_key: str
    occurred_at_utc: str
    event_type: str
    entity_type: str
    entity_id: str
    payload: Mapping[str, Any]
    prev_event_hash: str
    event_hash: str


class AuditLedgerError(RuntimeError):
    pass


class AuditLedger:
    """SQLite ledger whose chain and row insertion share one transaction."""

    def __init__(
        self,
        path: str | Path,
        *,
        campaign_id: str = "",
        account_key: str = "",
        legacy_snapshot_hash: str | None = None,
        anchor: Callable[[str, str], Any] | None = None,
        anchor_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.campaign_id = campaign_id
        self.account_key = account_key
        self.legacy_snapshot_hash = legacy_snapshot_hash
        self.anchor = anchor
        self.anchor_lookup = anchor_lookup
        self.anchor_path = self.path.with_suffix(self.path.suffix + ".anchor")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self._configure()
        self._create_schema()

    def _configure(self) -> None:
        self.connection.execute("PRAGMA busy_timeout=30000")
        journal = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal is None or str(journal[0]).lower() != "wal":
            raise AuditLedgerError("audit ledger requires SQLite WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        sync = self.connection.execute("PRAGMA synchronous").fetchone()
        if sync is None or int(sync[0]) != 2:
            raise AuditLedgerError("audit ledger requires synchronous=FULL")

    def _create_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_chain_events (
                schema_version INTEGER NOT NULL DEFAULT 2,
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                campaign_id TEXT NOT NULL,
                account_key TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload_canonical_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_anchor_outbox (
                event_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL UNIQUE,
                event_hash TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                queued_at_utc TEXT NOT NULL,
                attempted_at_utc TEXT,
                acked_at_utc TEXT,
                last_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_state_records (
                order_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                request_hash TEXT NOT NULL DEFAULT '',
                updated_at_utc TEXT NOT NULL
            )
            """
        )
        columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(audit_chain_events)")}
        if "schema_version" not in columns:
            self.connection.execute("ALTER TABLE audit_chain_events ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
        if "previous_hash" not in columns and "prev_event_hash" in columns:
            self.connection.execute(
                "ALTER TABLE audit_chain_events RENAME COLUMN prev_event_hash TO previous_hash"
            )
        if "previous_hash" not in columns and "prev_event_hash" not in columns:
            raise AuditLedgerError("audit ledger has no canonical previous-hash column")
        self.verify()
        self._verify_anchor_outbox()
        self._recover_anchor_suffix()

    def _read_startup_anchor(self) -> tuple[int, str] | None:
        if not self.anchor_path.exists():
            return None
        try:
            value = json.loads(self.anchor_path.read_text(encoding="utf-8"))
            return (int(value["sequence"]), str(value["event_hash"]))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AuditLedgerError("audit startup anchor is corrupt") from exc

    def _last(self, connection: sqlite3.Connection | None = None) -> tuple[int, str] | None:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT sequence, event_hash FROM audit_chain_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return None if row is None else (int(row[0]), str(row[1]))

    def _verify_anchor_outbox(self) -> None:
        rows = self.connection.execute(
            "SELECT state,sequence,event_hash FROM audit_anchor_outbox ORDER BY sequence"
        ).fetchall()
        suffix_started = False
        for state, sequence, digest in rows:
            state = str(state)
            if state not in {"ACKED", "PENDING", "DELIVERY_ATTEMPTED", "FAILED"}:
                raise AuditLedgerError(f"audit anchor state is invalid at sequence {sequence}")
            if state == "ACKED" and suffix_started:
                raise AuditLedgerError("audit anchor ACK prefix is not contiguous")
            suffix_started = suffix_started or state != "ACKED"
            row = self.connection.execute(
                "SELECT event_hash FROM audit_chain_events WHERE sequence=?", (int(sequence),)
            ).fetchone()
            if row is None or str(row[0]) != str(digest):
                raise AuditLedgerError("audit anchor outbox is not bound to the ledger")

    def _recover_anchor_suffix(self) -> None:
        ack = self.connection.execute(
            "SELECT sequence,event_hash FROM audit_anchor_outbox WHERE state='ACKED' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        expected = (0, GENESIS_HASH) if ack is None else (int(ack[0]), str(ack[1]))
        observed = self._read_startup_anchor()
        if observed is None:
            if expected != (0, GENESIS_HASH):
                raise AuditLedgerError("audit startup anchor is missing")
        elif observed[0] > expected[0]:
            raise AuditLedgerError("audit startup anchor is ahead of the acknowledged prefix")
        elif observed != expected:
            raise AuditLedgerError("audit startup anchor does not match the acknowledged prefix")
        self.deliver_pending()

    def append(
        self,
        event_type: str,
        *,
        entity_type: str = "system",
        entity_id: str = "",
        payload: Mapping[str, Any] | None = None,
        campaign_id: str | None = None,
        account_key: str | None = None,
        occurred_at_utc: datetime | str | None = None,
        event_id: str | None = None,
        state_update: Callable[[sqlite3.Connection], Any] | None = None,
    ) -> AuditEvent:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            result = self.append_in_transaction(
                event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=payload,
                campaign_id=campaign_id,
                account_key=account_key,
                occurred_at_utc=occurred_at_utc,
                event_id=event_id,
                state_update=state_update,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        self._deliver_anchor(result.event_id, result.sequence, result.event_hash)
        return result

    def append_in_transaction(
        self,
        event_type: str,
        *,
        entity_type: str = "system",
        entity_id: str = "",
        payload: Mapping[str, Any] | None = None,
        campaign_id: str | None = None,
        account_key: str | None = None,
        occurred_at_utc: datetime | str | None = None,
        event_id: str | None = None,
        state_update: Callable[[sqlite3.Connection], Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> AuditEvent:
        """Append the ledger and outbox rows to an already-open DB transaction."""
        connection = connection or self.connection
        if not event_type or not entity_type:
            raise ValueError("event_type and entity_type are required")
        when = occurred_at_utc or datetime.now(timezone.utc)
        if isinstance(when, datetime):
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when_text = when.astimezone(timezone.utc).isoformat()
        else:
            when_text = str(when)
        event_id = event_id or str(uuid4())
        body: dict[str, Any] = dict(payload or {})
        last = self._last(connection=connection)
        previous = GENESIS_HASH if last is None else last[1]
        sequence = 1 if last is None else last[0] + 1
        if last is None:
            body.setdefault("legacy_snapshot_hash", self.legacy_snapshot_hash or self._legacy_snapshot_hash(connection))
            body.setdefault("genesis", True)
        event_json = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "sequence": sequence,
            "event_id": event_id,
            "campaign_id": self.campaign_id if campaign_id is None else campaign_id,
            "account_key": self.account_key if account_key is None else account_key,
            "occurred_at_utc": when_text,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": body,
            "previous_hash": previous,
        }
        digest = event_hash(previous, event_json)
        # Re-read sequence/hash in the caller's write transaction to prevent
        # two writers from deriving the same next link.
        last_in_tx = self._last(connection=connection)
        previous_in_tx = GENESIS_HASH if last_in_tx is None else last_in_tx[1]
        sequence_in_tx = 1 if last_in_tx is None else last_in_tx[0] + 1
        if previous_in_tx != previous or sequence_in_tx != sequence:
            previous, sequence = previous_in_tx, sequence_in_tx
        event_json["sequence"] = sequence
        event_json["previous_hash"] = previous
        digest = event_hash(previous, event_json)
        connection.execute(
            """INSERT INTO audit_chain_events
            (schema_version,sequence,event_id,campaign_id,account_key,occurred_at_utc,event_type,
             entity_type,entity_id,payload_canonical_json,previous_hash,event_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                AUDIT_SCHEMA_VERSION, sequence, event_id, event_json["campaign_id"], event_json["account_key"],
                when_text, event_type, entity_type, entity_id,
                canonical_json(body), previous, digest,
            ),
        )
        if state_update is not None:
            state_update(connection)
        connection.execute(
            "INSERT INTO audit_anchor_outbox(event_id,sequence,event_hash,state,queued_at_utc) VALUES(?,?,?,?,?)",
            (event_id, sequence, digest, "PENDING", when_text),
        )
        return AuditEvent(
            sequence, event_id, str(event_json["campaign_id"]), str(event_json["account_key"]),
            when_text, event_type, entity_type, entity_id, body, previous, digest,
        )

    def deliver_pending(self) -> None:
        """Deliver committed anchor outbox rows after their DB transaction."""
        rows = self.connection.execute(
            "SELECT event_id,sequence,event_hash FROM audit_anchor_outbox "
            "WHERE state IN ('PENDING','DELIVERY_ATTEMPTED','FAILED') ORDER BY sequence"
        ).fetchall()
        for event_id, sequence, digest in rows:
            self._deliver_anchor(str(event_id), int(sequence), str(digest))

    def _deliver_anchor(self, event_id: str, sequence: int, digest: str) -> None:
        attempted = datetime.now(timezone.utc).isoformat()
        # Anchor delivery is a cross-process ordered side effect.  The DB
        # write lock must cover claim, anchor replacement, and ACK; otherwise
        # two processes can publish sequence N+1 before N and leave the
        # startup anchor inconsistent with the acknowledged prefix.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT state,event_hash FROM audit_anchor_outbox WHERE event_id=? AND sequence=?",
                (event_id, sequence),
            ).fetchone()
            if row is None or str(row[1]) != digest:
                raise AuditLedgerError("audit anchor outbox row is missing or not bound")
            if str(row[0]) == "ACKED":
                self.connection.commit()
                return
            next_row = self.connection.execute(
                "SELECT sequence FROM audit_anchor_outbox WHERE state <> 'ACKED' ORDER BY sequence LIMIT 1"
            ).fetchone()
            if next_row is None or int(next_row[0]) != sequence:
                self.connection.commit()
                return
            self.connection.execute(
                "UPDATE audit_anchor_outbox SET state='DELIVERY_ATTEMPTED',attempted_at_utc=?,last_error='' WHERE event_id=? AND state IN ('PENDING','DELIVERY_ATTEMPTED','FAILED')",
                (attempted, event_id),
            )
            anchored_hash = self.anchor_lookup(event_id) if self.anchor_lookup is not None else None
            if anchored_hash is not None and str(anchored_hash).lower() != digest:
                raise AuditLedgerError("external audit event ID is bound to a different hash")
            if anchored_hash is None and self.anchor is not None:
                self.anchor(digest, event_id)
            _atomic_anchor(
                self.anchor_path,
                {"schema_version": AUDIT_SCHEMA_VERSION, "sequence": sequence, "event_hash": digest, "event_id": event_id},
            )
            if self.connection.execute(
                "UPDATE audit_anchor_outbox SET state='ACKED',acked_at_utc=? WHERE event_id=? AND event_hash=? AND state='DELIVERY_ATTEMPTED'",
                (datetime.now(timezone.utc).isoformat(), event_id, digest),
            ).rowcount != 1:
                raise AuditLedgerError("audit anchor ACK could not be persisted")
            self.connection.commit()
        except Exception as exc:
            if self.connection.in_transaction:
                try:
                    self.connection.execute(
                        "UPDATE audit_anchor_outbox SET state='FAILED',last_error=? WHERE event_id=?",
                        (f"{type(exc).__name__}: {exc}", event_id),
                    )
                    self.connection.commit()
                except Exception:
                    self.connection.rollback()
            if isinstance(exc, AuditLedgerError):
                raise
            raise AuditLedgerError("audit anchor delivery failed") from exc

    def _legacy_snapshot_hash(self, connection: sqlite3.Connection | None = None) -> str:
        connection = connection or self.connection
        rows: list[tuple[str, list[tuple[Any, ...]]]] = []
        for table in ("order_intents", "order_event_outbox", "broker_execution_states"):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                values = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                rows.append((table, values))
        return hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()

    def verify(self) -> bool:
        rows = self.connection.execute(
            """SELECT schema_version,sequence,event_id,campaign_id,account_key,occurred_at_utc,event_type,
               entity_type,entity_id,payload_canonical_json,previous_hash,event_hash
               FROM audit_chain_events ORDER BY sequence"""
        ).fetchall()
        previous = GENESIS_HASH
        expected_sequence = 1
        for row in rows:
            schema_version, sequence, event_id, campaign_id, account_key, occurred, event_type, entity_type, entity_id, payload_json, prev, digest = row
            if int(sequence) != expected_sequence or str(prev) != previous:
                raise AuditLedgerError("audit chain is deleted, reordered, or has a sequence gap")
            if int(schema_version) != AUDIT_SCHEMA_VERSION:
                raise AuditLedgerError("audit schema version is not current")
            try:
                payload = json.loads(str(payload_json))
            except json.JSONDecodeError as exc:
                raise AuditLedgerError("audit payload is not valid JSON") from exc
            if not isinstance(payload, dict) or canonical_json(payload) != str(payload_json):
                raise AuditLedgerError("audit payload canonical form was mutated")
            event_json = {
                "schema_version": int(schema_version), "sequence": int(sequence), "event_id": event_id,
                "campaign_id": campaign_id, "account_key": account_key,
                "occurred_at_utc": occurred, "event_type": event_type,
                "entity_type": entity_type, "entity_id": entity_id, "payload": payload,
                "previous_hash": previous,
            }
            calculated = event_hash(previous, event_json)
            if str(digest) != calculated:
                raise AuditLedgerError("audit chain hash mismatch")
            previous = calculated
            expected_sequence += 1
        return True

    def latest_hash(self) -> str:
        last = self._last()
        return GENESIS_HASH if last is None else last[1]

    def anchor_latest(self, reason: str = "") -> str:
        digest = self.latest_hash()
        last = self._last()
        payload = {"schema_version": AUDIT_SCHEMA_VERSION, "sequence": 0 if last is None else last[0], "event_hash": digest, "reason": reason}
        _atomic_anchor(self.anchor_path, payload)
        if self.anchor is not None:
            self.anchor(digest, reason)
        return digest

    def close(self, *, clean_shutdown: bool = False) -> None:
        if clean_shutdown:
            self.anchor_latest("clean_shutdown")
        self.connection.close()
