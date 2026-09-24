"""Cost-inclusive, lifecycle-evidence-based risk diagnostics."""

from __future__ import annotations

import hashlib
import json
from math import sqrt
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from .numeric_contracts import FinancialMathError, finite_float, profit_factor


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _weighted_hhi(labels: Iterable[object], weights: Iterable[float]) -> float | None:
    pairs = [(str(label), abs(finite_float(weight, "concentration_weight"))) for label, weight in zip(labels, weights)]
    total = sum(weight for _, weight in pairs)
    if not pairs or total <= 0:
        return None
    buckets: dict[str, float] = {}
    for label, weight in pairs:
        buckets[label] = buckets.get(label, 0.0) + weight
    return sum((weight / total) ** 2 for weight in buckets.values())


def _series(fills: pd.DataFrame, eligible_dates: Iterable[object] | None, coverage: Mapping[str, object] | None = None) -> pd.Series:
    daily = fills.groupby("date", dropna=False)["r_multiple"].sum() if not fills.empty else pd.Series(dtype=float)
    if eligible_dates is None:
        return daily.astype(float)
    index = [str(value) for value in eligible_dates]
    if coverage is not None:
        evaluated = {str(value) for value in coverage.get("evaluated_dates", ())}
        classification = coverage.get("classification", {})
        if isinstance(classification, Mapping):
            index = [value for value in index if classification.get(value) == "VALID" and value in evaluated]
    existing = {str(index_value): float(value) for index_value, value in daily.items()}
    return pd.Series([existing.get(value, 0.0) for value in index], index=index, dtype=float)


def _input_hash(frame: pd.DataFrame) -> str:
    normalized = frame.copy()
    if normalized.empty:
        return hashlib.sha256(b"EMPTY").hexdigest()
    normalized = normalized.reindex(sorted(normalized.columns), axis=1).fillna("").astype(str)
    normalized = normalized.sort_values(list(normalized.columns), kind="mergesort").reset_index(drop=True)
    return hashlib.sha256(
        normalized.to_csv(index=False, lineterminator="\n").encode("utf-8")
    ).hexdigest()


