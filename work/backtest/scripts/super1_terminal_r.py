"""Pure, broker-side terminal-R calculation shared by runtime and validator."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from backtest.numeric_contracts import finite_float


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def calculate_terminal_r(
    deals: Sequence[Any],
    positions: Sequence[Any],
    *,
    magic: int,
    reward_by_symbol: Mapping[str, float],
    lookback: int,
    entry_out: int = 1,
    reason_sl: int = 4,
    reason_tp: int = 5,
) -> list[dict[str, Any]]:
    """Return broker-derived terminal outcomes ordered by (time_msc, ticket).

    ``raw_r`` is intentionally never read.  Duplicate terminal deals for one
    position and ambiguous reasons are hard failures rather than guesses.
    """
    if isinstance(lookback, bool) or int(lookback) <= 0:
        raise ValueError("lookback must be positive")
    open_positions = {
        int(_value(row, "ticket") or _value(row, "identifier"))
        for row in positions
        if (_value(row, "ticket") or _value(row, "identifier")) is not None
        and int(_value(row, "magic", -1)) == int(magic)
    }
    by_position: dict[int, dict[str, Any]] = {}
    seen_deal_ids: set[int] = set()
    for deal in deals:
        if int(_value(deal, "magic", -1)) != int(magic):
            continue
        if int(_value(deal, "entry", -1)) != int(entry_out):
            continue
        position_id = int(_value(deal, "position_id", 0) or 0)
        if not position_id or position_id in open_positions:
            continue
        deal_id = int(_value(deal, "deal_id", _value(deal, "ticket", 0)) or 0)
        if deal_id <= 0 or deal_id in seen_deal_ids:
            raise ValueError("terminal broker deal IDs must be present and unique")
        seen_deal_ids.add(deal_id)
        reason = int(_value(deal, "reason", -1))
        symbol = str(_value(deal, "symbol", ""))
        if reason == int(reason_sl):
            r_value = -1.0
        elif reason == int(reason_tp) and symbol in reward_by_symbol:
            r_value = finite_float(reward_by_symbol[symbol], "reward_r")
            if r_value <= 0:
                raise ValueError("reward_r must be positive")
        else:
            raise ValueError(f"ambiguous terminal reason or unknown symbol: {reason}/{symbol}")
        if position_id in by_position:
            raise ValueError(f"multiple terminal deals for position {position_id}")
        by_position[position_id] = {
            "position_id": position_id,
            "time_msc": int(_value(deal, "time_msc", 0)),
            "ticket": int(_value(deal, "ticket", 0)),
            "deal_id": deal_id,
            "symbol": symbol,
            "reason": reason,
            "r": r_value,
        }
    ordered = sorted(by_position.values(), key=lambda row: (row["time_msc"], row["ticket"]))
    # Keep the complete terminal series; the caller applies the configured
    # lookback when computing the current risk state.
    return ordered
