"""Lease-bound, single-use approvals in the canonical order database."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping
from uuid import uuid4


class ApprovalError(RuntimeError):
    pass


def proposal_hash(proposal: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(proposal), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
    ).hexdigest()


def wire_request_hash(request: Mapping[str, Any]) -> str:
    """Hash exactly the canonical request that may reach the broker."""
    return hashlib.sha256(
        json.dumps(dict(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    state: str
    lease_id: str
    lease_nonce: str
    operator_sid: str
    campaign_id: str
    account_key: str
    proposal_id: str
    proposal_hash: str
    approval_type: str
    issued_at_utc: str
    expires_at_utc: str
    release_id: str = ""
    candidate_hash: str = ""
    reason: str = ""
    wire_request_hash: str = ""


def canonical_store_path(output_root: str | Path) -> Path:
    """Return the only permitted mutable order database path."""
    return Path(output_root) / "orders" / "idempotency.sqlite3"


class ApprovalStore:
    """SQLite approval lifecycle with proposal identity as the unique key."""

    def __init__(self, path: str | Path, *, now: Callable[[], datetime] | None = None, halt: Callable[..., Any] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.halt = halt
        self.connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None, check_same_thread=False)
        self.connection.execute("PRAGMA busy_timeout=30000")
        journal = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal is None or str(journal[0]).lower() != "wal":
            raise ApprovalError("approval store requires SQLite WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS staged_proposals (
                proposal_id TEXT PRIMARY KEY,
                proposal_hash TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                account_key TEXT NOT NULL,
                release_id TEXT NOT NULL,
                candidate_hash TEXT NOT NULL,
                approval_type TEXT NOT NULL DEFAULT 'limit',
                state TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                lease_nonce TEXT NOT NULL,
                operator_sid TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                account_key TEXT NOT NULL,
                proposal_id TEXT NOT NULL UNIQUE,
                proposal_hash TEXT NOT NULL,
                approval_type TEXT NOT NULL,
                issued_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                release_id TEXT NOT NULL,
                candidate_hash TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                wire_request_hash TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Fail closed on partial migrations.  Defaulted columns can be added;
        # duplicate proposal lifecycles must never be silently repaired.
        staged_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(staged_proposals)").fetchall()
        }
        approval_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(approvals)").fetchall()
        }
        if "approval_type" not in staged_columns:
            self.connection.execute(
                "ALTER TABLE staged_proposals ADD COLUMN approval_type TEXT NOT NULL DEFAULT 'limit'"
            )
        if "wire_request_hash" not in approval_columns:
            self.connection.execute(
                "ALTER TABLE approvals ADD COLUMN wire_request_hash TEXT NOT NULL DEFAULT ''"
            )
        duplicates = self.connection.execute(
            "SELECT proposal_id, COUNT(*) FROM approvals GROUP BY proposal_id HAVING COUNT(*) > 1"
        ).fetchall()
        if duplicates:
            raise ApprovalError("approval database contains duplicate proposal lifecycles")
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_approvals_proposal_id ON approvals(proposal_id)"
        )
        self.connection.execute("CREATE INDEX IF NOT EXISTS ix_staged_proposals_state ON staged_proposals(state)")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS approval_state_outbox (
                event_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, approval_id TEXT NOT NULL,
                state TEXT NOT NULL, payload_canonical_json TEXT NOT NULL,
                event_hash TEXT NOT NULL, created_at_utc TEXT NOT NULL,
                delivery_state TEXT NOT NULL DEFAULT 'PENDING'
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS entry_slot_reservations (
                account_key TEXT NOT NULL,
                trade_date_ny TEXT NOT NULL,
                order_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('RESERVED', 'RELEASED')),
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY(account_key, trade_date_ny, order_id)
            )"""
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_entry_slots_day ON entry_slot_reservations(account_key, trade_date_ny, instrument_id, state)"
        )

    def _audit_tx(self, *, proposal_id: str, approval_id: str, state: str, at: str, **bindings: Any) -> None:
        payload = {"proposal_id": proposal_id, "approval_id": approval_id, "state": state, "at_utc": at, **bindings}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        self.connection.execute(
            "INSERT INTO approval_state_outbox(event_id,proposal_id,approval_id,state,payload_canonical_json,event_hash,created_at_utc) VALUES(?,?,?,?,?,?,?)",
            (uuid4().hex, proposal_id, approval_id, state, canonical, hashlib.sha256(canonical.encode()).hexdigest(), at),
        )

    @staticmethod
    def _require_lease(lease: Mapping[str, Any]) -> None:
        required = ("lease_id", "invocation_nonce", "runner_sid", "authorized_operator_sid", "campaign_id", "release_id", "state")
        account = lease.get("account") or lease.get("expected_account_login")
        if any(not str(lease.get(key) or "").strip() for key in required) or not str(account or "").strip() or lease.get("state") != "ACTIVE":
            raise ApprovalError("active signed daily lease is required")

    @staticmethod
    def _record(row: tuple[Any, ...]) -> ApprovalRecord:
        values = [str(value) for value in row]
        if len(values) == len(ApprovalRecord.__slots__) - 1:
            values.append("")
        return ApprovalRecord(*values)

    def _persistent_halt(self, reason: str, **details: Any) -> None:
        if self.halt is not None:
            try:
                self.halt(reason, **details)
            except TypeError:
                self.halt(reason)

    def stage(self, proposal: Mapping[str, Any], *, campaign_id: str, account_key: str, release_id: str, candidate_hash: str, approval_type: str = "limit", now: datetime | None = None) -> str:
        proposal_id = str(proposal.get("proposal_id") or "").strip()
        if not proposal_id or not campaign_id or not account_key or not release_id or not candidate_hash:
            raise ApprovalError("STAGED proposal is missing a signed binding")
        if approval_type not in {"market", "limit", "stop", "SMOKE"}:
            raise ApprovalError("approval type is not allowlisted")
        digest = proposal_hash(proposal)
        timestamp = now or self.now()
        timestamp = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
        stamp = timestamp.astimezone(timezone.utc).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO staged_proposals(proposal_id,proposal_hash,proposal_json,campaign_id,account_key,release_id,candidate_hash,approval_type,state,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, digest, json.dumps(dict(proposal), sort_keys=True, separators=(",", ":"), default=str), campaign_id, account_key, release_id, candidate_hash, approval_type, "STAGED", stamp, stamp),
            ).rowcount
            if inserted == 1:
                self._audit_tx(proposal_id=proposal_id, approval_id="", state="STAGED", at=stamp,
                               proposal_hash=digest, campaign_id=campaign_id, account_key=account_key,
                               release_id=release_id, candidate_hash=candidate_hash)
                self.connection.commit()
                return proposal_id
            row = self.connection.execute(
                "SELECT proposal_hash,campaign_id,account_key,release_id,candidate_hash,approval_type,state FROM staged_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise ApprovalError("proposal insert was ignored but no lifecycle row exists")
            matches = (
                tuple(str(item) for item in row[:5])
                == (digest, campaign_id, account_key, release_id, candidate_hash)
                and str(row[5]) == approval_type
            )
            if not matches:
                self._persistent_halt(
                    "PROPOSAL_BINDING_CONFLICT",
                    proposal_id=proposal_id,
                    existing_state=str(row[6]),
                )
                raise ApprovalError("proposal binding conflict; persistent HALT is required")
            if str(row[6]) != "STAGED":
                raise ApprovalError("proposal lifecycle is already terminal or approved")
            self.connection.commit()
            return proposal_id
        except Exception:
            self.connection.rollback()
            raise

    def staged(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT proposal_id,proposal_hash,proposal_json,campaign_id,account_key,release_id,candidate_hash,approval_type,state,created_at_utc,updated_at_utc FROM staged_proposals WHERE state='STAGED' ORDER BY created_at_utc,proposal_id"
        ).fetchall()
        return [
            {"proposal_id": str(row[0]), "proposal_hash": str(row[1]), "proposal": json.loads(str(row[2])), "campaign_id": str(row[3]), "account_key": str(row[4]), "release_id": str(row[5]), "candidate_hash": str(row[6]), "approval_type": str(row[7]), "state": str(row[8]), "created_at_utc": str(row[9]), "updated_at_utc": str(row[10])}
            for row in rows
        ]

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT proposal_id,proposal_hash,proposal_json,campaign_id,account_key,release_id,"
            "candidate_hash,approval_type,state,created_at_utc,updated_at_utc "
            "FROM staged_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "proposal_id": str(row[0]), "proposal_hash": str(row[1]),
            "proposal": json.loads(str(row[2])), "campaign_id": str(row[3]),
            "account_key": str(row[4]), "release_id": str(row[5]),
            "candidate_hash": str(row[6]), "approval_type": str(row[7]),
            "state": str(row[8]), "created_at_utc": str(row[9]),
            "updated_at_utc": str(row[10]),
        }

    def list(self) -> list[ApprovalRecord]:
        rows = self.connection.execute(
            "SELECT approval_id,state,lease_id,lease_nonce,operator_sid,campaign_id,account_key,proposal_id,proposal_hash,approval_type,issued_at_utc,expires_at_utc,release_id,candidate_hash,reason,wire_request_hash FROM approvals ORDER BY issued_at_utc,approval_id"
        ).fetchall()
        return [self._record(row) for row in rows]

    def show(self, approval_id: str) -> ApprovalRecord:
        row = self.connection.execute(
            "SELECT approval_id,state,lease_id,lease_nonce,operator_sid,campaign_id,account_key,proposal_id,proposal_hash,approval_type,issued_at_utc,expires_at_utc,release_id,candidate_hash,reason,wire_request_hash FROM approvals WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ApprovalError("approval not found")
        return self._record(row)

    def show_for_proposal(self, proposal_id: str) -> ApprovalRecord:
        row = self.connection.execute(
            "SELECT approval_id,state,lease_id,lease_nonce,operator_sid,campaign_id,account_key,proposal_id,proposal_hash,approval_type,issued_at_utc,expires_at_utc,release_id,candidate_hash,reason,wire_request_hash FROM approvals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ApprovalError("approval not found for proposal")
        return self._record(row)

    def find_approved(self, proposal_id: str, *, campaign_id: str, account_key: str, release_id: str, candidate_hash: str, now: datetime | None = None) -> ApprovalRecord | None:
        current = now or self.now()
        current = current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)
        row = self.connection.execute(
            "SELECT approval_id,state,lease_id,lease_nonce,operator_sid,campaign_id,account_key,proposal_id,proposal_hash,approval_type,issued_at_utc,expires_at_utc,release_id,candidate_hash,reason,wire_request_hash FROM approvals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        record = self._record(row)
        if record.state != "APPROVED":
            return None
        if current.astimezone(timezone.utc) >= datetime.fromisoformat(record.expires_at_utc):
            stamp = current.astimezone(timezone.utc).isoformat()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                if self.connection.execute("UPDATE approvals SET state='EXPIRED' WHERE approval_id=? AND state='APPROVED'", (record.approval_id,)).rowcount == 1:
                    self.connection.execute("UPDATE staged_proposals SET state='EXPIRED',updated_at_utc=? WHERE proposal_id=? AND state='APPROVED'", (stamp, proposal_id))
                    self._audit_tx(proposal_id=proposal_id, approval_id=record.approval_id, state="EXPIRED", at=stamp)
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            return None
        if (record.campaign_id, record.account_key, record.release_id, record.candidate_hash) != (campaign_id, account_key, release_id, candidate_hash):
            raise ApprovalError("approved proposal binding differs from current runtime")
        return record

    def approve(self, proposal_id: str, *, lease: Mapping[str, Any], operator_sid: str, release_id: str, candidate_hash: str, approval_type: str | None = None, now: datetime | None = None) -> ApprovalRecord:
        self._require_lease(lease)
        configured_operator = str(lease.get("authorized_operator_sid") or "").strip()
        runner_sid = str(lease.get("runner_sid") or "").strip()
        if (
            not re.fullmatch(r"S-\d-(?:\d+-)+\d+", runner_sid)
            or not re.fullmatch(r"S-\d-(?:\d+-)+\d+", configured_operator)
            or not re.fullmatch(r"S-\d-(?:\d+-)+\d+", operator_sid)
            or operator_sid != configured_operator
            or operator_sid == runner_sid
        ):
            raise ApprovalError("operator SID must be the signed interactive operator and differ from Runner")
        issued = now or self.now()
        issued = issued if issued.tzinfo is not None else issued.replace(tzinfo=timezone.utc)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT proposal_json,proposal_hash,campaign_id,account_key,release_id,candidate_hash,approval_type,state FROM staged_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None or str(row[7]) != "STAGED":
                raise ApprovalError("proposal must be a persisted STAGED proposal")
            proposal = json.loads(str(row[0]))
            expiry = datetime.fromisoformat(str(proposal["expires_at"]))
            expiry = expiry if expiry.tzinfo is not None else expiry.replace(tzinfo=timezone.utc)
            selected_type = str(row[6])
            if approval_type is not None and approval_type != selected_type:
                raise ApprovalError("approval type is persisted in the order request")
            expires = min(
                issued + timedelta(seconds=30 if selected_type in {"market", "SMOKE"} else 300),
                expiry,
            )
            if expires <= issued or str(row[2]) != str(lease["campaign_id"]) or str(row[3]) != str(lease.get("account") or lease.get("expected_account_login")) or str(row[4]) != release_id or str(row[5]) != candidate_hash or str(lease["release_id"]) != release_id:
                raise ApprovalError("proposal/lease binding mismatch or expiry")
            if self.connection.execute("SELECT 1 FROM approvals WHERE proposal_id=?", (proposal_id,)).fetchone() is not None:
                raise ApprovalError("proposal already has an approval lifecycle")
            exact_wire_hash = str(proposal.get("wire_request_hash") or "")
            wire_request = proposal.get("wire_request")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", exact_wire_hash):
                raise ApprovalError("STAGED proposal wire_request_hash must be exact 64-hex")
            if not isinstance(wire_request, Mapping) or wire_request_hash(wire_request) != exact_wire_hash.lower():
                raise ApprovalError("STAGED proposal wire_request does not match wire_request_hash")
            record = ApprovalRecord(uuid4().hex, "APPROVED", str(lease["lease_id"]), str(lease["invocation_nonce"]), operator_sid, str(row[2]), str(row[3]), proposal_id, str(row[1]), selected_type, issued.astimezone(timezone.utc).isoformat(), expires.astimezone(timezone.utc).isoformat(), release_id, candidate_hash, "", exact_wire_hash)
            self.connection.execute(
                "INSERT INTO approvals(approval_id,state,lease_id,lease_nonce,operator_sid,campaign_id,account_key,proposal_id,proposal_hash,approval_type,issued_at_utc,expires_at_utc,release_id,candidate_hash,reason,wire_request_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(getattr(record, field) for field in record.__slots__),
            )
            self.connection.execute("UPDATE staged_proposals SET state='APPROVED',updated_at_utc=? WHERE proposal_id=? AND state='STAGED'", (issued.astimezone(timezone.utc).isoformat(), proposal_id))
            self._audit_tx(proposal_id=proposal_id, approval_id=record.approval_id, state="APPROVED",
                           at=issued.astimezone(timezone.utc).isoformat(), proposal_hash=record.proposal_hash,
                           wire_request_hash=record.wire_request_hash, operator_sid=operator_sid,
                           lease_id=record.lease_id, lease_nonce=record.lease_nonce)
            self.connection.commit()
            return record
        except Exception:
            self.connection.rollback()
            raise

    def _reserve_entry_slot(
        self,
        *,
        account_key: str,
        trade_date_ny: str,
        order_id: str,
        instrument_id: str,
        campaign_id: str,
        instrument_limit: int,
        total_limit: int,
        at_utc: str,
    ) -> None:
        if (
            not account_key or not trade_date_ny or not order_id or not instrument_id or not campaign_id
            or isinstance(instrument_limit, bool) or not isinstance(instrument_limit, int) or instrument_limit < 1
            or isinstance(total_limit, bool) or not isinstance(total_limit, int) or total_limit < 1
        ):
            raise ApprovalError("daily entry-slot binding is incomplete")
        existing = self.connection.execute(
            "SELECT instrument_id,campaign_id,state FROM entry_slot_reservations WHERE account_key=? AND trade_date_ny=? AND order_id=?",
            (account_key, trade_date_ny, order_id),
        ).fetchone()
        if existing is not None:
            if tuple(str(value) for value in existing) != (instrument_id, campaign_id, "RESERVED"):
                raise ApprovalError("entry-slot reservation binding mismatch")
            return
        instrument_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM entry_slot_reservations WHERE account_key=? AND trade_date_ny=? AND instrument_id=? AND state='RESERVED'",
            (account_key, trade_date_ny, instrument_id),
        ).fetchone()[0])
        total_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM entry_slot_reservations WHERE account_key=? AND trade_date_ny=? AND state='RESERVED'",
            (account_key, trade_date_ny),
        ).fetchone()[0])
        if instrument_count >= instrument_limit or total_count >= total_limit:
            raise ApprovalError("daily entry slot limit is active")
        self.connection.execute(
            "INSERT INTO entry_slot_reservations(account_key,trade_date_ny,order_id,instrument_id,campaign_id,state,created_at_utc) VALUES(?,?,?,?,?,?,?)",
            (account_key, trade_date_ny, order_id, instrument_id, campaign_id, "RESERVED", at_utc),
        )

    def consume_and_arm(self, approval_id: str, *, proposal: Mapping[str, Any], campaign_id: str, account_key: str, release_id: str, candidate_hash: str, lease_nonce: str, operator_sid: str, order_id: str, request: Mapping[str, Any], arm: Callable[[sqlite3.Connection, str, str], Any], now: datetime | None = None, slot_date_ny: str | None = None, slot_instrument_id: str | None = None, slot_instrument_limit: int | None = None, slot_total_limit: int | None = None) -> ApprovalRecord:
        current = now or self.now()
        current = current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)
        request_digest = wire_request_hash(request)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT approval_id,state,lease_id,lease_nonce,operator_sid,campaign_id,account_key,proposal_id,proposal_hash,approval_type,issued_at_utc,expires_at_utc,release_id,candidate_hash,reason,wire_request_hash FROM approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if row is None or str(row[1]) != "APPROVED":
                raise ApprovalError("approval is missing, consumed, rejected, or expired")
            if current.astimezone(timezone.utc) >= datetime.fromisoformat(str(row[11])):
                stamp = current.astimezone(timezone.utc).isoformat()
                self.connection.execute("UPDATE approvals SET state='EXPIRED' WHERE approval_id=? AND state='APPROVED'", (approval_id,))
                self.connection.execute("UPDATE staged_proposals SET state='EXPIRED',updated_at_utc=? WHERE proposal_id=? AND state='APPROVED'", (stamp, str(row[7])))
                self._audit_tx(proposal_id=str(row[7]), approval_id=approval_id, state="EXPIRED", at=stamp)
                self.connection.commit()
                raise ApprovalError("approval is expired")
            observed = (str(row[8]), str(row[5]), str(row[6]), str(row[12]), str(row[13]), str(row[3]), str(row[4]), str(row[7]))
            expected = (proposal_hash(proposal), campaign_id, account_key, release_id, candidate_hash, lease_nonce, operator_sid, order_id)
            if observed != expected:
                raise ApprovalError("approval binding mismatch")
            if str(proposal.get("approval_type") or "limit") != str(row[9]):
                raise ApprovalError("approval purpose does not match the persisted proposal")
            stored_wire_hash = str(row[15] or "")
            if stored_wire_hash != request_digest:
                raise ApprovalError("exact wire request hash differs from the operator approval")
            if any(value is not None for value in (slot_date_ny, slot_instrument_id, slot_instrument_limit, slot_total_limit)):
                if None in (slot_date_ny, slot_instrument_id, slot_instrument_limit, slot_total_limit):
                    raise ApprovalError("entry-slot reservation binding is incomplete")
                self._reserve_entry_slot(
                    account_key=account_key,
                    trade_date_ny=str(slot_date_ny),
                    order_id=order_id,
                    instrument_id=str(slot_instrument_id),
                    campaign_id=campaign_id,
                    instrument_limit=slot_instrument_limit,  # type: ignore[arg-type]
                    total_limit=slot_total_limit,  # type: ignore[arg-type]
                    at_utc=current.astimezone(timezone.utc).isoformat(),
                )
            if self.connection.execute("UPDATE approvals SET state='CONSUMED' WHERE approval_id=? AND state='APPROVED'", (approval_id,)).rowcount != 1:
                raise ApprovalError("approval replay rejected")
            if self.connection.execute("UPDATE staged_proposals SET state='CONSUMED',updated_at_utc=? WHERE proposal_id=? AND state='APPROVED'", (current.astimezone(timezone.utc).isoformat(), order_id)).rowcount != 1:
                raise ApprovalError("proposal lifecycle could not be consumed")
            self._audit_tx(proposal_id=order_id, approval_id=approval_id, state="CONSUMED",
                           at=current.astimezone(timezone.utc).isoformat(), wire_request_hash=request_digest)
            arm(self.connection, request_digest, str(row[9]))
            self.connection.commit()
            return self._record((row[0], "CONSUMED", *row[2:]))
        except Exception:
            self.connection.rollback()
            raise

    def consume(self, approval_id: str, *, proposal: Mapping[str, Any], campaign_id: str, account_key: str, release_id: str, candidate_hash: str, lease_nonce: str, operator_sid: str, now: datetime | None = None) -> ApprovalRecord:
        wire_request = proposal.get("wire_request")
        if not isinstance(wire_request, Mapping):
            raise ApprovalError("consume requires the exact staged wire request")
        return self.consume_and_arm(approval_id, proposal=proposal, campaign_id=campaign_id, account_key=account_key, release_id=release_id, candidate_hash=candidate_hash, lease_nonce=lease_nonce, operator_sid=operator_sid, order_id=str(proposal.get("proposal_id") or ""), request=wire_request, arm=lambda _connection, _request_hash, _approval_type: None, now=now)

    def reject(self, proposal_id: str, *, reason: str = "") -> ApprovalRecord:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT approval_id,proposal_id,state FROM approvals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None or str(row[2]) != "APPROVED":
                raise ApprovalError("approval not found or already terminal")
            if self.connection.execute("UPDATE approvals SET state='REJECTED',reason=? WHERE proposal_id=? AND state='APPROVED'", (reason, proposal_id)).rowcount != 1:
                raise ApprovalError("approval replay rejected")
            rejected_at = self.now()
            if rejected_at.tzinfo is None:
                rejected_at = rejected_at.replace(tzinfo=timezone.utc)
            self.connection.execute("UPDATE staged_proposals SET state='REJECTED',updated_at_utc=? WHERE proposal_id=? AND state='APPROVED'", (rejected_at.astimezone(timezone.utc).isoformat(), str(row[1])))
            self._audit_tx(proposal_id=str(row[1]), approval_id=str(row[0]), state="REJECTED",
                           at=rejected_at.astimezone(timezone.utc).isoformat(), reason=reason)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.show(str(row[0]))

    def is_valid(self, approval_id: str, proposal: Mapping[str, Any], *, now: datetime | None = None) -> bool:
        try:
            record = self.show(approval_id)
            current = now or self.now()
            current = current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)
            return record.state == "APPROVED" and current.astimezone(timezone.utc) < datetime.fromisoformat(record.expires_at_utc) and record.proposal_hash == proposal_hash(proposal)
        except (ApprovalError, ValueError):
            return False

    def close(self) -> None:
        self.connection.close()
