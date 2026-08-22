from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_canonical_production_full_history.py"
SPEC = importlib.util.spec_from_file_location("run_canonical_production_full_history", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_drawdown_includes_losses_below_initial_zero_equity() -> None:
    filled = pd.DataFrame(
        [
            {"date": "2026-01-02", "outcome": "SL", "r_multiple": -1.0},
            {"date": "2026-01-03", "outcome": "TP", "r_multiple": 3.0},
        ]
    )
    summary = MODULE.performance_summary(filled, pd.DataFrame(), pd.DataFrame())
    assert summary["max_drawdown_r"] == -1.0
