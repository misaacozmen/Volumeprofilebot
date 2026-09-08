"""Persistent fail-closed HALT and restart-safe emergency flatten controls."""

from __future__ import annotations

import json
import base64
import os
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import sqlite3
from typing import Any, Callable, Mapping
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .audit_ledger import AuditLedger, canonical_json


class HaltError(RuntimeError):
    pass


HALT_SCHEMA_VERSION = 2


def datetime_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: Mapping[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        handle = os.open(path, flags)
        try:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            os.write(handle, data)
            os.fsync(handle)
        finally:
            os.close(handle)
        return
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class HaltController:
    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)
        self.sentinel = self.output_root / "fatal_latch.json"
        self.flatten_attempt_root = self.output_root / "flatten_attempts"
        self.flatten_latch = self.sentinel
        # HALT owns a separate canonical SQLite ledger so it can be latched
        # while the order ledger is still inside a failed transaction.
        self.db_path = self.output_root / "orders" / "halt.sqlite3"

    def _db(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS halt_episodes (
                halt_episode_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                reason TEXT NOT NULL,
                first_triggered_at TEXT NOT NULL,
                latched_at TEXT NOT NULL,
                last_broker_evidence_json TEXT,
                unresolved_tickets_json TEXT NOT NULL,
                audit_chain_tail_json TEXT,
                details_json TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS halt_reasons (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                halt_episode_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                details_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(halt_episode_id, reason)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS halt_recovery_nonces (
                nonce TEXT PRIMARY KEY,
                halt_episode_id TEXT NOT NULL,
                consumed_at_utc TEXT NOT NULL
            )"""
        )
        return connection

    def _read_db(self, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        owned_connection = connection is None
        connection = connection or self._db()
        try:
            row = connection.execute(
                "SELECT halt_episode_id,state,reason,first_triggered_at,latched_at,last_broker_evidence_json,unresolved_tickets_json,audit_chain_tail_json,details_json FROM halt_episodes WHERE state <> 'CLEARED' ORDER BY latched_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            reasons = [str(item[0]) for item in connection.execute("SELECT reason FROM halt_reasons WHERE halt_episode_id=? ORDER BY sequence", (str(row[0]),))]
            def decode(value: Any, fallback: Any) -> Any:
                if value is None:
                    return fallback
                try:
                    return json.loads(str(value))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise HaltError("canonical HALT database is corrupt") from exc
            details = decode(row[8], {})
            if not isinstance(details, dict):
                raise HaltError("canonical HALT details are corrupt")
            result = {
                "schema_version": HALT_SCHEMA_VERSION,
                "halt_episode_id": str(row[0]), "first_triggered_at": str(row[3]), "latched_at": str(row[4]),
                "state": str(row[1]), "reason": str(row[2]), "reasons": reasons or [str(row[2])],
                "last_broker_evidence": decode(row[5], None),
                "unresolved_tickets": decode(row[6], []), "audit_chain_tail": decode(row[7], None),
                "details": details,
            }
            reserved = {"schema_version", "halt_episode_id", "first_triggered_at", "latched_at", "state", "reason", "reasons", "last_broker_evidence", "unresolved_tickets", "audit_chain_tail", "details"}
            result.update({key: value for key, value in details.items() if key not in reserved})
            return result
        finally:
            if owned_connection:
                connection.close()

    def _mirror(self, payload: Mapping[str, Any]) -> None:
        _atomic_json(self.sentinel, payload)

    def read(self) -> dict[str, Any] | None:
        canonical = self._read_db()
        if canonical is not None:
            return canonical
        connection = self._db()
        try:
            has_history = connection.execute("SELECT 1 FROM halt_episodes LIMIT 1").fetchone() is not None
        finally:
            connection.close()
        if has_history:
            return None
        if not self.sentinel.exists():
            return None
        try:
            value = json.loads(self.sentinel.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HaltError("HALT sentinel cannot be read; startup is blocked") from exc
        if not isinstance(value, dict) or not value.get("halt_episode_id"):
            raise HaltError("HALT sentinel is malformed; startup is blocked")
        if int(value.get("schema_version", 1)) not in {1, HALT_SCHEMA_VERSION}:
            raise HaltError("HALT sentinel schema is unsupported; startup is blocked")
        return value

    def assert_clear(self) -> None:
        if self.read() is not None:
            raise HaltError("persistent HALT is active")

    def trigger(self, reason: str, **details: Any) -> dict[str, Any]:
        connection = self._db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._read_db(connection)
            if existing is not None:
                episode = str(existing["halt_episode_id"])
                merged = {**(existing.get("details") if isinstance(existing.get("details"), dict) else {}), **details}
                connection.execute(
                    "INSERT OR IGNORE INTO halt_reasons(halt_episode_id,reason,details_json,recorded_at) VALUES(?,?,?,?)",
                    (episode, reason, json.dumps(details, sort_keys=True, separators=(",", ":"), allow_nan=False), datetime_now_iso()),
                )
                connection.execute(
                    "UPDATE halt_episodes SET details_json=?,last_broker_evidence_json=?,unresolved_tickets_json=?,audit_chain_tail_json=? WHERE halt_episode_id=?",
                    (json.dumps(merged, sort_keys=True, separators=(",", ":"), allow_nan=False), json.dumps(details.get("broker_evidence", existing.get("last_broker_evidence")), sort_keys=True, separators=(",", ":"), allow_nan=False), json.dumps(list(details.get("unresolved_tickets", existing.get("unresolved_tickets", []))), sort_keys=True, separators=(",", ":"), allow_nan=False), json.dumps(details.get("audit_chain_tail", existing.get("audit_chain_tail")), sort_keys=True, separators=(",", ":"), allow_nan=False), episode),
                )
                connection.commit()
                result = self._read_db()
                if result is None:
                    raise HaltError("canonical HALT episode disappeared")
                self._mirror(result)
                connection.close()
                return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
            raise
        now = datetime_now_iso()
        payload = {
            "schema_version": HALT_SCHEMA_VERSION,
            "halt_episode_id": uuid4().hex,
            "first_triggered_at": now,
            "latched_at": now,
            "state": reason,
            "reason": reason,
            "reasons": [reason],
            "last_broker_evidence": details.get("broker_evidence"),
            "unresolved_tickets": list(details.get("unresolved_tickets", [])),
            "audit_chain_tail": details.get("audit_chain_tail"),
            "details": details,
        }
        episode = str(payload["halt_episode_id"])
        connection.execute(
            "INSERT INTO halt_episodes(halt_episode_id,state,reason,first_triggered_at,latched_at,last_broker_evidence_json,unresolved_tickets_json,audit_chain_tail_json,details_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (episode, reason, reason, now, now, json.dumps(payload["last_broker_evidence"], sort_keys=True, separators=(",", ":"), allow_nan=False), json.dumps(payload["unresolved_tickets"], sort_keys=True, separators=(",", ":"), allow_nan=False), json.dumps(payload["audit_chain_tail"], sort_keys=True, separators=(",", ":"), allow_nan=False), json.dumps(details, sort_keys=True, separators=(",", ":"), allow_nan=False)),
        )
        connection.execute(
            "INSERT INTO halt_reasons(halt_episode_id,reason,details_json,recorded_at) VALUES(?,?,?,?)",
            (episode, reason, json.dumps(details, sort_keys=True, separators=(",", ":"), allow_nan=False), now),
        )
        connection.commit()
        result = self._read_db()
        if result is None:
            raise HaltError("canonical HALT episode was not persisted")
        self._mirror(result)
        connection.close()
        return result

    def add_evidence(self, *, reason: str, broker_evidence: Any = None, unresolved_tickets: Any = None, audit_chain_tail: Any = None, **details: Any) -> dict[str, Any]:
        """Extend the active episode without ever replacing its identity."""
        return self.trigger(
            reason,
            broker_evidence=broker_evidence,
            unresolved_tickets=[] if unresolved_tickets is None else unresolved_tickets,
            audit_chain_tail=audit_chain_tail,
            **details,
        )

    def clear_episode_cas(self, episode_id: str, *, nonce: str, evidence: Mapping[str, Any]) -> None:
        connection = self._db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "INSERT OR IGNORE INTO halt_recovery_nonces(nonce,halt_episode_id,consumed_at_utc) VALUES(?,?,?)",
                (nonce, episode_id, datetime_now_iso()),
            ).rowcount != 1:
                raise HaltError("recovery nonce was already consumed")
            if connection.execute(
                "UPDATE halt_episodes SET state='CLEARED',details_json=? WHERE halt_episode_id=? AND state <> 'CLEARED'",
                (canonical_json(dict(evidence)), episode_id),
            ).rowcount != 1:
                raise HaltError("exact active HALT episode CAS failed")
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        if self.sentinel.exists():
            archived = self.sentinel.with_name(
                f"fatal_latch.cleared.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.{episode_id}.json"
            )
            os.replace(self.sentinel, archived)

    def episode_id(self) -> str:
        value = self.read()
        if value is None:
            raise HaltError("no HALT episode is active")
        return str(value["halt_episode_id"])

    def claim_flatten(self, episode_id: str | None = None, *, ticket: int | str | None = None) -> bool:
        episode = episode_id or self.episode_id()
        self.flatten_attempt_root.mkdir(parents=True, exist_ok=True)
        key = str(ticket or "episode")
        if not key.isalnum() and key != "episode":
            raise HaltError("flatten ticket key is invalid")
        claim = self.flatten_attempt_root / f"episode.{episode}.{key}.claim.json"
        owner = f"{os.getpid()}:{threading.get_ident()}"
        if claim.exists():
            try:
                existing = json.loads(claim.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HaltError("flatten claim cannot be read") from exc
            existing_owner = str(existing.get("owner") or "") if isinstance(existing, Mapping) else ""
            existing_state = str(existing.get("state") or "") if isinstance(existing, Mapping) else ""
            if existing_state == "CLAIMED" and not self._claim_owner_alive(existing_owner):
                try:
                    claim.unlink()
                except FileNotFoundError:
                    pass
        try:
            _atomic_json(
                claim,
                {
                    "schema_version": HALT_SCHEMA_VERSION,
                    "halt_episode_id": episode,
                    "ticket": key,
                    "state": "CLAIMED",
                    "state_history": ["DISCOVERED", "CLAIMED"],
                    "side_effect_attempted": False,
                    "owner": owner,
                },
                exclusive=True,
            )
        except FileExistsError:
            return False
        return True

    @staticmethod
    def _claim_owner_alive(owner: str) -> bool:
        try:
            pid_text, thread_text = owner.split(":", 1)
            pid = int(pid_text)
            thread_id = int(thread_text)
        except (TypeError, ValueError):
            return False
        if pid == os.getpid():
            return any(thread.ident == thread_id and thread.is_alive() for thread in threading.enumerate())
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    def release_claim(self, episode_id: str | None = None, *, ticket: int | str | None = None) -> None:
        episode = episode_id or self.episode_id()
        key = str(ticket or "episode")
        claim = self.flatten_attempt_root / f"episode.{episode}.{key}.claim.json"
        try:
            claim.unlink()
        except FileNotFoundError:
            pass

    def mark_side_effect_attempted(self, episode_id: str | None = None, *, ticket: int | str | None = None) -> None:
        episode = episode_id or self.episode_id()
        key = str(ticket or "episode")
        if not key.isalnum() and key != "episode":
            raise HaltError("flatten ticket key is invalid")
        claim = self.flatten_attempt_root / f"episode.{episode}.{key}.claim.json"
        if not claim.exists():
            raise HaltError("flatten claim is missing")
        try:
            claim_payload = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HaltError("flatten claim cannot be read") from exc
        history = list(claim_payload.get("state_history", ["DISCOVERED", "CLAIMED"])) if isinstance(claim_payload, Mapping) else ["DISCOVERED", "CLAIMED"]
        if not history or history[-1] != "CLAIMED":
            raise HaltError("flatten claim is not in CLAIMED state")
        history.append("WRITE_ATTEMPTED")
        key = str(ticket or "episode")
        if not key.isalnum() and key != "episode":
            raise HaltError("flatten ticket key is invalid")
        attempt = self.flatten_attempt_root / f"{episode}.{key}.attempt.json"
        _atomic_json(attempt, {"schema_version": HALT_SCHEMA_VERSION, "halt_episode_id": episode, "ticket": key, "state": "WRITE_ATTEMPTED", "state_history": history, "side_effect_attempted": True})
        _atomic_json(claim, {"schema_version": HALT_SCHEMA_VERSION, "halt_episode_id": episode, "ticket": key, "state": "WRITE_ATTEMPTED", "state_history": history, "side_effect_attempted": True})

    def mark_flatten_state(self, episode_id: str, *, ticket: int | str, state: str, evidence: Mapping[str, Any] | None = None) -> None:
        if state not in {"ACK", "UNKNOWN", "RECONCILED_ABSENT", "CLOSED"}:
            raise HaltError("invalid flatten terminal state")
        key = str(ticket)
        if not key.isalnum():
            raise HaltError("flatten ticket key is invalid")
        attempt = self.flatten_attempt_root / f"{episode_id}.{key}.attempt.json"
        history: list[str] = ["DISCOVERED", "CLAIMED", "WRITE_ATTEMPTED"]
        if attempt.exists():
            try:
                current = json.loads(attempt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HaltError("flatten attempt cannot be read") from exc
            if isinstance(current, Mapping):
                saved_history = current.get("state_history")
                if isinstance(saved_history, list) and saved_history:
                    history = [str(item) for item in saved_history]
        if not history or history[-1] not in {"WRITE_ATTEMPTED", "ACK", "UNKNOWN", "RECONCILED_ABSENT"}:
            raise HaltError("flatten ticket state transition is invalid")
        history.append(state)
        _atomic_json(
            attempt,
            {"schema_version": HALT_SCHEMA_VERSION, "halt_episode_id": episode_id, "ticket": key, "state": state, "state_history": history, "side_effect_attempted": True, "evidence": dict(evidence or {})},
        )

    def side_effect_attempted(self, episode_id: str | None = None, *, ticket: int | str | None = None) -> bool:
        episode = episode_id or self.episode_id()
        if not self.flatten_attempt_root.exists():
            return False
        key = None if ticket is None else str(ticket)
        attempts = self.flatten_attempt_root.glob(f"{episode}.*.attempt.json")
        for attempt in attempts:
            try:
                value = json.loads(attempt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HaltError("flatten attempt cannot be read") from exc
            if isinstance(value, Mapping) and str(value.get("halt_episode_id")) == episode and (key is None or str(value.get("ticket")) == key) and bool(value.get("side_effect_attempted")):
                return True
        return False


def clear_halt_episode(
    controller: HaltController,
    *,
    recovery_record: Mapping[str, Any],
    trusted_public_key: bytes,
    expected_campaign: str,
    expected_account: str,
    expected_policy_hash: str,
    read_orders: Callable[[], object],
    read_positions: Callable[[], object],
    audit_ledger: AuditLedger,
    mutex: Callable[[], Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cryptographically authorize and CAS-clear one exact canonical episode."""
    payload = recovery_record.get("payload")
    if not isinstance(payload, Mapping):
        raise HaltError("signed recovery payload/signature is missing")
    if recovery_record.get("algorithm") != "RSA-SHA256-PKCS1v15":
        raise HaltError("recovery signature algorithm is invalid")
    try:
        signature = base64.b64decode(str(recovery_record["signature_b64"]), validate=True)
        public_key = serialization.load_pem_public_key(trusted_public_key)
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise TypeError("trust root is not RSA")
        public_key.verify(
            signature,
            canonical_json(dict(payload)).encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (KeyError, ValueError, TypeError, InvalidSignature) as exc:
        raise HaltError("recovery signature verification failed") from exc
    episode = controller.read()
    if episode is None:
        raise HaltError("no active HALT episode exists")
    required = {
        "halt_episode_id": str(episode["halt_episode_id"]),
        "campaign_id": expected_campaign,
        "account_key": expected_account,
        "recovery_policy_hash": expected_policy_hash,
    }
    if any(str(payload.get(key) or "") != value for key, value in required.items()):
        raise HaltError("signed recovery binding does not match the active episode")
    nonce = str(payload.get("nonce") or "")
    if not nonce:
        raise HaltError("signed recovery nonce is missing")
    try:
        expiry = datetime.fromisoformat(str(payload["expires_at_utc"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HaltError("signed recovery expiry is invalid") from exc
    expiry = expiry if expiry.tzinfo is not None else expiry.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.astimezone(timezone.utc) >= expiry.astimezone(timezone.utc):
        raise HaltError("signed recovery record is expired")
    with (mutex or (lambda: nullcontext()))():
        orders = read_orders()
        positions = read_positions()
        if not isinstance(orders, (tuple, list)) or not isinstance(positions, (tuple, list)):
            raise HaltError("fresh broker exposure readback is unknown")
        if orders or positions:
            raise HaltError("fresh broker exposure readback is not flat")
        audit_ledger.verify()
        audit_event = audit_ledger.append(
            "HALT_CLEAR_AUTHORIZED",
            entity_type="halt_episode",
            entity_id=str(episode["halt_episode_id"]),
            payload={
                "campaign_id": expected_campaign,
                "account_key": expected_account,
                "nonce": nonce,
                "recovery_policy_hash": expected_policy_hash,
                "broker_orders": 0,
                "broker_positions": 0,
            },
        )
        controller.clear_episode_cas(
            str(episode["halt_episode_id"]),
            nonce=nonce,
            evidence={
                "cleared_at_utc": current.astimezone(timezone.utc).isoformat(),
                "audit_event_id": audit_event.event_id,
                "audit_event_hash": audit_event.event_hash,
                "recovery_policy_hash": expected_policy_hash,
            },
        )
    if controller.read() is not None:
        raise HaltError("canonical HALT episode remained active after clear")
    return {
        "state": "CLEARED",
        "halt_episode_id": str(episode["halt_episode_id"]),
        "audit_event_id": audit_event.event_id,
        "audit_event_hash": audit_event.event_hash,
    }


def broker_response_is_success(response: object, *, accepted_retcodes: set[int]) -> bool:
    """Error envelopes, nested errors, false ``ok``, and bad retcodes fail closed."""
    if response is None:
        return False
    if not isinstance(response, Mapping):
        response = {
            "retcode": getattr(response, "retcode", None),
            "ok": getattr(response, "ok", None),
            "status": getattr(response, "status", None),
        }
    if "error" in response or response.get("ok") is False or str(response.get("status", "")).lower() == "error":
        return False
    nested = response.get("result")
    if isinstance(nested, Mapping) and ("error" in nested or nested.get("ok") is False or str(nested.get("status", "")).lower() == "error"):
        return False
    if "retcode" in response:
        try:
            retcode = int(response["retcode"])
        except (TypeError, ValueError):
            return False
        if retcode not in accepted_retcodes:
            return False
    else:
        return False
    return True


def close_request(position_ticket: int, *, symbol: str | None = None) -> dict[str, Any]:
    """A close request must identify the exact position ticket."""
    if isinstance(position_ticket, bool) or int(position_ticket) <= 0:
        raise ValueError("position ticket is required for close")
    payload: dict[str, Any] = {"position": int(position_ticket)}
    if symbol is not None:
        payload["symbol"] = symbol
    return payload


def _validate_flatten_action(action: Mapping[str, Any]) -> None:
    kind = str(action.get("kind") or action.get("action") or "").upper()
    if "position" in action or "position_ticket" in action:
        ticket = action.get("position", action.get("position_ticket"))
        close_request(ticket, symbol=action.get("symbol"))
        return
    if kind in {"CANCEL", "CANCEL_ORDER"}:
        ticket = action.get("order", action.get("order_ticket", action.get("ticket")))
        if isinstance(ticket, bool) or int(ticket) <= 0:
            raise ValueError("pending order ticket is required for cancel")
        return
    raise ValueError("flatten action must identify a position or pending order ticket")


def emergency_flatten_cycle(
    controller: HaltController,
    read_exposure: Callable[[], Mapping[str, Any]],
    cancel_or_close: Callable[[Mapping[str, Any]], object],
) -> dict[str, Any]:
    """Read safely, claim once, mark before write, then perform one write.

    Read exceptions and malformed results release only this episode's orphan
    claim.  A write is never retried by this helper.
    """
    episode = controller.episode_id()
    try:
        exposure = read_exposure()
        if not isinstance(exposure, Mapping) or "error" in exposure or exposure.get("ok") is False:
            raise HaltError("broker read returned an error envelope")
        actions = exposure.get("actions", ())
        if not isinstance(actions, (list, tuple)):
            raise HaltError("broker read returned an invalid action list")
        for action in actions:
            if not isinstance(action, Mapping):
                raise HaltError("broker read returned an invalid flatten action")
            _validate_flatten_action(action)
    except Exception:
        controller.release_claim(episode)
        return {"state": "READ_UNKNOWN_NO_SEND", "halt_episode_id": episode}
    if not actions:
        reconciled: list[str] = []
        if controller.flatten_attempt_root.exists():
            for attempt in controller.flatten_attempt_root.glob(f"{episode}.*.attempt.json"):
                try:
                    value = json.loads(attempt.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    controller.add_evidence(reason="FLATTEN_STATE_UNREADABLE", unresolved_tickets=[attempt.name])
                    return {"state": "READ_UNKNOWN_NO_SEND", "halt_episode_id": episode}
                if not isinstance(value, Mapping) or str(value.get("halt_episode_id")) != episode:
                    continue
                ticket = str(value.get("ticket") or "")
                state = str(value.get("state") or "")
                if ticket and state in {"WRITE_ATTEMPTED", "ACK", "UNKNOWN", "RECONCILED_ABSENT"}:
                    controller.mark_flatten_state(
                        episode,
                        ticket=ticket,
                        state="RECONCILED_ABSENT",
                        evidence={"exposure": "ABSENT", "reconciled_at_utc": datetime_now_iso()},
                    )
                    controller.mark_flatten_state(
                        episode,
                        ticket=ticket,
                        state="CLOSED",
                        evidence={"exposure": "ABSENT", "reconciled_at_utc": datetime_now_iso()},
                    )
                    reconciled.append(ticket)
        controller.release_claim(episode)
        if reconciled:
            return {"state": "FLATTEN_CLOSED", "halt_episode_id": episode, "tickets": reconciled}
        return {"state": "NO_EXPOSURE", "halt_episode_id": episode}
    try:
        results = []
        attempted = 0
        for action in actions:
            ticket = action.get("position", action.get("position_ticket", action.get("order", action.get("order_ticket", action.get("ticket")))))
            if controller.side_effect_attempted(episode, ticket=ticket):
                # A fresh exact-ticket read still shows exposure.  A prior
                # transport ACK is not proof of closure, so permit one new
                # exclusive claim for this cycle.
                controller.release_claim(episode, ticket=ticket)
            if not controller.claim_flatten(episode, ticket=ticket):
                continue
            attempted += 1
            controller.mark_side_effect_attempted(episode, ticket=ticket)
            results.append(cancel_or_close(action))
            if not broker_response_is_success(results[-1], accepted_retcodes={10008, 10009, 10010}):
                controller.mark_flatten_state(episode, ticket=ticket, state="UNKNOWN", evidence={"response": str(results[-1])})
                return {"state": "WRITE_UNKNOWN", "halt_episode_id": episode, "ticket": str(ticket)}
            controller.mark_flatten_state(episode, ticket=ticket, state="ACK", evidence={"response": str(results[-1])})
        if attempted == 0:
            return {"state": "FLATTEN_LATCHED", "halt_episode_id": episode}
    except Exception as exc:
        try:
            ticket = locals().get("ticket")
            if ticket is not None:
                controller.mark_flatten_state(
                    episode,
                    ticket=ticket,
                    state="UNKNOWN",
                    evidence={"error": f"{type(exc).__name__}: {exc}"},
                )
        except Exception:
            pass
        return {"state": "WRITE_UNKNOWN", "halt_episode_id": episode, "error": type(exc).__name__}
    return {"state": "FLATTEN_ATTEMPTED", "halt_episode_id": episode, "response": results}
