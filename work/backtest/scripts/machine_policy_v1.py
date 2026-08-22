from __future__ import annotations

from typing import Any

import pandas as pd

from backtest.risk import apply_pair_risk_rule


def blocked_pairs(config: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(leg_key), str(context_source))
        for leg_key, sources in config["blocked_context_sources"].items()
        for context_source in sources
    }


def blocked_values(config: dict[str, Any], key: str) -> set[tuple[str, str]]:
    return {
        (str(leg_key), str(value))
        for leg_key, values in config.get(key, {}).items()
        for value in values
    }


def decision_allowed(decision: dict[str, Any] | pd.Series, config: dict[str, Any]) -> bool:
    leg_key = str(decision["leg_key"])
    if (leg_key, str(decision["context_source"])) in blocked_pairs(config):
        return False
    if (leg_key, str(decision.get("direction", ""))) in blocked_values(config, "blocked_directions"):
        return False
    date_value = decision.get("date")
    if date_value is None:
        return True
    weekday = pd.Timestamp(date_value).day_name()
    return (leg_key, weekday) not in blocked_values(config, "blocked_weekdays")


def filtered_decisions(decisions: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    if decisions.empty:
        return decisions.copy()
    allowed = decisions.apply(lambda row: decision_allowed(row, config), axis=1)
    return decisions.loc[allowed].copy()


def causal_fills_after_pair_cap(decisions: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    eligible = filtered_decisions(decisions, config)
    filled = eligible[eligible["order_state"] == "FILLED"].copy()
    if filled.empty:
        return filled
    filled["group"] = "NQ_SPX_PAIR"
    filled["label"] = filled["leg_key"]
    return apply_exact_pair_cap(
        filled,
        float(config["pair_cap_r"]),
    )


def apply_exact_pair_cap(filled: pd.DataFrame, cap_r: float = -1.0) -> pd.DataFrame:
    """Apply a causal cap without replacing exact/partial engine R with nominal TP/SL R."""
    if filled.empty:
        return filled.copy()
    result = filled.copy()
    exact = pd.to_numeric(result["r_multiple"], errors="coerce")
    closed = result["outcome"].isin(["TP", "SL", "BE"])
    if exact[closed].isna().any():
        raise ValueError("Closed machine-policy decisions must carry exact R multiples.")
    result["mark_to_market_r_multiple"] = exact
    result["r_multiple"] = exact.where(closed)
    result["_realized_r_multiple"] = result["r_multiple"].fillna(0.0)
    capped = apply_pair_risk_rule(
        result,
        cap_r,
        terminal_time_col="terminal_known_time",
        r_col="_realized_r_multiple",
    )
    return capped.drop(columns="_realized_r_multiple")


def causal_policy_violations(decisions: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    blocked = decisions[
        ~decisions.apply(lambda row: decision_allowed(row, config), axis=1)
        & decisions["order_state"].eq("FILLED")
    ].copy()
    if blocked.empty:
        return blocked
    event_known = pd.to_datetime(blocked["event_known_time"], utc=True, errors="coerce")
    entry_known = pd.to_datetime(blocked["entry_known_time"], utc=True, errors="coerce")
    return blocked[event_known.isna() | entry_known.isna() | (event_known > entry_known)].copy()