def _stable_terminal_order(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.reset_index(drop=True)
    if "terminal_known_time" not in frame.columns:
        raise FinancialMathError("non-empty fills require terminal_known_time evidence")
    result = frame.copy()
    result["__terminal_order"] = pd.to_datetime(result["terminal_known_time"], errors="coerce", utc=True)
    result["__input_order"] = range(len(result))
    if result["__terminal_order"].isna().any():
        raise FinancialMathError("terminal_known_time is missing or invalid")
    return result.sort_values(["__terminal_order", "__input_order"], kind="mergesort").drop(columns=["__terminal_order", "__input_order"]).reset_index(drop=True)


def _drawdown(values: pd.Series) -> pd.Series:
    equity = pd.concat([pd.Series([0.0]), values.reset_index(drop=True)]).cumsum()
    return equity - equity.cummax()


def _drawdown_duration(values: pd.Series) -> int:
    dd = _drawdown(values)
    current = longest = 0
    for value in dd.iloc[1:]:
        if float(value) < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _recovery_index(values: pd.Series) -> int | None:
    equity = pd.concat([pd.Series([0.0]), values.reset_index(drop=True)]).cumsum()
    dd = equity - equity.cummax()
    trough = int(dd.idxmin())
    if float(dd.iloc[trough]) >= 0:
        return None
    prior_peak = float(equity.iloc[: trough + 1].cummax().iloc[-1])
    for index in range(trough + 1, len(equity)):
        if float(equity.iloc[index]) >= prior_peak:
            return index
    return None


def _simultaneous_risk(frame: pd.DataFrame) -> float | None:
    risk_column = next((column for column in ("open_risk", "exposure_r", "risk_at_entry") if column in frame.columns), None)
    if risk_column is None or frame.empty:
        return None
    if not {"entry_time", "exit_time"}.issubset(frame.columns):
        return None
    events: list[tuple[pd.Timestamp, int, float]] = []
    for _, row in frame.iterrows():
        risk = finite_float(row[risk_column], risk_column)
        if risk < 0:
            raise FinancialMathError(f"{risk_column} must be non-negative")
        start = pd.Timestamp(row["entry_time"])
        end = pd.Timestamp(row["exit_time"])
        if pd.isna(start) or pd.isna(end):
            return None
        if start.tzinfo is None:
            start = start.tz_localize("UTC")
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        if end < start:
            raise FinancialMathError("risk interval exit precedes entry")
        events.extend(((start, 1, risk), (end, -1, risk)))
    current = maximum = 0.0
    for _, kind, risk in sorted(events, key=lambda event: (event[0], event[1])):
        if kind == -1:
            current -= risk
        else:
            current += risk
            maximum = max(maximum, current)
    return maximum


def build_risk_xray(
    fills: pd.DataFrame,
    *,
    eligible_dates: Iterable[object] | None = None,
    funnel: Mapping[str, int | None] | None = None,
    coverage: Mapping[str, object] | None = None,
    provenance_hashes: Mapping[str, str] | None = None,
) -> dict[str, object]:
    if not isinstance(fills, pd.DataFrame):
        fills = pd.DataFrame(fills)
    if fills.empty:
        frame = pd.DataFrame(columns=["r_multiple", "date", "symbol", "direction"])
    else:
        if "r_multiple" not in fills.columns:
            raise FinancialMathError("fill frame missing: r_multiple")
        frame = _stable_terminal_order(fills.copy())
        if "result" in frame.columns:
            closed_results = {"win", "loss", "loss_same_bar", "breakeven", "reduced_loss"}
            if (~frame["result"].isin(closed_results)).any():
                raise FinancialMathError("risk x-ray accepts closed terminal fills only")
        frame["r_multiple"] = [finite_float(value, "r_multiple") for value in frame["r_multiple"]]
        for column, default in (("date", ""), ("symbol", ""), ("direction", "")):
            if column not in frame.columns:
                frame[column] = default
    values = frame["r_multiple"].astype(float)
    gross_profit = float(values[values > 0].sum()) if len(values) else 0.0
    gross_loss = float(abs(values[values < 0].sum())) if len(values) else 0.0
    daily = _series(frame, eligible_dates, coverage)
    daily_std = float(daily.std(ddof=1)) if len(daily) > 1 else None
    downside = daily[daily < 0]
    downside_std = float((downside.pow(2).mean()) ** 0.5) if len(downside) else None
    daily_mean = float(daily.mean()) if len(daily) else None
    sorted_daily = daily.sort_values().to_numpy()
    cvar = float(sorted_daily[: max(1, int(len(sorted_daily) * 0.05))].mean()) if len(sorted_daily) >= 2 else None
    no_trades = not len(values)
    risk_column = next((column for column in ("open_risk", "exposure_r", "risk_at_entry") if column in frame.columns), None)
    risk_weights = frame[risk_column] if risk_column is not None else []
    pnl_weights = frame["r_multiple"].abs() if len(frame) else []
    risk_hhi = {"symbol": _weighted_hhi(frame["symbol"], risk_weights), "direction": _weighted_hhi(frame["direction"], risk_weights)} if len(frame) else {"symbol": None, "direction": None}
    pnl_hhi = {"symbol": _weighted_hhi(frame["symbol"], pnl_weights), "direction": _weighted_hhi(frame["direction"], pnl_weights)} if len(frame) else {"symbol": None, "direction": None}
    daily_reason = "NO_ELIGIBLE_DAYS" if not len(daily) else ("INSUFFICIENT_DAILY_SAMPLE" if len(daily) < 2 else ("ZERO_VARIANCE" if daily_std == 0 else None))
    sortino_reason = (
        "NO_ELIGIBLE_DAYS" if daily_mean is None else
        "NO_DOWNSIDE_DAYS" if not len(downside) else
        "ZERO_DOWNSIDE_VARIANCE" if downside_std == 0 else None
    )
    cvar_reason = "NO_ELIGIBLE_DAYS" if not len(daily) else ("INSUFFICIENT_DAILY_SAMPLE" if len(daily) < 2 else None)
    pf, pf_reason = profit_factor(gross_profit, gross_loss)
    simultaneous_open_risk = _simultaneous_risk(frame)
    hashes = dict(provenance_hashes or {})
    provenance_complete = all(_is_sha256(hashes.get(key)) for key in ("input_hash", "code_hash", "coverage_hash"))
    funnel_value = dict(funnel) if funnel is not None else {"proposed": None, "risk_approved": None, "staged": None, "filled": None, "rejected": None, "expired": None}
    return {
        "fill_count": int(len(values)),
        "gross_profit_r": gross_profit,
        "gross_loss_r": gross_loss,
        "gross_r": gross_profit - gross_loss,
        "net_r": float(values.sum()) if len(values) else 0.0,
        "expectancy": None if no_trades else float(values.mean()),
        "expectancy_reason": "NO_CLOSED_TRADES" if no_trades else None,
        "profit_factor": pf,
        "profit_factor_reason": "NO_CLOSED_TRADES" if no_trades else pf_reason,
        "max_drawdown_r": None if no_trades else float(_drawdown(values).min()),
        "max_drawdown_r_reason": "NO_CLOSED_TRADES" if no_trades else None,
        "drawdown_duration": None if no_trades else _drawdown_duration(values),
        "drawdown_duration_reason": "NO_CLOSED_TRADES" if no_trades else None,
        "drawdown_recovery": None if no_trades else _recovery_index(values),
        "drawdown_recovery_reason": "NO_CLOSED_TRADES" if no_trades else ("NO_DRAWDOWN" if not (_drawdown(values) < 0).any() else ("NO_RECOVERY" if _recovery_index(values) is None else None)),
        "daily_net_r_volatility": None if daily_std is None else daily_std * sqrt(252),
        "daily_net_r_volatility_reason": daily_reason,
        "daily_r_sharpe_rf0": None if daily_mean is None or not daily_std else daily_mean / daily_std * sqrt(252),
        "daily_r_sharpe_rf0_reason": daily_reason if daily_mean is None or not daily_std else None,
        "sortino": None if daily_mean is None or not downside_std else daily_mean / downside_std * sqrt(252),
        "sortino_reason": sortino_reason,
        "cvar_95": cvar,
        "cvar_95_reason": cvar_reason,
        "risk_concentration": {"risk_weighted_hhi": risk_hhi, "pnl_weighted_hhi": pnl_hhi, "risk_hhi_reason": None if risk_column is not None else "NO_RISK_CONTRIBUTION_EVIDENCE"},
        "simultaneous_open_risk": simultaneous_open_risk,
        "simultaneous_open_risk_reason": None if simultaneous_open_risk is not None else "NO_OPEN_INTERVAL_EVIDENCE",
        "funnel": funnel_value,
        "funnel_evidence_reason": None if funnel is not None else "NO_LIFECYCLE_EVIDENCE_FOR_UNFILLED_STAGES",
        "coverage": None if coverage is None else dict(coverage),
        "provenance_hashes": hashes | {"input_hash": hashes.get("input_hash") or _input_hash(frame)},
        "provenance_complete": provenance_complete,
        "provenance_reason": None if provenance_complete else "MISSING_INPUT_CODE_OR_COVERAGE_HASH",
    }


def write_risk_xray(xray: Mapping[str, object], output_dir: str | Path, *, stem: str = "risk_xray") -> tuple[Path, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(xray), sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    json_path, md_path = root / f"{stem}.json", root / f"{stem}.md"
    json_path.write_text(payload, encoding="utf-8")
    lines = ["# Risk x-ray", ""]
    for key, value in xray.items():
        lines.append(f"- **{key}**: `{json.dumps(value, ensure_ascii=False, allow_nan=False)}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
