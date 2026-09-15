"""Owner-only SQLite replay ledger primitives; JSON replay evidence is invalid."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3


HEX64 = re.compile(r"^[0-9a-f]{64}$")
STATES = frozenset({"RESERVED", "CONSUMED"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    if str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
        connection.close()
        raise ValueError("owner replay ledger requires SQLite WAL")
    return connection


def initialize(path: str | Path) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = _connection(target)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS owner_replay (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                nonce TEXT NOT NULL UNIQUE,
                purpose TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('RESERVED','CONSUMED')),
                reserved_at_utc TEXT NOT NULL,
                consumed_at_utc TEXT,
                UNIQUE(run_id, nonce)
            );
            INSERT OR IGNORE INTO ledger_meta(key,value) VALUES('schema_version','1');
            """
        )
    finally:
        connection.close()


def reserve(path: str | Path, *, run_id: str, nonce: str, purpose: str) -> None:
    if not str(run_id).strip() or not HEX64.fullmatch(str(nonce)) or not str(purpose).strip():
        raise ValueError("owner replay identity is incomplete")
    initialize(path)
    connection = _connection(Path(path).resolve())
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO owner_replay(run_id,nonce,purpose,state,reserved_at_utc,consumed_at_utc) VALUES(?,?,?,?,?,NULL)",
            (str(run_id), str(nonce), str(purpose), "RESERVED", _now()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def consume(path: str | Path, *, run_id: str, nonce: str) -> None:
    if not str(run_id).strip() or not HEX64.fullmatch(str(nonce)):
        raise ValueError("owner replay identity is incomplete")
    connection = _connection(Path(path).resolve())
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE owner_replay SET state='CONSUMED',consumed_at_utc=? WHERE run_id=? AND nonce=? AND state='RESERVED'",
            (_now(), str(run_id), str(nonce)),
        ).rowcount
        if changed != 1:
            connection.rollback()
            raise ValueError("owner replay row is missing or already consumed")
        connection.commit()
    finally:
        connection.close()


def validate(path: str | Path, *, run_id: str, nonces: tuple[str, str]) -> dict[str, object]:
    target = Path(path).resolve()
    if not target.is_file():
        raise ValueError("owner replay ledger must be an external SQLite database")
    if len(set(nonces)) != 2 or any(not HEX64.fullmatch(str(value)) for value in nonces):
        raise ValueError("owner replay nonces are invalid")
    connection = _connection(target)
    try:
        meta_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ledger_meta)")}
        replay_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(owner_replay)")}
        if meta_columns != {"key", "value"} or replay_columns != {"entry_id", "run_id", "nonce", "purpose", "state", "reserved_at_utc", "consumed_at_utc"}:
            raise ValueError("owner replay ledger schema is not closed")
        if connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone() != ("1",):
            raise ValueError("owner replay ledger schema version is invalid")
        rows = connection.execute(
            "SELECT run_id,nonce,purpose,state,reserved_at_utc,consumed_at_utc FROM owner_replay WHERE run_id=? ORDER BY nonce",
            (str(run_id),),
        ).fetchall()
        if len(rows) != 2 or {str(row[1]) for row in rows} != set(nonces):
            raise ValueError("owner replay ledger does not reserve both audit nonces")
        if any(str(row[3]) not in STATES or not str(row[2]).strip() or not str(row[4]).strip() for row in rows):
            raise ValueError("owner replay ledger row is invalid")
        return {"schema_version": 1, "run_id": str(run_id), "nonces": sorted(str(row[1]) for row in rows), "states": sorted(str(row[3]) for row in rows)}
    finally:
        connection.close()
