"""Canonical, account-wide broker deal ingestion for live risk facts."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from numbers import Real
from typing import Any, Callable, Iterable, Mapping


class DealIngestionError(RuntimeError):
    """Raised when broker history cannot be reconciled into risk facts."""


def deal_id(deal: Mapping[str, Any]) -> str:
    value = deal.get("deal_id")
    if value in (None, ""):
        value = deal.get("ticket")
    identity = str(value or "").strip()
    if not identity:
        raise DealIngestionError("broker deal has no stable deal ID")
    return identity


def deal_timestamp(deal: Mapping[str, Any], *, required: bool = True) -> datetime | None:
    field = "time_msc" if deal.get("time_msc") is not None else "time"
    value = deal.get(field)
    if value is None:
        if required:
            raise DealIngestionError(f"deal {deal_id(deal)} has no broker timestamp")
        return None
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise DealIngestionError(f"deal {deal_id(deal)} timestamp is invalid")
    stamp = float(value) / (1000.0 if field == "time_msc" else 1.0)
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise DealIngestionError(f"deal {deal_id(deal)} timestamp is outside UTC range") from exc


def daily_deals(
    deals: Iterable[Mapping[str, Any]],
    *,
    day_start: datetime,
    day_end: datetime,
) -> tuple[dict[str, Any], ...]:
    """Return the exact account-day slice; no campaign/magic filter is allowed."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in deals:
        deal = dict(source)
        identity = deal_id(deal)
        if identity in seen:
            raise DealIngestionError(f"duplicate broker deal {identity}")
        seen.add(identity)
        timestamp = deal_timestamp(deal)
        if day_start <= timestamp < day_end:
            result.append(deal)
    return tuple(result)


def realized_r(
    deals: Iterable[Mapping[str, Any]],
    *,
    exit_values: set[Any],
    inout_values: set[Any],
    out_by_values: set[Any],
    known_values: set[Any],
    starting_risk_reader: Callable[[str], float | None],
) -> float:
    """Calculate net realized R once per account deal, including all costs."""
    total = 0.0
    for deal in deals:
        identity = deal_id(deal)
        entry = deal.get("entry")
        if entry not in known_values:
            raise DealIngestionError(f"broker deal {identity} has an unknown entry type")
        if entry not in exit_values | inout_values | out_by_values:
            continue
        position_id = str(deal.get("position_id") or "").strip()
        if not position_id:
            raise DealIngestionError(f"closing broker deal {identity} has no position ID")
        risk = starting_risk_reader(position_id)
        if isinstance(risk, bool) or not isinstance(risk, Real) or not math.isfinite(float(risk)) or float(risk) <= 0:
            raise DealIngestionError(f"broker deal {identity} cannot be converted to R")
        costs: list[float] = []
        for field in ("profit", "commission", "swap", "fee"):
            value = deal.get(field)
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                raise DealIngestionError(f"broker deal {identity} cannot be converted to R: {field}")
            costs.append(float(value))
        total += sum(costs) / float(risk)
    return float(total)


def last_account_entry_time(
    deals: Iterable[Mapping[str, Any]],
    *,
    in_values: set[Any],
    inout_values: set[Any],
    exit_values: set[Any],
    out_by_values: set[Any],
) -> datetime | None:
    last_entry: datetime | None = None
    for source in deals:
        deal = dict(source)
        entry = deal.get("entry")
        if entry not in in_values | inout_values | exit_values | out_by_values:
            raise DealIngestionError(f"broker deal {deal_id(deal)} has an unknown entry type")
        timestamp = deal_timestamp(deal)
        assert timestamp is not None
        if entry in in_values | inout_values and (last_entry is None or timestamp > last_entry):
            last_entry = timestamp
    return last_entry
