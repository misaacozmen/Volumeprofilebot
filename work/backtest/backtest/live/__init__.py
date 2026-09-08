"""Fail-closed live architecture primitives.

The package is intentionally small and dependency-free.  Strategy code may
produce :class:`SignalProposal`; only the risk boundary may create a
:class:`RiskApprovedOrder`.
"""

from .contracts import (
    BrokerSnapshot,
    BrokerEvidence,
    ExecutionAdapter,
    InstrumentContract,
    RiskApprovedOrder,
    RiskDecision,
    RiskSnapshot,
)
from .order_state import OrderState
from .risk_guard import RiskGuard
from .production_flow import ProductionDependencies, ProductionOrderFlow
from .strategy_health import BrokerHealthMetrics, StrategyHealth

__all__ = [
    "BrokerSnapshot",
    "BrokerEvidence",
    "ExecutionAdapter",
    "InstrumentContract",
    "RiskApprovedOrder",
    "RiskDecision",
    "RiskSnapshot",
    "RiskGuard",
    "OrderState",
    "ProductionDependencies",
    "ProductionOrderFlow",
    "BrokerHealthMetrics",
    "StrategyHealth",
]
