"""Single source of truth for financial numeric validation."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN
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


def _decimal(value: Any, name: str) -> Decimal:
    finite_float(value, name)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FinancialMathError(f"{name} is not a finite decimal") from exc


def quantize_price(value: Any, tick_size: Any) -> float:
    price, tick = _decimal(value, "price"), _decimal(tick_size, "tick_size")
    if price <= 0 or tick <= 0:
        raise FinancialMathError("price and tick_size must be positive")
    return float((price / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick)


def quantize_volume_down(value: Any, step: Any, minimum: Any, maximum: Any) -> float:
    volume, quantum = _decimal(value, "volume"), _decimal(step, "volume_step")
    lower, upper = _decimal(minimum, "volume_min"), _decimal(maximum, "volume_max")
    if quantum <= 0 or lower <= 0 or upper < lower or volume <= 0:
        raise FinancialMathError("volume contract is invalid")
    rounded = (volume / quantum).to_integral_value(rounding=ROUND_DOWN) * quantum
    if rounded < lower or rounded > upper:
        raise FinancialMathError("quantized volume is outside broker limits")
    return float(rounded)


def safe_divide(numerator: Any, denominator: Any, *, reason: str) -> tuple[float | None, str | None]:
    top, bottom = finite_float(numerator, "numerator"), finite_float(denominator, "denominator")
    if bottom == 0:
        return None, reason
    result = top / bottom
    if not math.isfinite(result):
        raise FinancialMathError("division result is non-finite")
    return result, None


def finite_vector(values: Any, name: str = "values") -> list[float]:
    if isinstance(values, (str, bytes, bool)) or values is None:
        raise FinancialMathError(f"{name} must be a numeric vector")
    try:
        return [finite_float(value, f"{name}[{index}]") for index, value in enumerate(values)]
    except TypeError as exc:
        raise FinancialMathError(f"{name} must be a numeric vector") from exc
