"""The only order lifecycle that is allowed to reach a broker-write adapter."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from ..signals import SignalProposal
from .approval import ApprovalError, ApprovalStore, proposal_hash, wire_request_hash
from .audit_ledger import AuditLedger
from .contracts import BrokerEvidence, BrokerSnapshot, ExecutionAdapter, InstrumentContract, RiskApprovedOrder
from .execution import issue_persistence_receipt
from .halt import HaltController
from .instruments import InstrumentRegistry
from .order_state import OrderState, OrderStateMachine
from .risk_guard import RiskGuard
from .settings import RuntimeSettings
from .strategy_health import StrategyHealth


class ProductionFlowError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProductionDependencies:
    risk_guard: RiskGuard
    order_state: OrderStateMachine
    audit_ledger: AuditLedger
    halt_controller: HaltController
    instrument_registry: InstrumentRegistry
    approval_store: ApprovalStore
    runtime_settings: RuntimeSettings
    strategy_health: StrategyHealth
    execution_adapter: ExecutionAdapter


REQUIRED_DEPENDENCIES = tuple(ProductionDependencies.__dataclass_fields__)


class ProductionOrderFlow:
    def __init__(self, dependencies: ProductionDependencies, *, event_sink: Callable[[str], Any] | None = None) -> None:
        self.dependencies = dependencies
        self.event_sink = event_sink
        self.sequence: list[str] = []

    def _event(self, name: str) -> None:
        self.sequence.append(name)
        if self.event_sink is not None:
            self.event_sink(name)

    @staticmethod
    def _persist_state(
        connection: Any,
        proposal_id: str,
        state: OrderState,
        request_hash: str = "",
        request: dict[str, Any] | None = None,
        approval_id: str = "",
    ) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS order_state_records (
                order_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                request_hash TEXT NOT NULL DEFAULT '',
                request_json TEXT NOT NULL DEFAULT '',
                approval_id TEXT NOT NULL DEFAULT '',
                updated_at_utc TEXT NOT NULL
            )"""
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(order_state_records)")}
        for name, definition in (("request_json", "TEXT NOT NULL DEFAULT ''"), ("approval_id", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                connection.execute(f'ALTER TABLE order_state_records ADD COLUMN "{name}" {definition}')
        request_json = "" if request is None else json.dumps(dict(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        connection.execute(
            """INSERT INTO order_state_records(order_id,state,request_hash,request_json,approval_id,updated_at_utc)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET state=excluded.state,
               request_hash=CASE WHEN excluded.request_hash <> '' THEN excluded.request_hash ELSE order_state_records.request_hash END,
               request_json=CASE WHEN excluded.request_json <> '' THEN excluded.request_json ELSE order_state_records.request_json END,
               approval_id=CASE WHEN excluded.approval_id <> '' THEN excluded.approval_id ELSE order_state_records.approval_id END,
               updated_at_utc=excluded.updated_at_utc""",
            (proposal_id, state.value, request_hash, request_json, approval_id, datetime.now(timezone.utc).isoformat()),
        )

    def _audit_state(
        self,
        proposal: SignalProposal,
        state: OrderState,
        *,
        campaign_id: str,
        account_key: str,
        payload: dict[str, Any] | None = None,
        request_hash: str = "",
        event_type: str | None = None,
    ) -> None:
        self.dependencies.audit_ledger.append(
            event_type or state.value,
            entity_type="order",
            entity_id=proposal.proposal_id,
            campaign_id=campaign_id,
            account_key=account_key,
            payload=payload or {},
            state_update=lambda connection: self._persist_state(connection, proposal.proposal_id, state, request_hash),
        )

    def _require_dependencies(self) -> None:
        for name in REQUIRED_DEPENDENCIES:
            if getattr(self.dependencies, name, None) is None:
                raise ProductionFlowError(f"missing production dependency: {name}")
        if not callable(getattr(self.dependencies.execution_adapter, "reconcile", None)):
            raise ProductionFlowError("execution adapter has no broker reconciliation method")

    def send(
        self,
        proposal: SignalProposal,
        *,
        snapshot_provider: Callable[[], BrokerSnapshot],
        approval_id: str,
        campaign_id: str,
        account_key: str,
        release_id: str,
        candidate_hash: str,
        approval_type: str = "limit",
    ) -> Any:
        self._require_dependencies()
        d = self.dependencies
        state = d.order_state if d.order_state.state is OrderState.INTENT else OrderStateMachine()
        self._event("SignalProposal")
        d.halt_controller.assert_clear()
        if not d.strategy_health.allows_new_position():
            raise ProductionFlowError("strategy health blocks final send")
        d.instrument_registry.get(proposal.instrument_id)
        first = snapshot_provider()
        if not isinstance(first, BrokerSnapshot):
            raise ProductionFlowError("first broker snapshot is not typed")
        self._event("first RiskGuard")
        precheck = d.risk_guard.precheck(proposal, first)
        if not precheck.approved:
            raise ProductionFlowError(precheck.reason)
        planned = d.risk_guard.plan(proposal, first)
        request_builder = getattr(d.execution_adapter, "request_for_order", None)
        if not callable(request_builder):
            raise ProductionFlowError("execution adapter cannot construct the exact staged wire request")
        staged_wire_request = request_builder(planned)
        if not isinstance(staged_wire_request, dict):
            raise ProductionFlowError("staged wire request is malformed")
        staged_wire_hash = wire_request_hash(staged_wire_request)
        state.transition(OrderState.RISK_APPROVED)
        self._audit_state(
            proposal, state.state, campaign_id=campaign_id, account_key=account_key,
            payload={"snapshot_hash": precheck.snapshot_hash, "risk_cash": planned.risk_cash, "stop_risk": planned.stop_risk, "volume": planned.volume},
        )
        self._event("STAGED")
        staged_proposal = {
            "proposal_id": proposal.proposal_id,
            "candidate_hash": proposal.candidate_hash,
            "instrument_id": proposal.instrument_id,
            "direction": proposal.direction,
            "entry_price": proposal.entry_price,
            "stop_price": proposal.stop_price,
            "target_price": proposal.target_price,
            "decision_time": proposal.decision_time.isoformat(),
            "expires_at": proposal.expires_at.isoformat(),
            "evidence_hash": proposal.evidence_hash,
        }
        if proposal.broker_symbol:
            staged_proposal["broker_symbol"] = proposal.broker_symbol
        if proposal.instrument_registry_sha256:
            staged_proposal["instrument_registry_sha256"] = proposal.instrument_registry_sha256
        if approval_type != "limit":
            staged_proposal["approval_type"] = approval_type
        staged_proposal["wire_request"] = staged_wire_request
        staged_proposal["wire_request_hash"] = staged_wire_hash
        existing = d.approval_store.get_proposal(proposal.proposal_id)
        if existing is None or str(existing.get("state")) != "APPROVED":
            d.approval_store.stage(
                staged_proposal,
                campaign_id=campaign_id,
                account_key=account_key,
                release_id=release_id,
                candidate_hash=candidate_hash,
                approval_type=approval_type,
            )
        state.transition(OrderState.STAGED)
        self._audit_state(proposal, state.state, campaign_id=campaign_id, account_key=account_key, payload={"proposal_hash": proposal_hash(staged_proposal)})
        try:
            approval = d.approval_store.show(approval_id)
        except ApprovalError as exc:
            raise ProductionFlowError("single-use operator approval is missing") from exc
        if approval.state != "APPROVED":
            raise ProductionFlowError("single-use operator approval is missing")
        if approval.approval_type != approval_type:
            raise ProductionFlowError("operator approval purpose does not match the proposal")
        self._event("verified operator approval")
        state.transition(OrderState.OPERATOR_APPROVED)
        self._audit_state(proposal, state.state, campaign_id=campaign_id, account_key=account_key, payload={"approval_id": approval_id})
        second = snapshot_provider()
        if second is first or not isinstance(second, BrokerSnapshot):
            raise ProductionFlowError("final risk check did not use a fresh broker snapshot")
        self._event("fresh broker snapshot")
        order = d.risk_guard.approve(proposal, second, approval_id=approval_id)
        request_for_order = getattr(d.execution_adapter, "request_for_order", None)
        wire_request = request_for_order(order)
        if not isinstance(wire_request, dict):
            raise ProductionFlowError("execution request is malformed")
        observed_wire_hash = wire_request_hash(wire_request)
        expected_wire_hash = str(staged_proposal.get("wire_request_hash") or "")
        if not expected_wire_hash or observed_wire_hash != expected_wire_hash:
            d.halt_controller.trigger(
                "WIRE_REQUEST_HASH_MISMATCH",
                proposal_id=proposal.proposal_id,
                expected_wire_request_hash=expected_wire_hash,
                observed_wire_request_hash=observed_wire_hash,
            )
            raise ProductionFlowError("execution request differs from the staged wire request")
        if not approval.wire_request_hash or approval.wire_request_hash != expected_wire_hash:
            d.halt_controller.trigger(
                "WIRE_REQUEST_HASH_MISMATCH",
                proposal_id=proposal.proposal_id,
                expected_wire_request_hash=approval.wire_request_hash,
                observed_wire_request_hash=expected_wire_hash,
            )
            raise ProductionFlowError("operator approval is bound to a different wire request")
        order = replace(order, request_hash=observed_wire_hash)
        validate_request = getattr(d.execution_adapter, "validate_request", None)
        if callable(validate_request):
            validate_request(order)

        same_db = Path(d.approval_store.path).resolve() == Path(d.audit_ledger.path).resolve()

        def arm(connection: Any, _request_digest: str, _approval_type: str) -> None:
            write_port = getattr(d.execution_adapter, "write_port", None)
            if write_port is not None:
                write_port.arm_in_transaction(
                    connection,
                    order_id=proposal.proposal_id,
                    request=wire_request,
                    request_hash=_request_digest,
                    approval_id=approval_id,
                )
            else:
                self._persist_state(
                    connection, proposal.proposal_id, OrderState.SEND_ARMED,
                    _request_digest, dict(wire_request), approval_id,
                )
            if same_db:
                d.audit_ledger.append_in_transaction(
                    "SEND_ARMED",
                    entity_type="order",
                    entity_id=proposal.proposal_id,
                    campaign_id=campaign_id,
                    account_key=account_key,
                    payload={"approval_id": approval_id, "snapshot_hash": order.snapshot_hash, "request_hash": order.request_hash},
                    connection=connection,
                )

        try:
            consumed = d.approval_store.consume_and_arm(
                approval_id,
                proposal=staged_proposal,
                campaign_id=campaign_id,
                account_key=account_key,
                release_id=release_id,
                candidate_hash=candidate_hash,
                lease_nonce=approval.lease_nonce,
                operator_sid=approval.operator_sid,
                order_id=proposal.proposal_id,
                request=wire_request,
                arm=arm,
            )
        except ApprovalError as exc:
            raise ProductionFlowError("approval consumption failed closed") from exc
        if consumed.state != "CONSUMED":
            raise ProductionFlowError("approval was not atomically consumed")
        persisted = replace(order, persisted=True, persistence_receipt=issue_persistence_receipt(order))
        state.transition(OrderState.SEND_ARMED)
        try:
            if same_db:
                d.audit_ledger.deliver_pending()
            else:
                d.audit_ledger.append(
                    "SEND_ARMED",
                    entity_type="order",
                    entity_id=proposal.proposal_id,
                    campaign_id=campaign_id,
                    account_key=account_key,
                    payload={"approval_id": approval_id, "snapshot_hash": order.snapshot_hash, "request_hash": order.request_hash},
                )
        except Exception as exc:
            d.halt_controller.trigger(
                "AUDIT_ANCHOR_UNACKED",
                proposal_id=proposal.proposal_id,
                error=str(exc),
            )
            raise ProductionFlowError("SEND_ARMED audit anchor is not acknowledged") from exc
        self._event("SEND_ARMED+audit")
        try:
            result = d.execution_adapter.send(persisted)
            self._event("mt5.order_send")
            d.audit_ledger.append(
                "WRITE_ATTEMPTED",
                entity_type="order",
                entity_id=proposal.proposal_id,
                campaign_id=campaign_id,
                account_key=account_key,
                payload={"approval_id": approval_id, "request_hash": persisted.request_hash},
            )
            evidence = d.execution_adapter.reconcile(result, persisted)
        except Exception as exc:
            try:
                d.halt_controller.trigger(
                    "BROKER_STATE_UNKNOWN_AFTER_SEND",
                    proposal_id=proposal.proposal_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception as halt_exc:
                raise ProductionFlowError("broker readback failed and HALT could not be persisted") from halt_exc
            raise ProductionFlowError("broker write/readback state is unknown; HALT is active") from exc
        try:
            if not isinstance(evidence, BrokerEvidence):
                raise ProductionFlowError("broker reconciliation did not return typed evidence")
            if evidence.retcode in {10008, 10009, 10010}:
                state.transition(OrderState.SUBMITTED, evidence=evidence)
            else:
                state.transition(OrderState.SEND_REJECTED, evidence=evidence)
            self._audit_state(
                proposal,
                state.state,
                campaign_id=campaign_id,
                account_key=account_key,
                payload={"retcode": evidence.retcode, "ticket": evidence.ticket, "deal_id": evidence.deal_id, "broker_state": evidence.broker_state},
                request_hash=persisted.request_hash,
            )
        except Exception as exc:
            try:
                d.halt_controller.trigger(
                    "POST_WRITE_PERSISTENCE_FAILURE",
                    proposal_id=proposal.proposal_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception as halt_exc:
                raise ProductionFlowError("post-write persistence failed and HALT could not be persisted") from halt_exc
            raise ProductionFlowError("post-write persistence failed; HALT is active") from exc
        self._event("broker reconciliation")
        return result


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
