"""Central order lifecycle state machine.

Enum values intentionally retain the strings already used by the legacy
SQLite ledger.  New states are additive so old rows remain readable.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable

from .contracts import BrokerEvidence


class OrderState(str, Enum):
    INTENT = "INTENT"
    RISK_APPROVED = "RISK_APPROVED"
    STAGED = "STAGED"
    OPERATOR_APPROVED = "OPERATOR_APPROVED"
    CHECK_RETRYABLE = "CHECK_RETRYABLE"
    CHECK_REJECTED = "CHECK_REJECTED"
    PRE_SEND_DEFERRED = "PRE_SEND_DEFERRED"
    SEND_ARMED = "SEND_ARMED"
    WRITE_CLAIMED = "WRITE_CLAIMED"
    WRITE_ATTEMPTED = "WRITE_ATTEMPTED"
    SUBMITTED = "SUBMITTED"
    SEND_PARTIAL = "SEND_PARTIAL"
    SEND_REJECTED = "SEND_REJECTED"
    SEND_UNKNOWN = "SEND_UNKNOWN"
    PENDING_CONFIRMED = "PENDING_CONFIRMED"
    PARTIAL_FILL = "PARTIAL_FILL"
    OPEN_PROTECTED = "OPEN_PROTECTED"
    OPEN_UNPROTECTED = "OPEN_UNPROTECTED"
    LINKED_EXISTING = "LINKED_EXISTING"
    CLOSED_SL = "CLOSED_SL"
    CLOSED_TP = "CLOSED_TP"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCEL_ARMED = "CANCEL_ARMED"
    CANCEL_ACKNOWLEDGED = "CANCEL_ACKNOWLEDGED"
    CANCEL_UNKNOWN = "CANCEL_UNKNOWN"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN_NO_SEND = "UNKNOWN_NO_SEND"
    BROKER_REQUEST_MISMATCH_NO_SEND = "BROKER_REQUEST_MISMATCH_NO_SEND"
    FILTER_BLOCKED = "FILTER_BLOCKED"
    FILTER_UNRESOLVED_DEFERRED = "FILTER_UNRESOLVED_DEFERRED"
    FILTER_EXPIRED_NO_SEND = "FILTER_EXPIRED_NO_SEND"
    PARTIAL_EXIT_NO_SEND = "PARTIAL_EXIT_NO_SEND"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"
    WINDOW_EXPIRED = "WINDOW_EXPIRED"
    WINDOW_NOT_OPEN = "WINDOW_NOT_OPEN"
    PREFIX_REQUIRED = "PREFIX_REQUIRED"


class InvalidOrderTransition(RuntimeError):
    pass


TERMINAL_STATES = frozenset(
    {
        OrderState.CLOSED_SL,
        OrderState.CLOSED_TP,
        OrderState.FILLED,
        OrderState.CHECK_REJECTED,
        OrderState.SEND_REJECTED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.UNKNOWN_NO_SEND,
        OrderState.BROKER_REQUEST_MISMATCH_NO_SEND,
        OrderState.FILTER_BLOCKED,
        OrderState.FILTER_EXPIRED_NO_SEND,
        OrderState.NOT_EXECUTABLE,
        OrderState.WINDOW_EXPIRED,
        OrderState.WINDOW_NOT_OPEN,
        OrderState.PREFIX_REQUIRED,
    }
)


# Legacy states are included explicitly.  There is one authoritative map for
# both new and migrated records; no caller may invent an ad-hoc transition.
TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.INTENT: frozenset({OrderState.RISK_APPROVED, OrderState.STAGED, OrderState.CHECK_RETRYABLE, OrderState.PRE_SEND_DEFERRED, OrderState.FILTER_BLOCKED, OrderState.FILTER_UNRESOLVED_DEFERRED, OrderState.FILTER_EXPIRED_NO_SEND}),
    OrderState.RISK_APPROVED: frozenset({OrderState.STAGED, OrderState.PRE_SEND_DEFERRED}),
    OrderState.STAGED: frozenset({OrderState.OPERATOR_APPROVED, OrderState.PRE_SEND_DEFERRED}),
    OrderState.OPERATOR_APPROVED: frozenset({OrderState.SEND_ARMED, OrderState.PRE_SEND_DEFERRED}),
    OrderState.CHECK_RETRYABLE: frozenset({OrderState.INTENT, OrderState.STAGED, OrderState.PRE_SEND_DEFERRED, OrderState.CHECK_REJECTED}),
    OrderState.CHECK_REJECTED: frozenset(),
    OrderState.PRE_SEND_DEFERRED: frozenset({OrderState.INTENT, OrderState.RISK_APPROVED, OrderState.STAGED, OrderState.OPERATOR_APPROVED, OrderState.FILTER_EXPIRED_NO_SEND}),
    OrderState.SEND_ARMED: frozenset({OrderState.SUBMITTED, OrderState.SEND_PARTIAL, OrderState.SEND_REJECTED, OrderState.SEND_UNKNOWN, OrderState.CANCEL_ARMED, OrderState.UNKNOWN_NO_SEND}),
    OrderState.SUBMITTED: frozenset({OrderState.PENDING_CONFIRMED, OrderState.PARTIAL_FILL, OrderState.OPEN_PROTECTED, OrderState.OPEN_UNPROTECTED, OrderState.FILLED, OrderState.CANCEL_ARMED, OrderState.SEND_UNKNOWN}),
    OrderState.SEND_PARTIAL: frozenset({OrderState.PARTIAL_FILL, OrderState.OPEN_PROTECTED, OrderState.OPEN_UNPROTECTED, OrderState.CANCEL_ARMED, OrderState.SEND_UNKNOWN}),
    OrderState.SEND_REJECTED: frozenset(),
    OrderState.SEND_UNKNOWN: frozenset({OrderState.SUBMITTED, OrderState.PENDING_CONFIRMED, OrderState.PARTIAL_FILL, OrderState.OPEN_PROTECTED, OrderState.OPEN_UNPROTECTED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.CANCEL_ARMED, OrderState.UNKNOWN_NO_SEND}),
    OrderState.PENDING_CONFIRMED: frozenset({OrderState.PARTIAL_FILL, OrderState.FILLED, OrderState.CANCEL_ARMED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED, OrderState.SEND_UNKNOWN}),
    OrderState.PARTIAL_FILL: frozenset({OrderState.FILLED, OrderState.OPEN_PROTECTED, OrderState.OPEN_UNPROTECTED, OrderState.CLOSED_SL, OrderState.CLOSED_TP, OrderState.CANCEL_ARMED, OrderState.PARTIAL_EXIT_NO_SEND, OrderState.SEND_UNKNOWN}),
    OrderState.OPEN_PROTECTED: frozenset({OrderState.PARTIAL_FILL, OrderState.FILLED, OrderState.CLOSED_SL, OrderState.CLOSED_TP, OrderState.CANCEL_ARMED, OrderState.SEND_UNKNOWN}),
    OrderState.OPEN_UNPROTECTED: frozenset({OrderState.OPEN_PROTECTED, OrderState.PARTIAL_FILL, OrderState.CLOSED_SL, OrderState.CLOSED_TP, OrderState.CANCEL_ARMED, OrderState.SEND_UNKNOWN}),
    OrderState.LINKED_EXISTING: frozenset({OrderState.OPEN_PROTECTED, OrderState.PARTIAL_FILL, OrderState.CLOSED_SL, OrderState.CLOSED_TP, OrderState.CANCEL_ARMED}),
    OrderState.CLOSED_SL: frozenset(),
    OrderState.CLOSED_TP: frozenset(),
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCEL_ARMED: frozenset({OrderState.CANCEL_ACKNOWLEDGED, OrderState.CANCEL_REJECTED, OrderState.CANCEL_UNKNOWN, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED, OrderState.PARTIAL_FILL, OrderState.OPEN_PROTECTED, OrderState.SEND_UNKNOWN}),
    OrderState.CANCEL_ACKNOWLEDGED: frozenset({OrderState.CANCELLED, OrderState.EXPIRED, OrderState.PARTIAL_FILL, OrderState.OPEN_PROTECTED, OrderState.SEND_UNKNOWN}),
    OrderState.CANCEL_UNKNOWN: frozenset({OrderState.CANCELLED, OrderState.EXPIRED, OrderState.PARTIAL_FILL, OrderState.OPEN_PROTECTED, OrderState.SEND_UNKNOWN}),
    OrderState.CANCEL_REJECTED: frozenset({OrderState.CANCEL_UNKNOWN, OrderState.PARTIAL_FILL, OrderState.OPEN_PROTECTED, OrderState.SEND_UNKNOWN}),
    OrderState.CANCELLED: frozenset(),
    OrderState.EXPIRED: frozenset(),
    OrderState.UNKNOWN_NO_SEND: frozenset(),
    OrderState.BROKER_REQUEST_MISMATCH_NO_SEND: frozenset(),
    OrderState.FILTER_BLOCKED: frozenset(),
    OrderState.FILTER_UNRESOLVED_DEFERRED: frozenset({OrderState.FILTER_EXPIRED_NO_SEND, OrderState.STAGED}),
    OrderState.FILTER_EXPIRED_NO_SEND: frozenset(),
    OrderState.PARTIAL_EXIT_NO_SEND: frozenset({OrderState.PARTIAL_FILL, OrderState.OPEN_PROTECTED, OrderState.CLOSED_SL, OrderState.CLOSED_TP}),
    OrderState.NOT_EXECUTABLE: frozenset(),
    OrderState.WINDOW_EXPIRED: frozenset(),
    OrderState.WINDOW_NOT_OPEN: frozenset(),
    OrderState.PREFIX_REQUIRED: frozenset(),
}


def _state(value: OrderState | str) -> OrderState:
    try:
        return value if isinstance(value, OrderState) else OrderState(str(value))
    except ValueError as exc:
        raise InvalidOrderTransition(f"unknown order state: {value!r}") from exc


BROKER_DERIVED_STATES = frozenset({
    OrderState.SUBMITTED, OrderState.SEND_PARTIAL, OrderState.SEND_REJECTED,
    OrderState.SEND_UNKNOWN, OrderState.PENDING_CONFIRMED, OrderState.PARTIAL_FILL,
    OrderState.OPEN_PROTECTED, OrderState.OPEN_UNPROTECTED, OrderState.LINKED_EXISTING,
    OrderState.CLOSED_SL, OrderState.CLOSED_TP, OrderState.FILLED, OrderState.REJECTED,
    OrderState.CANCEL_ACKNOWLEDGED, OrderState.CANCEL_UNKNOWN, OrderState.CANCEL_REJECTED,
    OrderState.CANCELLED, OrderState.EXPIRED,
})


def transition_order(
    current: OrderState | str,
    target: OrderState | str,
    *,
    evidence: BrokerEvidence | None = None,
    order_id: str | None = None,
    audit: Callable[..., Any] | None = None,
    halt: Callable[..., Any] | None = None,
) -> OrderState:
    """Return the target state only when the transition is legal."""

    source = _state(current)
    destination = _state(target)
    legal = destination in TRANSITIONS.get(source, frozenset())
    if destination in BROKER_DERIVED_STATES and not isinstance(evidence, BrokerEvidence):
        legal = False
    if isinstance(evidence, BrokerEvidence):
        expected_operation = "CANCEL" if source in {
            OrderState.CANCEL_ARMED, OrderState.CANCEL_ACKNOWLEDGED,
            OrderState.CANCEL_UNKNOWN, OrderState.CANCEL_REJECTED,
        } or destination in {
            OrderState.CANCEL_ACKNOWLEDGED, OrderState.CANCEL_UNKNOWN,
            OrderState.CANCEL_REJECTED, OrderState.CANCELLED, OrderState.EXPIRED,
        } else "RECONCILE" if destination in {
            OrderState.PENDING_CONFIRMED, OrderState.PARTIAL_FILL, OrderState.OPEN_PROTECTED,
            OrderState.OPEN_UNPROTECTED, OrderState.LINKED_EXISTING, OrderState.CLOSED_SL,
            OrderState.CLOSED_TP, OrderState.FILLED,
        } else "SEND"
        if destination in BROKER_DERIVED_STATES and evidence.operation != expected_operation:
            legal = False
    if not legal:
        details = {
            "order_id": order_id,
            "from_state": source.value,
            "to_state": destination.value,
            "reason": "ILLEGAL_ORDER_TRANSITION",
        }
        halt_error: Exception | None = None
        if halt is not None:
            try:
                if callable(halt):
                    halt("ILLEGAL_ORDER_TRANSITION", details)
                elif hasattr(halt, "trigger"):
                    halt.trigger("ILLEGAL_ORDER_TRANSITION", **details)
            except Exception as exc:  # HALT remains the first safety action.
                halt_error = exc
        if audit is not None:
            try:
                if callable(audit):
                    audit("ILLEGAL_ORDER_TRANSITION", details)
                elif hasattr(audit, "append"):
                    audit.append(
                        "ILLEGAL_ORDER_TRANSITION",
                        entity_type="order",
                        entity_id=order_id or "",
                        payload=details,
                    )
            except Exception:
                # The failed audit is itself a safety failure; do not turn an
                # illegal transition into an apparently recoverable one.
                pass
        if halt_error is not None:
            raise InvalidOrderTransition(
                f"illegal order transition {source.value} -> {destination.value}; HALT write failed"
            ) from halt_error
        raise InvalidOrderTransition(
            f"illegal order transition {source.value} -> {destination.value}"
        )
    return destination


class OrderStateMachine:
    def __init__(
        self,
        state: OrderState | str = OrderState.INTENT,
        *,
        order_id: str | None = None,
        audit: Callable[..., Any] | None = None,
        halt: Callable[..., Any] | None = None,
    ) -> None:
        self.state = _state(state)
        self.order_id = order_id
        self.audit = audit
        self.halt = halt

    def transition(self, target: OrderState | str, *, evidence: BrokerEvidence | None = None) -> OrderState:
        self.state = transition_order(
            self.state,
            target,
            evidence=evidence,
            order_id=self.order_id,
            audit=self.audit,
            halt=self.halt,
        )
        return self.state

    def reconcile(self, broker_state: OrderState | str, *, evidence: BrokerEvidence) -> OrderState:
        """Apply broker-confirmed state only; reconciliation is an event."""
        return self.transition(broker_state, evidence=evidence)


def reconcile_order(
    current: OrderState | str,
    broker_state: OrderState | str,
    **kwargs: Any,
) -> OrderState:
    return transition_order(current, broker_state, **kwargs)
