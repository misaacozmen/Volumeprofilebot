from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.config import SymbolConfig
from backtest.numeric_contracts import FinancialMathError, finite_float, positive_float, strict_ratio
from backtest.strategy import round_price_to_tick, simulate_exit
from backtest.volume_profile import compute_volume_profile


@pytest.mark.parametrize("value", [0, np.nan, np.inf, -np.inf])
def test_numeric_contracts_reject_invalid_values(value) -> None:
    if value == 0:
        assert finite_float(value) == 0.0
    else:
        with pytest.raises(FinancialMathError):
            finite_float(value)
    with pytest.raises(FinancialMathError):
        positive_float(value)


def test_financial_edge_cases_fail_closed() -> None:
    with pytest.raises(FinancialMathError):
        SymbolConfig("X", "5m", 1, 0, 0, reward_r=-1)
    with pytest.raises(ValueError):
        round_price_to_tick(1.0, 0)
    with pytest.raises(ValueError):
        simulate_exit([], pd.Timestamp("2025-01-01", tz="UTC"), "long", 100, 100, 101, 1)
    with pytest.raises(ValueError):
        compute_volume_profile(pd.DataFrame({"low": [1], "high": [2], "volume": [np.inf]}))
    with pytest.raises(FinancialMathError):
        strict_ratio(-1)
