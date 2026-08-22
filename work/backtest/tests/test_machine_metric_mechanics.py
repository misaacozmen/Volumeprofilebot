from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

from scripts.machine_policy_v1 import decision_allowed


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_machine_metric_mechanics.py"
SPEC = importlib.util.spec_from_file_location("run_machine_metric_mechanics", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_policy_blocks_only_requested_leg_and_context() -> None:
    decisions = pd.DataFrame(
        [
            {
                "order_state": "FILLED",
                "leg_key": "spx",
                "context_source": "FIRST_QUALIFIED_STRUCTURE",
                "outcome": "SL",
                "r_multiple": -0.5,
                "date": "2026-01-02",
                "entry_time": "2026-01-02T10:00:00-05:00",
                "terminal_known_time": "2026-01-02T10:05:00-05:00",
                "symbol": "SPX",
            },
            {
                "order_state": "FILLED",
                "leg_key": "nq",
                "context_source": "FIRST_QUALIFIED_STRUCTURE",
                "outcome": "TP",
                "r_multiple": 2.4,
                "date": "2026-01-02",
                "entry_time": "2026-01-02T10:10:00-05:00",
                "terminal_known_time": "2026-01-02T10:20:00-05:00",
                "symbol": "NQ",
            },
        ]
    )
    result = MODULE.apply_machine_policy(decisions, {("spx", "FIRST_QUALIFIED_STRUCTURE")})
    assert result["leg_key"].tolist() == ["nq"]
    assert result["r_multiple"].tolist() == [2.4]


def test_frozen_machine_policy_blocks_only_selected_spx_sources() -> None:
    config = {
        "blocked_context_sources": {
            "nq": [],
            "spx": ["FIRST_QUALIFIED_STRUCTURE", "SELECTED_LIQUIDITY_SWEEP"],
        }
    }
    assert not decision_allowed(
        {"leg_key": "spx", "context_source": "SELECTED_LIQUIDITY_SWEEP"}, config
    )
    assert decision_allowed(
        {"leg_key": "nq", "context_source": "SELECTED_LIQUIDITY_SWEEP"}, config
    )
    assert decision_allowed(
        {"leg_key": "spx", "context_source": "PREMARKET_CONTEXT"}, config
    )


def test_machine_policy_can_block_weekday_without_blocking_other_days() -> None:
    config = {
        "blocked_context_sources": {"nq": [], "spx": []},
        "blocked_weekdays": {"spx": ["Tuesday"]},
    }
    base = {
        "leg_key": "spx",
        "context_source": "PREMARKET_CONTEXT",
        "direction": "long",
    }
    assert not decision_allowed({**base, "date": "2026-07-28"}, config)
    assert decision_allowed({**base, "date": "2026-07-29"}, config)
