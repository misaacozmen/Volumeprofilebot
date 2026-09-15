"""Owner-only SQLite replay ledger primitives; JSON replay evidence is invalid."""

from __future__ import annotations

from datetime import datetime, timezone
import base64
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3


HEX64 = re.compile(r"^[0-9a-f]{64}$")
STATES = frozenset({"RESERVED", "CONSUMED"})
REPLAY_PURPOSE = "SUPER1_FINAL_MANIFEST"
RECEIPT_KEYS = frozenset({
    "schema_version", "receipt_type", "run_id", "nonces", "purpose", "state",
    "source_commit", "source_tree_oid", "inventory_sha256", "issued_at_utc",
    "signer_public_key_sha256", "signature_algorithm", "signature_b64",
})


def _hex(value: object, pattern: re.Pattern[str], label: str) -> str:
    text = str(value or "")
    if not pattern.fullmatch(text):
        raise ValueError(f"owner replay receipt {label} is invalid")
    return text


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


def validate(path: str | Path, *, run_id: str, nonces: tuple[str, str], purpose: str = "audit", require_consumed: bool = False) -> dict[str, object]:
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
        if any(str(row[3]) not in STATES or str(row[2]) != str(purpose) or not str(row[4]).strip() for row in rows):
            raise ValueError("owner replay ledger row is invalid")
        if require_consumed and any(str(row[3]) != "CONSUMED" or not str(row[5]).strip() for row in rows):
            raise ValueError("owner replay ledger has not consumed both nonces")
        return {"schema_version": 1, "run_id": str(run_id), "nonces": sorted(str(row[1]) for row in rows), "states": sorted(str(row[3]) for row in rows)}
    finally:
        connection.close()


def canonical_receipt_bytes(receipt: dict[str, object]) -> bytes:
    return json.dumps({key: value for key, value in receipt.items() if key != "signature_b64"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def validate_receipt(
    path: str | Path,
    *,
    signer_public_key_path: str | Path,
    signer_public_key_sha256: str,
    run_id: str,
    nonces: tuple[str, str],
    source_commit: str,
    source_tree_oid: str,
    inventory_sha256: str,
    purpose: str = REPLAY_PURPOSE,
) -> dict[str, object]:
    receipt_path = Path(path).resolve()
    key_path = Path(signer_public_key_path).resolve()
    if not receipt_path.is_file() or not key_path.is_file():
        raise ValueError("owner replay receipt or signer key is missing")
    if not HEX64.fullmatch(str(signer_public_key_sha256)) or sha256(key_path.read_bytes()).hexdigest() != str(signer_public_key_sha256):
        raise ValueError("owner replay receipt signer key fingerprint differs")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("owner replay receipt is unreadable") from exc
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS or receipt.get("schema_version") != 1 or receipt.get("receipt_type") != "OWNER_REPLAY_RECEIPT_V1":
        raise ValueError("owner replay receipt schema is not closed")
    if not isinstance(receipt.get("nonces"), list) or len(receipt["nonces"]) != 2 or len(set(str(value) for value in receipt["nonces"])) != 2 or str(receipt.get("run_id")) != str(run_id) or receipt.get("nonces") != sorted(str(value) for value in nonces) or receipt.get("purpose") != str(purpose) or receipt.get("state") != "CONSUMED":
        raise ValueError("owner replay receipt run binding differs")
    _hex(receipt.get("run_id"), HEX64, "run_id")
    if any(not HEX64.fullmatch(str(value)) for value in receipt["nonces"]):
        raise ValueError("owner replay receipt nonce is invalid")
    _hex(receipt.get("source_commit"), re.compile(r"^[0-9a-f]{40}$"), "source_commit")
    _hex(receipt.get("source_tree_oid"), re.compile(r"^[0-9a-f]{40}$"), "source_tree_oid")
    _hex(receipt.get("inventory_sha256"), HEX64, "inventory_sha256")
    if receipt.get("source_commit") != source_commit or receipt.get("source_tree_oid") != source_tree_oid or receipt.get("inventory_sha256") != inventory_sha256 or receipt.get("signature_algorithm") != "RSA-PSS-SHA256":
        raise ValueError("owner replay receipt source binding differs")
    if not isinstance(receipt.get("issued_at_utc"), str) or not receipt["issued_at_utc"].strip() or not HEX64.fullmatch(str(receipt.get("signer_public_key_sha256"))):
        raise ValueError("owner replay receipt signature header is invalid")
    if receipt["signer_public_key_sha256"] != str(signer_public_key_sha256):
        raise ValueError("owner replay receipt signer binding differs")
    try:
        signature = base64.b64decode(str(receipt["signature_b64"]), validate=True)
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        serialization.load_pem_public_key(key_path.read_bytes()).verify(signature, canonical_receipt_bytes(receipt), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    except Exception as exc:
        raise ValueError("owner replay receipt signature verification failed") from exc
    return {"schema_version": 1, "run_id": str(receipt["run_id"]), "nonces": list(receipt["nonces"]), "purpose": str(receipt["purpose"]), "state": "CONSUMED"}
