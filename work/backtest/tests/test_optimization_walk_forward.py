from __future__ import annotations

import json

import pandas as pd
import pytest

from backtest.optimization import StudyConfig, StudyConfigError, optimize
from backtest.walk_forward import walk_forward


def study(tmp_path, **updates):
    value = {
        "seed": 7, "symbol": "CAPITALCOM_NAS100", "timeframe": "5m",
        "search_space": {"reward_r": [2.0, 3.0]}, "train_sessions": 5,
        "oos_sessions": 2, "step_sessions": 2, "anchored": True, "max_evals": 2,
        "minimum_closed_trades": 1, "objective_metric": "daily_r_sharpe_rf0",
    }
    value.update(updates)
    path = tmp_path / "study.json"; path.write_text(json.dumps(value), encoding="utf-8")
    return StudyConfig.load(path)


def metrics(parameters):
    reward = float(parameters["reward_r"])
    return {"fill_count": 2, "daily_r_sharpe_rf0": reward, "expectancy": reward / 2, "max_drawdown_r": -1.0}


def test_optimization_is_deterministic_and_research_only(tmp_path) -> None:
    config = study(tmp_path)
    first = optimize(config, metrics, tmp_path / "one")
    second = optimize(config, metrics, tmp_path / "two")
    assert first["result_hash"] == second["result_hash"]
    assert first["winner"]["parameters"] == {"reward_r": 3.0}
    assert first["status"] == "RESEARCH_ONLY_NON_PROMOTABLE"


@pytest.mark.parametrize("updates", [
    {"search_space": {"unknown": [1]}},
    {"search_space": {"reward_r": [2.0, 2.0]}},
    {"search_space": {"reward_r": [float("inf")]}},
    {"max_evals": 1001},
])
def test_study_schema_fails_closed(tmp_path, updates) -> None:
    with pytest.raises(StudyConfigError):
        study(tmp_path, **updates)


def test_walk_forward_selects_on_train_and_reports_oos_only(tmp_path) -> None:
    config = study(tmp_path)
    calls = []
    def evaluator(parameters, sessions):
        calls.append(tuple(sessions))
        reward = float(parameters["reward_r"])
        rows = pd.DataFrame({
            "date": list(sessions), "symbol": "NQ", "direction": "long",
            "r_multiple": reward, "result": "win",
            "terminal_known_time": [f"{day}T20:00:00Z" for day in sessions],
        })
        return rows, {"fill_count": len(rows), "daily_r_sharpe_rf0": reward, "expectancy": reward, "max_drawdown_r": -1.0}
    sessions = list(pd.date_range("2025-01-02", periods=9, freq="B").date)
    first = walk_forward(config, sessions, evaluator, tmp_path / "wf1")
    second = walk_forward(config, sessions, evaluator, tmp_path / "wf2")
    assert first["result_hash"] == second["result_hash"]
    assert first["folds"] == 2
    assert len(pd.read_csv(tmp_path / "wf1/oos_trades.csv")) == 4
