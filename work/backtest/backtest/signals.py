"""Pure strategy proposals.

This module deliberately contains no broker, account, credential, or runtime
configuration dependency.  A proposal describes *what* a strategy saw; risk
and execution decide whether and how it may be traded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .numeric_contracts import validate_trade_geometry


@dataclass(frozen=True, slots=True)
class SignalProposal:
    """Broker-independent output of the strategy layer."""

    proposal_id: str
    candidate_hash: str
    instrument_id: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    decision_time: datetime
    expires_at: datetime
    evidence_hash: str
    broker_symbol: str = ""
    instrument_registry_sha256: str = ""

    def __post_init__(self) -> None:
        for name in ("proposal_id", "candidate_hash", "instrument_id", "evidence_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("broker_symbol", "instrument_registry_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
        if self.direction not in {"long", "short"}:
            raise ValueError("direction must be 'long' or 'short'")
        validate_trade_geometry(self.direction, self.entry_price, self.stop_price, self.target_price)
        if not isinstance(self.decision_time, datetime) or not isinstance(self.expires_at, datetime):
            raise TypeError("decision_time and expires_at must be datetime values")
        if self.expires_at <= self.decision_time:
            raise ValueError("proposal expiry must be after decision time")

    # Short names are convenient at the strategy boundary without introducing
    # risk-related fields into the proposal schema.
    @property
    def entry(self) -> float:
        return self.entry_price

    @property
    def stop(self) -> float:
        return self.stop_price

    @property
    def target(self) -> float:
        return self.target_price
