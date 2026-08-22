from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import SymbolConfig
from .data_inspector import parse_timeframe_minutes
from .integrity import structural_ohlcv_issues
from .manual_state import (
    ManualStateBacktestResult,
    ManualStateConfig,
    decisions_to_frame,
    run_manual_state_backtest,
)
from .risk import apply_pair_risk_rule


@dataclass(frozen=True)
class EngineLeg:
    key: str
    frame: pd.DataFrame
    config: SymbolConfig


@dataclass(frozen=True)
class CanonicalEngineResult:
    leg_results: dict[str, ManualStateBacktestResult]
    decisions: pd.DataFrame
    filled_after_pair_cap: pd.DataFrame
    suppressed_by_pair_cap: pd.DataFrame
    manifest: dict[str, object]


def validate_leg_contract(leg: EngineLeg) -> None:
    if leg.frame.empty:
        raise ValueError(f"{leg.key}: empty market data.")
    required = ["time", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in leg.frame.columns]
    if missing:
        raise ValueError(f"{leg.key}: market data is missing columns: {', '.join(missing)}.")
    normalized = leg.frame[required].copy()
    normalized["time"] = pd.to_datetime(normalized["time"], errors="coerce", utc=True, format="mixed")
    for column in ["open", "high", "low", "close", "volume"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    structural = structural_ohlcv_issues(normalized)
    if structural:
        codes = ", ".join(dict.fromkeys(issue.code for issue in structural))
        raise ValueError(f"{leg.key}: invalid market data ({codes}).")
    timestamps = normalized["time"]
    expected = parse_timeframe_minutes(leg.config.timeframe)
    diffs = timestamps.sort_values().diff().dt.total_seconds().div(60)
    intraday = diffs[(diffs > 0) & (diffs <= (expected or 5.0) * 3)]
    if expected is not None and not intraday.empty:
        observed = float(intraday.mode().iloc[0])
        if abs(observed - expected) > 1e-9:
            raise ValueError(
                f"{leg.key}: configured timeframe {leg.config.timeframe} "
                f"does not match observed {observed:g}m cadence."
            )


def feed_name(symbol: str) -> str:
    return symbol.split("_", 1)[0] if "_" in symbol else "UNSPECIFIED"


def run_canonical_pair_pipeline(
    legs: Iterable[EngineLeg],
    dates: Iterable[date],
    *,
    pair_cap_r: float = -1.0,
    state_config: ManualStateConfig | None = None,
    require_same_feed: bool = True,
) -> CanonicalEngineResult:
    leg_list = list(legs)
    date_list = sorted(set(dates))
    if not leg_list:
        raise ValueError("At least one engine leg is required.")
    leg_keys = [leg.key for leg in leg_list]
    if any(not key.strip() for key in leg_keys):
        raise ValueError("Engine leg keys must be non-empty.")
    duplicate_leg_keys = sorted({key for key in leg_keys if leg_keys.count(key) > 1})
    if duplicate_leg_keys:
        raise ValueError(f"Engine leg keys must be unique: {duplicate_leg_keys}")
    for leg in leg_list:
        validate_leg_contract(leg)
    feeds = {feed_name(leg.config.symbol) for leg in leg_list}
    if require_same_feed and len(feeds) != 1:
        raise ValueError(f"Mixed feeds are not allowed in one canonical pair run: {sorted(feeds)}")

    effective_state_config = state_config or ManualStateConfig()
    leg_results: dict[str, ManualStateBacktestResult] = {}
    decision_frames: list[pd.DataFrame] = []
    configs: dict[str, SymbolConfig] = {}
    for leg in leg_list:
        result = run_manual_state_backtest(leg.frame, leg.config, date_list, effective_state_config)
        leg_results[leg.key] = result
        configs[leg.key] = leg.config
        frame = decisions_to_frame(result.days)
        if not frame.empty:
            frame.insert(0, "leg_key", leg.key)
            decision_frames.append(frame)
    decisions = pd.concat(decision_frames, ignore_index=True) if decision_frames else pd.DataFrame()
    if decisions.empty:
        return CanonicalEngineResult(
            leg_results,
            decisions,
            decisions.copy(),
            decisions.copy(),
            build_run_manifest(leg_list, effective_state_config, decisions),
        )
    duplicate_orders = decisions.duplicated(["leg_key", "order_id"], keep=False)
    if duplicate_orders.any():
        sample = decisions.loc[duplicate_orders, ["leg_key", "order_id"]].head(3).to_dict("records")
        raise ValueError(f"Duplicate (leg_key, order_id) decisions: {sample}")

    decisions["pair_risk_state"] = "NOT_APPLICABLE"
    filled = decisions[decisions["order_state"] == "FILLED"].copy()
    if not filled.empty:
        filled["group"] = "NQ_SPX_PAIR"
        filled["label"] = filled["leg_key"]
        exact_r = pd.to_numeric(filled["r_multiple"], errors="coerce")
        closed = filled["outcome"].isin(["TP", "SL", "BE"])
        if exact_r[closed].isna().any():
            bad = filled.loc[closed & exact_r.isna(), "order_id"].head(3).tolist()
            raise ValueError(f"Filled closed decisions are missing exact R multiples: {bad}")
        filled["mark_to_market_r_multiple"] = exact_r
        filled["r_multiple"] = exact_r.where(closed)
        risk_input = filled.copy()
        risk_input["_realized_r_multiple"] = filled["r_multiple"].fillna(0.0)
        allowed = apply_pair_risk_rule(
            risk_input,
            pair_cap_r,
            terminal_time_col="terminal_known_time",
            r_col="_realized_r_multiple",
        )
        allowed = allowed.drop(columns="_realized_r_multiple")
        allowed_keys = set(zip(allowed["leg_key"], allowed["order_id"]))
        filled_keys = pd.Series(list(zip(filled["leg_key"], filled["order_id"])), index=filled.index)
        suppressed = filled[~filled_keys.isin(allowed_keys)].copy()
        decision_keys = pd.Series(
            list(zip(decisions["leg_key"], decisions["order_id"])), index=decisions.index
        )
        suppressed_keys = set(zip(suppressed["leg_key"], suppressed["order_id"]))
        decisions.loc[decision_keys.isin(allowed_keys), "pair_risk_state"] = "ALLOWED"
        decisions.loc[decision_keys.isin(suppressed_keys), "pair_risk_state"] = (
            "SUPPRESSED_DAILY_CAP"
        )
    else:
        allowed = filled
        suppressed = filled

    manifest = build_run_manifest(leg_list, effective_state_config, decisions)
    return CanonicalEngineResult(leg_results, decisions, allowed, suppressed, manifest)


def stable_frame_hash(frame: pd.DataFrame) -> str:
    if frame.empty:
        return sha256(b"EMPTY").hexdigest()
    normalized = frame.copy()
    normalized = normalized.reindex(sorted(normalized.columns), axis=1)
    normalized = normalized.fillna("").astype(str)
    normalized = normalized.sort_values(list(normalized.columns), kind="mergesort").reset_index(drop=True)
    payload = normalized.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return sha256(payload).hexdigest()


def source_code_hash() -> str:
    root = Path(__file__).resolve().parent
    digest = sha256()
    for path in sorted(root.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build_run_manifest(
    legs: list[EngineLeg],
    state_config: ManualStateConfig,
    decisions: pd.DataFrame,
) -> dict[str, object]:
    data_hashes = {
        leg.key: stable_frame_hash(
            leg.frame[[column for column in ["time", "open", "high", "low", "close", "volume"] if column in leg.frame]]
        )
        for leg in legs
    }
    config_payload = {
        "state": asdict(state_config),
        "legs": {leg.key: asdict(leg.config) for leg in legs},
    }
    config_json = json.dumps(config_payload, sort_keys=True, default=str)
    return {
        "engine": "manual_state_canonical_v1",
        "timezone": "America/New_York",
        "feeds": sorted({feed_name(leg.config.symbol) for leg in legs}),
        "data_hashes": data_hashes,
        "config_hash": sha256(config_json.encode("utf-8")).hexdigest(),
        "code_hash": source_code_hash(),
        "result_hash": stable_frame_hash(decisions),
    }
