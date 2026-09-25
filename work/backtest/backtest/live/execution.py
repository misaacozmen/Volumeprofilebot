"""Execution adapters with a strict approved-order boundary."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable
import hashlib

from .contracts import BrokerEvidence, RiskApprovedOrder
from .audit_ledger import AuditLedger


class ExecutionBoundaryError(TypeError):
    pass


class Mt5WritePort:
    """The sole MT5 write boundary.

    ``send`` accepts only a durable order id.  The request is read from the
    canonical SQLite ledger after a compare-and-swap claim; callers cannot
    smuggle a new raw request or permission flag across this boundary.
    """

    ACCEPTED_RETCODES = frozenset({10008, 10009, 10010})

    def __init__(self, mt5: Any, db_path: str | Path, *, mutex: Callable[[], Any] | None = None) -> None:
        self.mt5 = mt5
        self.db_path = Path(db_path)
        self.mutex = mutex or (lambda: nullcontext())

    def _physical_send(self, request: Mapping[str, Any]) -> Any:
        return self.mt5.order_send(dict(request))

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS order_state_records (
                order_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                request_hash TEXT NOT NULL DEFAULT '',
                request_json TEXT NOT NULL DEFAULT '',
                approval_id TEXT NOT NULL DEFAULT '',
                operation_type TEXT NOT NULL DEFAULT 'ENTRY',
                authorization_kind TEXT NOT NULL DEFAULT 'OPERATOR_APPROVAL',
                authorization_reference TEXT NOT NULL DEFAULT '',
                client_request_id TEXT NOT NULL DEFAULT '',
                final_snapshot_hash TEXT NOT NULL DEFAULT '',
                final_policy_hash TEXT NOT NULL DEFAULT '',
                final_risk_expires_at TEXT NOT NULL DEFAULT '',
                updated_at_utc TEXT NOT NULL
            )"""
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(order_state_records)")}
        for name, definition in (
            ("request_json", "TEXT NOT NULL DEFAULT ''"), ("approval_id", "TEXT NOT NULL DEFAULT ''"),
            ("operation_type", "TEXT NOT NULL DEFAULT 'ENTRY'"),
            ("authorization_kind", "TEXT NOT NULL DEFAULT 'OPERATOR_APPROVAL'"),
            ("authorization_reference", "TEXT NOT NULL DEFAULT ''"),
            ("client_request_id", "TEXT NOT NULL DEFAULT ''"),
            ("final_snapshot_hash", "TEXT NOT NULL DEFAULT ''"),
            ("final_policy_hash", "TEXT NOT NULL DEFAULT ''"),
            ("final_risk_expires_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                connection.execute(f'ALTER TABLE order_state_records ADD COLUMN "{name}" {definition}')

    def arm_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        order_id: str,
        request: Mapping[str, Any],
        request_hash: str,
        approval_id: str,
        final_snapshot_hash: str,
        final_policy_hash: str,
        final_risk_expires_at: str,
    ) -> None:
        self._ensure_schema(connection)
        request_json = json.dumps(dict(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        updated = connection.execute(
            """UPDATE order_state_records
               SET state='SEND_ARMED', request_hash=?, request_json=?, approval_id=?,
                   final_snapshot_hash=?,final_policy_hash=?,final_risk_expires_at=?,updated_at_utc=datetime('now')
               WHERE order_id=? AND state IN ('SEND_ARMED','OPERATOR_APPROVED','STAGED','RISK_APPROVED','INTENT')""",
            (request_hash, request_json, approval_id, final_snapshot_hash, final_policy_hash, final_risk_expires_at, order_id),
        ).rowcount
        if updated != 1:
            raise ExecutionBoundaryError("SEND_ARMED request was not durably persisted")

    def _claim(self, order_id: str) -> dict[str, Any]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT state,request_hash,request_json,approval_id,authorization_kind,authorization_reference,operation_type,final_snapshot_hash,final_policy_hash,final_risk_expires_at FROM order_state_records WHERE order_id=?",
                (order_id,),
            ).fetchone()
            if row is None or str(row[0]) != "SEND_ARMED":
                raise ExecutionBoundaryError("order is not durably SEND_ARMED")
            request_hash = str(row[1] or "")
            try:
                request = json.loads(str(row[2]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ExecutionBoundaryError("durable broker request is corrupt") from exc
            if not isinstance(request, dict) or len(request_hash) != 64 or not str(row[3] or ""):
                raise ExecutionBoundaryError("durable broker request binding is incomplete")
            if str(row[4]) != "OPERATOR_APPROVAL" or str(row[5] or row[3]) != str(row[3]):
                raise ExecutionBoundaryError("broker write authorization kind/reference is invalid")
            approval = connection.execute(
                "SELECT state,wire_request_hash FROM approvals WHERE approval_id=?",
                (str(row[3]),),
            ).fetchone()
            if approval is None or str(approval[0]) != "CONSUMED":
                raise ExecutionBoundaryError("consumed approval does not match the durable wire request")
            if str(row[6]) in {"ENTRY", "SMOKE"} and str(approval[1] or "") != request_hash:
                raise ExecutionBoundaryError("consumed approval does not match the durable wire request")
            if str(row[6]) in {"ENTRY", "SMOKE"}:
                if len(str(row[7] or "")) != 64 or len(str(row[8] or "")) != 64:
                    raise ExecutionBoundaryError("final risk hash binding is incomplete")
                try:
                    expiry = datetime.fromisoformat(str(row[9]))
                except ValueError as exc:
                    raise ExecutionBoundaryError("final risk expiry is invalid") from exc
                expiry = expiry if expiry.tzinfo is not None else expiry.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= expiry.astimezone(timezone.utc):
                    raise ExecutionBoundaryError("final risk decision is expired")
            audit = connection.execute(
                """SELECT e.payload_canonical_json FROM audit_chain_events e
                   JOIN audit_anchor_outbox o ON o.event_id=e.event_id
                   WHERE e.entity_type='order' AND e.entity_id=? AND e.event_type='SEND_ARMED' AND o.state='ACKED'
                   ORDER BY e.sequence DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
            if audit is None:
                raise ExecutionBoundaryError("SEND_ARMED audit ACK is missing")
            try:
                audit_payload = json.loads(str(audit[0]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ExecutionBoundaryError("SEND_ARMED audit payload is corrupt") from exc
            if not isinstance(audit_payload, dict) or str(audit_payload.get("request_hash") or "") != request_hash:
                raise ExecutionBoundaryError("SEND_ARMED audit does not bind the exact wire request")
            if str(row[6]) in {"ENTRY", "SMOKE"}:
                final_audit = connection.execute(
                    """SELECT e.payload_canonical_json FROM audit_chain_events e
                       JOIN audit_anchor_outbox o ON o.event_id=e.event_id
                       WHERE e.entity_type='order' AND e.entity_id=? AND e.event_type='RISK_APPROVED' AND o.state='ACKED'
                       ORDER BY e.sequence DESC LIMIT 1""",
                    (order_id,),
                ).fetchone()
                if final_audit is None:
                    raise ExecutionBoundaryError("final RISK_APPROVED audit ACK is missing")
                try:
                    final_payload = json.loads(str(final_audit[0]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ExecutionBoundaryError("final risk audit payload is corrupt") from exc
                if (
                    not isinstance(final_payload, dict)
                    or final_payload.get("request_hash") != request_hash
                    or final_payload.get("snapshot_hash") != str(row[7])
                    or final_payload.get("policy_hash") != str(row[8])
                    or final_payload.get("expires_at") != str(row[9])
                ):
                    raise ExecutionBoundaryError("final risk audit binding mismatch")
            unresolved = connection.execute(
                "SELECT 1 FROM order_state_records WHERE state IN ('WRITE_CLAIMED','WRITE_ATTEMPTED','SEND_UNKNOWN') LIMIT 1"
            ).fetchone()
            if unresolved is not None:
                raise ExecutionBoundaryError("an unresolved broker write blocks the next write")
            claimed = connection.execute(
                "UPDATE order_state_records SET state='WRITE_CLAIMED',updated_at_utc=datetime('now') WHERE order_id=? AND state='SEND_ARMED'",
                (order_id,),
            ).rowcount
            if claimed != 1:
                raise ExecutionBoundaryError("broker write claim was lost")
            connection.commit()
            return request
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def arm_operator_operation(
        self,
        operation_id: str,
        *,
        operation_type: str,
        request: Mapping[str, Any],
        approval_id: str,
        campaign_id: str,
        account_key: str,
        final_snapshot_hash: str = "",
        final_policy_hash: str = "",
        final_risk_expires_at: str = "",
    ) -> str:
        if operation_type not in {"ENTRY", "SMOKE", "CANCEL", "CLOSE"}:
            raise ExecutionBoundaryError("operation type is not allowlisted")
        if operation_type in {"ENTRY", "SMOKE"} and (
            len(final_snapshot_hash) != 64 or len(final_policy_hash) != 64 or not final_risk_expires_at
        ):
            raise ExecutionBoundaryError("ENTRY/SMOKE requires an exact final-risk authorization")
        request_json = json.dumps(dict(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        client_request_id = hashlib.sha256(f"{operation_id}\0{request_hash}".encode("utf-8")).hexdigest()
        ledger = AuditLedger(self.db_path, campaign_id=campaign_id, account_key=account_key)

        def persist(connection: sqlite3.Connection) -> None:
            self._ensure_schema(connection)
            approval = connection.execute(
                "SELECT state FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
            if approval is None or str(approval[0]) != "CONSUMED":
                raise ExecutionBoundaryError("operation requires a consumed operator approval")
            connection.execute(
                """INSERT INTO order_state_records
                   (order_id,state,request_hash,request_json,approval_id,operation_type,
                   authorization_kind,authorization_reference,client_request_id,
                   final_snapshot_hash,final_policy_hash,final_risk_expires_at,updated_at_utc)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                (operation_id, "SEND_ARMED", request_hash, request_json, approval_id, operation_type,
                 "OPERATOR_APPROVAL", approval_id, client_request_id,
                 final_snapshot_hash, final_policy_hash, final_risk_expires_at),
            )

        try:
            if operation_type in {"ENTRY", "SMOKE"}:
                ledger.append(
                    "RISK_APPROVED", entity_type="order", entity_id=operation_id,
                    payload={
                        "request_hash": request_hash, "snapshot_hash": final_snapshot_hash,
                        "policy_hash": final_policy_hash, "expires_at": final_risk_expires_at,
                    },
                )
            ledger.append(
                "SEND_ARMED", entity_type="order", entity_id=operation_id,
                payload={
                    "request_hash": request_hash, "operation_type": operation_type,
                    "authorization_kind": "OPERATOR_APPROVAL",
                    "authorization_reference": approval_id,
                    "client_request_id": client_request_id,
                },
                state_update=persist,
            )
        finally:
            ledger.close()
        return operation_id

    def send(self, order_id: str) -> Any:
        if not isinstance(order_id, str) or not order_id.strip():
            raise ExecutionBoundaryError("MT5 write port accepts a durable order_id only")
        with self.mutex():
            request = self._claim(order_id)
            self._mark_attempt(order_id, "WRITE_ATTEMPTED")
            try:
                return self._physical_send(request)
            except Exception:
                self._mark_attempt(order_id, "SEND_UNKNOWN", expected_state="WRITE_ATTEMPTED")
                raise

    def _mark_attempt(self, order_id: str, state: str, *, expected_state: str = "WRITE_CLAIMED") -> None:
        connection = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            connection.execute(
                "UPDATE order_state_records SET state=?,updated_at_utc=datetime('now') WHERE order_id=? AND state=?",
                (state, order_id, expected_state),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

def issue_persistence_receipt(order: RiskApprovedOrder) -> str:
    """Return a deterministic DB-verifiable receipt; no process-memory auth."""
    return hashlib.sha256(
        f"{order.proposal_id}\0{order.approval_id}\0{order.request_hash}".encode("utf-8")
    ).hexdigest()


def _is_persisted_risk_approved(order: RiskApprovedOrder) -> bool:
    if not order.persisted or len(order.request_hash) != 64 or not order.snapshot_hash or not order.approval_id:
        return False
    expected = hashlib.sha256(
        f"{order.proposal_id}\0{order.approval_id}\0{order.request_hash}".encode("utf-8")
    ).hexdigest()
    return bool(order.persistence_receipt) and order.persistence_receipt == expected


@dataclass(frozen=True, slots=True)
class FakeExecutionResult:
    state: str
    event_id: str
    proposal_id: str


class FakeExecutionAdapter:
    """Deterministic adapter for tests; it never imports or calls a broker SDK."""

    def __init__(self) -> None:
        self.calls: list[RiskApprovedOrder] = []
        self.claim_receipt: Callable[[str], bool] = lambda _receipt: True

    def send(self, order: RiskApprovedOrder) -> FakeExecutionResult:
        if not isinstance(order, RiskApprovedOrder):
            raise ExecutionBoundaryError("execution accepts RiskApprovedOrder only")
        if not _is_persisted_risk_approved(order) or not self.claim_receipt(order.persistence_receipt):
            raise ExecutionBoundaryError("only persisted, hashed RISK_APPROVED orders may execute")
        self.calls.append(order)
        # A transport return is only a write-attempt acknowledgement.  The
        # caller must reconcile broker state before recording SUBMITTED/FILLED.
        return FakeExecutionResult("WRITE_ATTEMPTED", order.approval_id, order.proposal_id)

    def request_for_order(self, order: RiskApprovedOrder) -> dict[str, Any]:
        return {"request_hash": order.request_hash, "approval_id": order.approval_id}

    def reconcile(self, response: FakeExecutionResult, order: RiskApprovedOrder) -> BrokerEvidence:
        if not isinstance(response, FakeExecutionResult) or response.proposal_id != order.proposal_id:
            raise ExecutionBoundaryError("fake broker readback is not bound to the order")
        return BrokerEvidence("SEND", 10009, broker_state="READBACK_CONFIRMED", raw={"fake": True})


def _response_value(response: object, name: str) -> object:
    if isinstance(response, Mapping):
        return response.get(name)
    return getattr(response, name, None)


class Mt5ExecutionAdapter:
    """Legacy contract adapter for tests; live Super1 never constructs it.

    The callback is explicit so this compatibility layer cannot create a
    second broker SDK write path.  The only installed physical MT5 write is
    the Super1 client's ``_order_send_checked`` boundary.
    """

    ACCEPTED_RETCODES = frozenset({10008, 10009, 10010})

    def __init__(self, mt5: Any, *, request_builder: Any, write_once: Callable[[Mapping[str, Any]], Any] | None = None, readback: Any = None, claim_receipt: Callable[[str], bool] | None = None) -> None:
        self.mt5 = mt5
        self.request_builder = request_builder
        self.write_once = write_once
        self.readback = readback
        self.calls = 0
        self.claim_receipt = claim_receipt or (lambda _receipt: True)

    def send(self, order: RiskApprovedOrder) -> Any:
        if not isinstance(order, RiskApprovedOrder):
            raise ExecutionBoundaryError("execution accepts RiskApprovedOrder only")
        if not _is_persisted_risk_approved(order) or not self.claim_receipt(order.persistence_receipt):
            raise ExecutionBoundaryError("only persisted, hashed RISK_APPROVED orders may execute")
        request = self.request_for_order(order)
        if not isinstance(request, Mapping):
            raise ExecutionBoundaryError("execution request is malformed")
        if not callable(self.write_once):
            raise ExecutionBoundaryError("legacy adapter has no explicit test write callback")
        self.calls += 1
        # No retry is permitted after this point. Reconciliation must inspect
        # the returned retcode and then read broker state.  This callback is
        # test-injected; it is not a production SDK boundary.
        return self.write_once(dict(request))

    def request_for_order(self, order: RiskApprovedOrder) -> dict[str, Any]:
        request = self.request_builder(order)
        if not isinstance(request, Mapping):
            raise ExecutionBoundaryError("execution request is malformed")
        return dict(request)

    def reconcile(self, response: object, order: RiskApprovedOrder) -> BrokerEvidence:
        retcode = _response_value(response, "retcode")
        if isinstance(retcode, bool) or not isinstance(retcode, int):
            raise ExecutionBoundaryError("broker response has no accepted-operation retcode")
        observed = self.readback(order, response) if callable(self.readback) else None
        if observed is None:
            reader = getattr(self.mt5, "orders_get", None) or getattr(self.mt5, "positions_get", None)
            if not callable(reader):
                raise ExecutionBoundaryError("broker readback is missing after order_send")
            observed = reader()
        if observed is None:
            raise ExecutionBoundaryError("broker readback is unknown after order_send")
        ticket = _response_value(response, "order")
        deal_id = _response_value(response, "deal")
        return BrokerEvidence("SEND", retcode, ticket=int(ticket) if isinstance(ticket, int) and ticket > 0 else None, deal_id=int(deal_id) if isinstance(deal_id, int) and deal_id > 0 else None, broker_state="READBACK_CONFIRMED", raw={"response": str(response), "readback": str(observed)})


def execute(adapter: Any, order: RiskApprovedOrder) -> Any:
    """Send once through an adapter; broker writes are never auto-retried."""

    if not isinstance(order, RiskApprovedOrder):
        raise ExecutionBoundaryError("execution accepts RiskApprovedOrder only")
    send = getattr(adapter, "send", None)
    if not callable(send):
        raise ExecutionBoundaryError("adapter does not implement send")
    return send(order)
