"""Typed boundaries between strategy, risk, broker reads, and execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
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
    canonical_underlying: str = ""
    expected_company: str = ""
    broker_path_regex: str = ""
    broker_description_regex: str = ""
    session_calendar_id: str = ""
    expected_one_tick_value_at_min_volume: float | None = None
    economic_value_tolerance: float | None = None

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
        semantic = (self.canonical_underlying, self.expected_company, self.broker_path_regex,
                    self.broker_description_regex, self.session_calendar_id)
        if any(semantic) and not all(isinstance(value, str) and value.strip() for value in semantic):
            raise ValueError("semantic instrument fields must be complete")
        for name in ("expected_one_tick_value_at_min_volume", "economic_value_tolerance"):
            value = getattr(self, name)
            if any(semantic) and (isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) or float(value) <= 0):
                raise ValueError(f"{name} must be finite and positive")

    @property
    def contract_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class LiveRiskPolicy:
    daily_loss_cap_r: float
    max_trades_per_day_by_instrument: tuple[tuple[str, int], ...]
    max_total_trades_per_day: int
    instrument_whitelist: tuple[tuple[str, str], ...]
    max_open_positions_by_instrument: int
    max_total_open_positions: int
    entry_cooldown_seconds: int
    max_position_volume_by_instrument: tuple[tuple[str, float], ...]
    max_total_stop_risk_percent: float = 2.2
    max_margin_fraction: float = 0.25
    max_leverage: float = 5.0
    max_pair_exposure_percent: float = 100.0
    max_concentration_percent: float = 100.0

    def __post_init__(self) -> None:
        numeric = (self.daily_loss_cap_r, self.max_total_stop_risk_percent, self.max_margin_fraction, self.max_leverage, self.max_pair_exposure_percent, self.max_concentration_percent)
        if any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) for value in numeric):
            raise ValueError("live risk policy contains non-finite numeric values")
        if self.daily_loss_cap_r != -1.0 or self.max_total_trades_per_day != 3 or self.entry_cooldown_seconds != 300:
            raise ValueError("live risk policy differs from the v3 fixed limits")
        if self.max_open_positions_by_instrument != 1 or self.max_total_open_positions != 2:
            raise ValueError("live risk position limits differ from v3")
        trade_limits = dict(self.max_trades_per_day_by_instrument)
        whitelist = dict(self.instrument_whitelist)
        volumes = dict(self.max_position_volume_by_instrument)
        if set(trade_limits) != set(whitelist) or set(volumes) != set(whitelist) or sorted(trade_limits.values()) != [1, 2]:
            raise ValueError("live risk instrument limits are incomplete")
        if any(
            not key or not symbol or isinstance(trade_limits.get(key), bool) or trade_limits.get(key, 0) <= 0
            for key, symbol in self.instrument_whitelist
        ):
            raise ValueError("live risk whitelist is invalid")
        if any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) or value <= 0 for value in volumes.values()):
            raise ValueError("live risk volume limits are invalid")

    @property
    def policy_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


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
    total_entry_count: int | None = None
    entry_counts_by_instrument: Mapping[str, int] = field(default_factory=dict)
    open_position_counts_by_instrument: Mapping[str, int] = field(default_factory=dict)
    pending_order_counts_by_instrument: Mapping[str, int] = field(default_factory=dict)
    last_accepted_entry_at: datetime | None = None
    last_terminal_loss_at: datetime | None = None
    owned_deal_ids: Sequence[str] = field(default_factory=tuple)
    policy_hash: str = ""
    instrument_contract_hash: str = ""


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
    policy_hash: str = ""

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
