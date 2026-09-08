"""Typed boundaries between strategy, risk, broker reads, and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from numbers import Real
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..signals import SignalProposal


@dataclass(frozen=True, slots=True)
class InstrumentContract:
    instrument_id: str
    asset_class: str
    broker: str
    expected_server: str
    broker_symbol: str
    timezone: str
    base_currency: str
    profit_currency: str
    margin_currency: str
    digits: int
    point: float
    tick_size: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    pip_size: float
    cost_unit: str
    trade_calc_mode: int

    def __post_init__(self) -> None:
        for name in ("instrument_id", "asset_class", "broker", "expected_server", "broker_symbol", "timezone", "base_currency", "profit_currency", "margin_currency", "cost_unit"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.digits, bool) or not isinstance(self.digits, int) or self.digits < 0:
            raise ValueError("digits must be a non-negative integer")
        if isinstance(self.trade_calc_mode, bool) or not isinstance(self.trade_calc_mode, Real) or not math.isfinite(float(self.trade_calc_mode)) or float(self.trade_calc_mode) < 0:
            raise ValueError("trade_calc_mode must be a finite non-negative number")
        for name in ("point", "tick_size", "contract_size", "volume_min", "volume_max", "volume_step", "pip_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.volume_max < self.volume_min:
            raise ValueError("volume_max must be at least volume_min")


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    """One causally-bound, broker-read view used by the final risk check."""

    as_of: datetime
    equity: float
    margin_free: float
    open_positions: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    pending_orders: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    # Critical broker facts intentionally have fail-closed sentinels.  A
    # caller must populate them from the current broker snapshot; constructor
    # defaults must never manufacture a healthy account state.
    daily_realized_r: float | None = None
    strategy_health: str = ""
    approval: bool | None = None
    halt: bool | None = None
    instrument_contract: InstrumentContract | None = None
    total_stop_risk: float | None = None
    leverage: float | None = None
    pair_exposure_percent: float | None = None
    concentration_percent: float | None = None
    deals: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    snapshot_hash: str = ""
    order_calc_profit: Callable[..., Any] | None = None
    order_calc_margin: Callable[..., Any] | None = None


@dataclass(frozen=True, slots=True)
class BrokerEvidence:
    """Typed readback evidence; a transport response is not broker state."""

    operation: str
    retcode: int
    ticket: int | None = None
    deal_id: int | None = None
    broker_state: str = ""
    observed_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.operation not in {"SEND", "CLOSE", "CANCEL", "RECONCILE"}:
            raise ValueError("unsupported broker evidence operation")
        if isinstance(self.retcode, bool) or not isinstance(self.retcode, int):
            raise ValueError("broker evidence retcode is required")
        if self.ticket is not None and (isinstance(self.ticket, bool) or self.ticket <= 0):
            raise ValueError("broker evidence ticket is invalid")
        if self.deal_id is not None and (isinstance(self.deal_id, bool) or self.deal_id <= 0):
            raise ValueError("broker evidence deal_id is invalid")


# Naming used by callers that prefer the more explicit term.
RiskSnapshot = BrokerSnapshot


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str
    checked_at: datetime
    snapshot_hash: str = ""
    stop_risk: float | None = None
    margin_required: float | None = None
    risk_cash: float | None = None

    @property
    def state(self) -> str:
        return "APPROVED" if self.approved else "DENIED"


@dataclass(frozen=True, slots=True)
class RiskApprovedOrder:
    """The only order object accepted by an execution adapter."""

    proposal: SignalProposal
    volume: float
    risk_cash: float
    stop_risk: float
    margin_required: float
    approved_at: datetime
    snapshot_hash: str = ""
    approval_id: str = ""
    request_hash: str = ""
    persisted: bool = False
    persistence_receipt: str = ""

    @property
    def proposal_id(self) -> str:
        return self.proposal.proposal_id

    @property
    def instrument_id(self) -> str:
        return self.proposal.instrument_id


class ExecutionAdapter(Protocol):
    """Execution boundary; implementations must never accept raw proposals."""

    def send(self, order: RiskApprovedOrder) -> Any:
        """Submit one already approved order, without automatic write retry."""
