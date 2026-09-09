"""Final, broker-read risk boundary."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from numbers import Real
from typing import Any, Callable, Mapping

from ..signals import SignalProposal
from .contracts import BrokerSnapshot, InstrumentContract, LiveRiskPolicy, RiskApprovedOrder, RiskDecision


class RiskGuardError(RuntimeError):
    """Raised when an approved-order object cannot be safely constructed."""


def _finite(value: object, field: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise RiskGuardError(f"{field} has the wrong type")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise RiskGuardError(f"{field} is missing, stale, or non-finite")
    return number


def _validate_broker_rows(value: object, field: str) -> None:
    if not isinstance(value, (list, tuple)):
        raise RiskGuardError(f"{field} is missing or has the wrong type")
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise RiskGuardError(f"{field}[{index}] has the wrong type")
        for name, item in row.items():
            if isinstance(item, bool) or not isinstance(item, Real):
                continue
            if not math.isfinite(float(item)):
                raise RiskGuardError(f"{field}[{index}].{name} is non-finite")


def _request_hash(order: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(order, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()


class RiskGuard:
    """Calculate risk from broker facts; callers cannot override risk inputs."""

    def __init__(
        self,
        *,
        pair_cap_r: float = -1.0,
        daily_loss_cap_r: float | None = None,
        base_risk_percent: float = 1.0,
        max_total_stop_risk_percent: float = 2.2,
        max_margin_fraction: float = 0.25,
        max_leverage: float | None = None,
        max_pair_exposure_percent: float | None = None,
        max_concentration_percent: float | None = None,
        max_snapshot_age_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        policy: LiveRiskPolicy | None = None,
    ) -> None:
        self.policy = policy
        if policy is not None:
            daily_loss_cap_r = policy.daily_loss_cap_r
            max_total_stop_risk_percent = policy.max_total_stop_risk_percent
            max_margin_fraction = policy.max_margin_fraction
            max_leverage = policy.max_leverage
            max_pair_exposure_percent = policy.max_pair_exposure_percent
            max_concentration_percent = policy.max_concentration_percent
        self.pair_cap_r = _finite(pair_cap_r, "pair_cap_r")
        self.daily_loss_cap_r = self.pair_cap_r if daily_loss_cap_r is None else _finite(daily_loss_cap_r, "daily_loss_cap_r")
        self.base_risk_percent = _finite(base_risk_percent, "base_risk_percent", nonnegative=True)
        if self.base_risk_percent <= 0:
            raise RiskGuardError("base_risk_percent must be positive")
        self.max_total_stop_risk_percent = _finite(max_total_stop_risk_percent, "max_total_stop_risk_percent", nonnegative=True)
        self.max_margin_fraction = _finite(max_margin_fraction, "max_margin_fraction", nonnegative=True)
        if self.max_margin_fraction > 1.0:
            raise RiskGuardError("max_margin_fraction must not exceed 1")
        self.max_snapshot_age_seconds = _finite(max_snapshot_age_seconds, "max_snapshot_age_seconds", nonnegative=True)
        self.max_leverage = None if max_leverage is None else _finite(max_leverage, "max_leverage", nonnegative=True)
        if self.max_leverage is not None and self.max_leverage <= 0:
            raise RiskGuardError("max_leverage must be positive")
        self.max_pair_exposure_percent = None if max_pair_exposure_percent is None else _finite(max_pair_exposure_percent, "max_pair_exposure_percent", nonnegative=True)
        self.max_concentration_percent = None if max_concentration_percent is None else _finite(max_concentration_percent, "max_concentration_percent", nonnegative=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self.clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _snapshot(source: BrokerSnapshot | Mapping[str, Any] | Callable[[], BrokerSnapshot]) -> BrokerSnapshot | Mapping[str, Any]:
        return source() if callable(source) else source

    @staticmethod
    def _value(snapshot: BrokerSnapshot | Mapping[str, Any], name: str, default: Any = None) -> Any:
        return snapshot.get(name, default) if isinstance(snapshot, Mapping) else getattr(snapshot, name, default)

    @staticmethod
    def _calculator(snapshot: BrokerSnapshot | Mapping[str, Any], name: str) -> Callable[..., Any]:
        value = RiskGuard._value(snapshot, name)
        if not callable(value):
            raise RiskGuardError(f"broker {name} is missing")
        return value

    def _facts(self, proposal: SignalProposal, snapshot: BrokerSnapshot | Mapping[str, Any], now: datetime, *, require_approval: bool) -> tuple[float, InstrumentContract, float, float, float]:
        as_of = self._value(snapshot, "as_of")
        if not isinstance(as_of, datetime):
            raise RiskGuardError("broker snapshot timestamp is missing")
        as_of = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
        age = (now - as_of).total_seconds()
        if age < -1.0 or age > self.max_snapshot_age_seconds:
            raise RiskGuardError("broker snapshot is stale")
        if not isinstance(proposal, SignalProposal):
            raise RiskGuardError("strategy output is not a SignalProposal")
        if now > proposal.expires_at:
            raise RiskGuardError("proposal is expired")
        snapshot_hash = str(self._value(snapshot, "snapshot_hash", "") or "").strip()
        if not snapshot_hash:
            raise RiskGuardError("broker snapshot hash is missing")
        equity = _finite(self._value(snapshot, "equity"), "equity", nonnegative=True)
        if equity <= 0:
            raise RiskGuardError("equity must be positive")
        margin_free = _finite(self._value(snapshot, "margin_free"), "margin_free", nonnegative=True)
        _validate_broker_rows(self._value(snapshot, "open_positions"), "open_positions")
        _validate_broker_rows(self._value(snapshot, "pending_orders"), "pending_orders")
        realized_r = _finite(self._value(snapshot, "daily_realized_r"), "daily_realized_r")
        total_stop_risk = _finite(self._value(snapshot, "total_stop_risk"), "total_stop_risk", nonnegative=True)
        _validate_broker_rows(self._value(snapshot, "deals"), "deals")
        contract = self._value(snapshot, "instrument_contract")
        if not isinstance(contract, InstrumentContract) or contract.instrument_id != proposal.instrument_id:
            raise RiskGuardError("instrument contract does not match proposal")
        if self.policy is not None:
            if self._value(snapshot, "policy_hash") != self.policy.policy_hash:
                raise RiskGuardError("broker snapshot policy hash mismatch")
            if self._value(snapshot, "instrument_contract_hash") != contract.contract_hash:
                raise RiskGuardError("broker snapshot instrument contract hash mismatch")
            whitelist = dict(self.policy.instrument_whitelist)
            if whitelist.get(proposal.instrument_id) != contract.broker_symbol:
                raise RiskGuardError("instrument is outside the exact signed whitelist")
            if proposal.broker_symbol and proposal.broker_symbol != contract.broker_symbol:
                raise RiskGuardError("proposal broker symbol differs from signed contract")
            total_entries = self._value(snapshot, "total_entry_count")
            entry_counts = self._value(snapshot, "entry_counts_by_instrument")
            position_counts = self._value(snapshot, "open_position_counts_by_instrument")
            pending_counts = self._value(snapshot, "pending_order_counts_by_instrument")
            if isinstance(total_entries, bool) or not isinstance(total_entries, int) or total_entries < 0:
                raise RiskGuardError("daily broker entry count is missing")
            if not all(isinstance(value, Mapping) for value in (entry_counts, position_counts, pending_counts)):
                raise RiskGuardError("broker exposure counts are missing")
            def exact_count(values: Mapping[str, Any], key: str) -> int:
                value = values.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise RiskGuardError("broker count is invalid")
                return value
            if total_entries >= self.policy.max_total_trades_per_day or exact_count(entry_counts, proposal.instrument_id) >= dict(self.policy.max_trades_per_day_by_instrument)[proposal.instrument_id]:
                raise RiskGuardError("daily broker trade-count limit is active")
            if sum(exact_count(position_counts, key) for key in whitelist) >= self.policy.max_total_open_positions or exact_count(position_counts, proposal.instrument_id) >= self.policy.max_open_positions_by_instrument:
                raise RiskGuardError("open-position limit is active")
            if exact_count(pending_counts, proposal.instrument_id) > 0:
                raise RiskGuardError("an owned pending order already exists")
            last_entry = self._value(snapshot, "last_accepted_entry_at")
            if last_entry is not None:
                if not isinstance(last_entry, datetime):
                    raise RiskGuardError("last accepted entry time is invalid")
                last_entry = last_entry if last_entry.tzinfo is not None else last_entry.replace(tzinfo=timezone.utc)
                if (now - last_entry).total_seconds() < self.policy.entry_cooldown_seconds:
                    raise RiskGuardError("entry cooldown is active")
        if self._value(snapshot, "halt") is not False:
            raise RiskGuardError("HALT is active")
        if require_approval and self._value(snapshot, "approval") is not True:
            raise RiskGuardError("operator approval is missing")
        if self._value(snapshot, "strategy_health") not in {"ACTIVE", "MONITORING"}:
            raise RiskGuardError("strategy health does not permit new positions")
        if realized_r <= self.pair_cap_r:
            raise RiskGuardError("pair daily loss cap is active")
        if realized_r <= self.daily_loss_cap_r:
            raise RiskGuardError("daily loss cap is active")
        if self.max_leverage is not None:
            leverage = _finite(self._value(snapshot, "leverage"), "leverage", nonnegative=True)
            if leverage <= 0:
                raise RiskGuardError("leverage must be positive")
            if leverage > self.max_leverage:
                raise RiskGuardError("account leverage exceeds the signed limit")
        if self.max_pair_exposure_percent is not None:
            exposure = _finite(self._value(snapshot, "pair_exposure_percent"), "pair_exposure_percent", nonnegative=True)
            if exposure > self.max_pair_exposure_percent:
                raise RiskGuardError("pair exposure exceeds the signed limit")
        if self.max_concentration_percent is not None:
            concentration = _finite(self._value(snapshot, "concentration_percent"), "concentration_percent", nonnegative=True)
            if concentration > self.max_concentration_percent:
                raise RiskGuardError("concentration exceeds the signed limit")

        action = "BUY" if proposal.direction == "long" else "SELL"
        profit_calculator = self._calculator(snapshot, "order_calc_profit")
        profit = profit_calculator(action, contract.broker_symbol, 1.0, proposal.entry_price, proposal.stop_price)
        loss_per_unit = abs(_finite(profit, "order_calc_profit"))
        if loss_per_unit <= 0:
            raise RiskGuardError("broker stop loss calculation is zero")
        risk_cash = equity * self.base_risk_percent / 100.0
        raw_volume = risk_cash / loss_per_unit
        steps = math.floor((raw_volume + contract.volume_step * 1e-9) / contract.volume_step)
        volume = steps * contract.volume_step
        if volume < contract.volume_min or volume > contract.volume_max:
            raise RiskGuardError("calculated volume is outside signed instrument limits")
        if self.policy is not None and volume > dict(self.policy.max_position_volume_by_instrument)[proposal.instrument_id]:
            raise RiskGuardError("calculated volume exceeds signed policy limit")
        volume = round(volume, max(contract.digits, 8))
        stop_risk = abs(_finite(profit_calculator(action, contract.broker_symbol, volume, proposal.entry_price, proposal.stop_price), "stop_risk"))
        risk_tolerance = max(1e-8, abs(risk_cash) * 1e-8)
        if stop_risk <= 0 or stop_risk > risk_cash + risk_tolerance:
            raise RiskGuardError("stop risk exceeds the current risk budget")
        if stop_risk + total_stop_risk > equity * self.max_total_stop_risk_percent / 100.0:
            raise RiskGuardError("aggregate stop risk exceeds existing limit")
        margin = _finite(self._calculator(snapshot, "order_calc_margin")(
            action, contract.broker_symbol, volume, proposal.entry_price
        ), "margin_required", nonnegative=True)
        if margin > margin_free * self.max_margin_fraction:
            raise RiskGuardError("margin requirement exceeds existing limit")
        return volume, contract, risk_cash, stop_risk, margin

    def _evaluate(self, proposal: SignalProposal, broker_snapshot: BrokerSnapshot | Mapping[str, Any] | Callable[[], BrokerSnapshot], *, approval_id: str | None, require_approval: bool) -> tuple[RiskDecision, tuple[float, InstrumentContract, float, float, float] | None]:
        now = self._now()
        try:
            snapshot = self._snapshot(broker_snapshot)
            values = self._facts(proposal, snapshot, now, require_approval=require_approval)
            if require_approval and (not isinstance(approval_id, str) or not approval_id.strip()):
                raise RiskGuardError("real operator approval id is required")
            return RiskDecision(True, "APPROVED", now, str(self._value(snapshot, "snapshot_hash")), values[3], values[4], values[2]), values
        except (RiskGuardError, TypeError, ValueError, OverflowError) as exc:
            return RiskDecision(False, str(exc), now), None

    def precheck(self, proposal: SignalProposal, broker_snapshot: BrokerSnapshot | Mapping[str, Any] | Callable[[], BrokerSnapshot]) -> RiskDecision:
        return self._evaluate(proposal, broker_snapshot, approval_id=None, require_approval=False)[0]

    def evaluate(self, proposal: SignalProposal, broker_snapshot: BrokerSnapshot | Mapping[str, Any] | Callable[[], BrokerSnapshot], *, approval_id: str | None = None) -> RiskDecision:
        return self._evaluate(proposal, broker_snapshot, approval_id=approval_id, require_approval=True)[0]

    def approve(self, proposal: SignalProposal, broker_snapshot: BrokerSnapshot | Mapping[str, Any] | Callable[[], BrokerSnapshot], *, approval_id: str) -> RiskApprovedOrder:
        snapshot = self._snapshot(broker_snapshot)
        decision, values = self._evaluate(proposal, snapshot, approval_id=approval_id, require_approval=True)
        if not decision.approved or values is None:
            raise RiskGuardError(decision.reason)
        volume, _contract, risk_cash, stop_risk, margin = values
        request = {
            "proposal": asdict(proposal), "volume": volume, "risk_cash": risk_cash,
            "stop_risk": stop_risk, "margin_required": margin,
            "snapshot_hash": decision.snapshot_hash, "approval_id": approval_id,
        }
        request["proposal"]["decision_time"] = proposal.decision_time.isoformat()
        request["proposal"]["expires_at"] = proposal.expires_at.isoformat()
        return RiskApprovedOrder(
            proposal=proposal, volume=volume, risk_cash=risk_cash, stop_risk=stop_risk,
            margin_required=margin, approved_at=decision.checked_at,
            snapshot_hash=decision.snapshot_hash, approval_id=approval_id,
            request_hash=_request_hash(request), persisted=False,
            policy_hash=str(self._value(snapshot, "policy_hash", "")),
        )

    def plan(self, proposal: SignalProposal, broker_snapshot: BrokerSnapshot | Mapping[str, Any] | Callable[[], BrokerSnapshot]) -> RiskApprovedOrder:
        """Build exact volume/risk facts before operator approval is issued."""
        snapshot = self._snapshot(broker_snapshot)
        decision, values = self._evaluate(proposal, snapshot, approval_id=None, require_approval=False)
        if not decision.approved or values is None:
            raise RiskGuardError(decision.reason)
        volume, _contract, risk_cash, stop_risk, margin = values
        request = {
            "proposal": asdict(proposal), "volume": volume, "risk_cash": risk_cash,
            "stop_risk": stop_risk, "margin_required": margin,
            "snapshot_hash": decision.snapshot_hash, "approval_id": "",
        }
        request["proposal"]["decision_time"] = proposal.decision_time.isoformat()
        request["proposal"]["expires_at"] = proposal.expires_at.isoformat()
        return RiskApprovedOrder(
            proposal=proposal, volume=volume, risk_cash=risk_cash, stop_risk=stop_risk,
            margin_required=margin, approved_at=decision.checked_at,
            snapshot_hash=decision.snapshot_hash, request_hash=_request_hash(request),
            policy_hash=str(self._value(snapshot, "policy_hash", "")),
        )

    final_check = evaluate
