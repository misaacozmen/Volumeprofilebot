from __future__ import annotations

import pytest

from backtest.live.order_state import (
    InvalidOrderTransition,
    OrderState,
    OrderStateMachine,
    transition_order,
)
from backtest.live.contracts import BrokerEvidence


def test_legacy_values_and_new_approval_states_are_centralized() -> None:
    assert OrderState.SEND_ARMED.value == "SEND_ARMED"
    assert OrderState.RISK_APPROVED.value == "RISK_APPROVED"
    machine = OrderStateMachine()
    machine.transition(OrderState.RISK_APPROVED)
    machine.transition(OrderState.STAGED)
    assert machine.transition(OrderState.OPERATOR_APPROVED) is OrderState.OPERATOR_APPROVED


def test_submitted_and_filled_require_broker_response() -> None:
    with pytest.raises(InvalidOrderTransition):
        transition_order(OrderState.SEND_ARMED, OrderState.SUBMITTED)
    assert transition_order(OrderState.SEND_ARMED, OrderState.SUBMITTED, evidence=BrokerEvidence("SEND", 10009, ticket=7)) is OrderState.SUBMITTED


def test_illegal_transition_audits_and_halts() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def record(event: str, details: dict[str, object]) -> None:
        calls.append((event, details))

    with pytest.raises(InvalidOrderTransition):
        transition_order(OrderState.FILLED, OrderState.INTENT, audit=record, halt=record, order_id="o-1")
    assert [item[0] for item in calls] == ["ILLEGAL_ORDER_TRANSITION", "ILLEGAL_ORDER_TRANSITION"]
