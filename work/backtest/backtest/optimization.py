"""Deterministic, allowlisted research-only parameter studies."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from itertools import product
import json
import math
from pathlib import Path
import random
import platform
from typing import Any, Callable, Mapping

import pandas as pd


class StudyConfigError(ValueError):
    pass


ALLOWED_PARAMETERS = frozenset({
    "reward_r", "min_fvg_points", "max_trades_per_day", "fvg_entry_mode",
    "stop_model", "stop_management", "direction_filter", "setup_type_filter",
})

PARAMETER_RULES: dict[str, tuple[type, object]] = {
    "reward_r": (float, lambda value: math.isfinite(float(value)) and float(value) > 0),
    "min_fvg_points": (float, lambda value: math.isfinite(float(value)) and float(value) >= 0),
    "max_trades_per_day": (int, lambda value: value >= 1),
    "fvg_entry_mode": (str, lambda value: value in {"midpoint", "start", "quarter_25", "cisd_close", "body_end", "ote_62", "ote_705", "ote_79"}),
    "stop_model": (str, lambda value: value in {"sweep_wick", "cisd_body", "fvg_opposite_edge", "swing_based"}),
    "stop_management": (str, lambda value: value in {"none", "be_at_half_target", "half_stop_at_half_target"}),
    "direction_filter": (str, lambda value: value in {"all", "long", "short"}),
    "setup_type_filter": (str, lambda value: value in {"all", "fvg", "ifvg", "body_fvg", "pd_array"}),
}


def canonical_hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StudyConfig:
    seed: int
    symbol: str
    timeframe: str
    search_space: tuple[tuple[str, tuple[object, ...]], ...]
    train_sessions: int
    oos_sessions: int
    step_sessions: int
    anchored: bool
    max_evals: int
    minimum_closed_trades: int
    objective_metric: str

    @classmethod
    def load(cls, path: str | Path) -> "StudyConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StudyConfigError("study config is unreadable") from exc
        required = {"seed", "symbol", "timeframe", "search_space", "train_sessions", "oos_sessions", "step_sessions", "anchored", "max_evals", "minimum_closed_trades", "objective_metric"}
        if not isinstance(raw, dict) or set(raw) != required or not isinstance(raw["search_space"], dict):
            raise StudyConfigError("study config fields differ from the closed schema")
        if not isinstance(raw["symbol"], str) or not raw["symbol"].strip() or raw["timeframe"] not in {"3m", "5m"}:
            raise StudyConfigError("study symbol/timeframe is invalid")
        if not raw["search_space"]:
            raise StudyConfigError("study search space must not be empty")
        space: list[tuple[str, tuple[object, ...]]] = []
        for name, values in sorted(raw["search_space"].items()):
            if name not in ALLOWED_PARAMETERS or not isinstance(values, list) or not values:
                raise StudyConfigError("search space contains an unknown or unbounded parameter")
            expected_type, predicate = PARAMETER_RULES[name]
            for value in values:
                if isinstance(value, bool) or not isinstance(value, expected_type):
                    raise StudyConfigError(f"search space value has the wrong type: {name}")
                try:
                    valid_value = bool(predicate(value))
                except (TypeError, ValueError, OverflowError):
                    valid_value = False
                if not valid_value:
                    raise StudyConfigError(f"search space value is outside the signed domain: {name}")
            try:
                canonical = [json.dumps(value, sort_keys=True, allow_nan=False) for value in values]
            except (TypeError, ValueError) as exc:
                raise StudyConfigError("search space contains non-finite or non-JSON values") from exc
            if len(canonical) != len(set(canonical)):
                raise StudyConfigError("search space contains duplicate values")
            if any(isinstance(value, float) and not math.isfinite(value) for value in values):
                raise StudyConfigError("search space contains non-finite values")
            space.append((name, tuple(values)))
        integers = [raw[key] for key in ("seed", "train_sessions", "oos_sessions", "step_sessions", "max_evals", "minimum_closed_trades")]
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in integers):
            raise StudyConfigError("study integer limits must be positive")
        if raw["max_evals"] > 1000 or math.prod(len(values) for _, values in space) > raw["max_evals"]:
            raise StudyConfigError("search space exceeds max_evals")
        if raw["step_sessions"] < raw["oos_sessions"]:
            raise StudyConfigError("OOS folds must not overlap")
        if raw["objective_metric"] != "daily_r_sharpe_rf0" or not isinstance(raw["anchored"], bool):
            raise StudyConfigError("study objective or anchored flag is invalid")
        return cls(raw["seed"], str(raw["symbol"]), str(raw["timeframe"]), tuple(space), raw["train_sessions"], raw["oos_sessions"], raw["step_sessions"], raw["anchored"], raw["max_evals"], raw["minimum_closed_trades"], raw["objective_metric"])

    def parameter_sets(self) -> list[dict[str, object]]:
        names = [name for name, _ in self.search_space]
        rows = [dict(zip(names, values)) for values in product(*(values for _, values in self.search_space))]
        random.Random(self.seed).shuffle(rows)
        return rows[: self.max_evals]


def optimize(config: StudyConfig, evaluator: Callable[[Mapping[str, object]], Mapping[str, object]], output_dir: str | Path, *, data_hash: str | None = None, code_hash: str | None = None) -> dict[str, object]:
    trials: list[dict[str, object]] = []
    for parameters in config.parameter_sets():
        try:
            metrics = dict(evaluator(parameters))
            closed_value = metrics.get("fill_count") or 0
            if isinstance(closed_value, bool):
                raise ValueError("fill_count must be an integer")
            closed = int(closed_value)
            if closed < 0 or closed != closed_value:
                raise ValueError("fill_count must be a non-negative integer")
            for value in metrics.values():
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                    raise ValueError("evaluator returned a non-finite metric")
        except (TypeError, ValueError, OverflowError) as exc:
            raise StudyConfigError("evaluator returned invalid metrics") from exc
        objective = metrics.get(config.objective_metric)
        valid = closed >= config.minimum_closed_trades and isinstance(objective, (int, float)) and not isinstance(objective, bool) and math.isfinite(float(objective))
        trials.append({"parameter_hash": canonical_hash(parameters), "parameters": dict(parameters), "valid": valid, **metrics})
    valid_trials = [row for row in trials if row["valid"]]
    def metric(row: Mapping[str, object], name: str, default: float) -> float:
        value = row.get(name)
        if value is None or isinstance(value, bool):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return number if math.isfinite(number) else default

    winner = None if not valid_trials else sorted(
        valid_trials,
        key=lambda row: (
            -metric(row, config.objective_metric, -math.inf),
            -metric(row, "expectancy", -math.inf),
            abs(metric(row, "max_drawdown_r", math.inf)),
            str(row["parameter_hash"]),
        ),
    )[0]
    root = Path(output_dir); root.mkdir(parents=True, exist_ok=True)
    pd.json_normalize(trials).to_csv(root / "trials.csv", index=False)
    study_value = config.__dict__ if hasattr(config, "__dict__") else {name: getattr(config, name) for name in config.__slots__}
    manifest = {
        "status": "RESEARCH_ONLY_NON_PROMOTABLE" if winner is not None else "NO_VALID_TRIALS_NON_PROMOTABLE",
        "selection_status": "SELECTED" if winner is not None else "BLOCKED_NO_VALID_TRIAL",
        "study": study_value,
        "study_config_hash": canonical_hash(study_value), "data_hash": data_hash,
        "code_hash": code_hash, "seed": config.seed,
        "package_versions": {"python": platform.python_version(), "pandas": pd.__version__},
        "winner": winner, "result_hash": canonical_hash({"trials": trials, "winner": winner}),
    }
    (root / "study_manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return manifest
