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
        space: list[tuple[str, tuple[object, ...]]] = []
        for name, values in sorted(raw["search_space"].items()):
            if name not in ALLOWED_PARAMETERS or not isinstance(values, list) or not values:
                raise StudyConfigError("search space contains an unknown or unbounded parameter")
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
        metrics = dict(evaluator(parameters))
        if any(isinstance(value, float) and not math.isfinite(value) for value in metrics.values()):
            raise StudyConfigError("evaluator returned a non-finite metric")
        closed = int(metrics.get("fill_count") or 0)
        objective = metrics.get(config.objective_metric)
        valid = closed >= config.minimum_closed_trades and isinstance(objective, (int, float)) and not isinstance(objective, bool) and math.isfinite(float(objective))
        trials.append({"parameter_hash": canonical_hash(parameters), "parameters": dict(parameters), "valid": valid, **metrics})
    valid_trials = [row for row in trials if row["valid"]]
    winner = None if not valid_trials else sorted(valid_trials, key=lambda row: (-float(row[config.objective_metric]), -float(row.get("expectancy") or -math.inf), abs(float(row.get("max_drawdown_r") or math.inf)), str(row["parameter_hash"])))[0]
    root = Path(output_dir); root.mkdir(parents=True, exist_ok=True)
    pd.json_normalize(trials).to_csv(root / "trials.csv", index=False)
    study_value = config.__dict__ if hasattr(config, "__dict__") else {name: getattr(config, name) for name in config.__slots__}
    manifest = {
        "status": "RESEARCH_ONLY_NON_PROMOTABLE", "study": study_value,
        "study_config_hash": canonical_hash(study_value), "data_hash": data_hash,
        "code_hash": code_hash, "seed": config.seed,
        "package_versions": {"python": platform.python_version(), "pandas": pd.__version__},
        "winner": winner, "result_hash": canonical_hash({"trials": trials, "winner": winner}),
    }
    (root / "study_manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return manifest
