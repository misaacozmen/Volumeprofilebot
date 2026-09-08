"""Single source of truth for financial numeric validation."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any


class FinancialMathError(ValueError):
    pass


def finite_float(value: Any, name: str = "value") -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise FinancialMathError(f"{name} must be a finite float")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FinancialMathError(f"{name} must be a finite float") from exc
    if not math.isfinite(result):
        raise FinancialMathError(f"{name} must be a finite float")
    return result


def positive_float(value: Any, name: str = "value") -> float:
    result = finite_float(value, name)
    if result <= 0:
        raise FinancialMathError(f"{name} must be positive")
    return result


def nonnegative_float(value: Any, name: str = "value") -> float:
    result = finite_float(value, name)
    if result < 0:
        raise FinancialMathError(f"{name} must be non-negative")
    return result


def strict_ratio(value: Any, name: str = "ratio") -> float:
    result = finite_float(value, name)
    if result <= 0 or result > 1:
        raise FinancialMathError(f"{name} must be in (0, 1]")
    return result


def nullable_ratio(value: Any | None, name: str = "ratio") -> float | None:
    return None if value is None else strict_ratio(value, name)


def validate_trade_geometry(direction: str, entry: Any, stop: Any, target: Any) -> tuple[float, float, float]:
    entry_value = positive_float(entry, "entry_price")
    stop_value = positive_float(stop, "stop_price")
    target_value = positive_float(target, "target_price")
    if direction == "long" and not stop_value < entry_value < target_value:
        raise FinancialMathError("long trade geometry must satisfy stop < entry < target")
    if direction == "short" and not target_value < entry_value < stop_value:
        raise FinancialMathError("short trade geometry must satisfy target < entry < stop")
    if direction not in {"long", "short"}:
        raise FinancialMathError("trade direction is invalid")
    return entry_value, stop_value, target_value
