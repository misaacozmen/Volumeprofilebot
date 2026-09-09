from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.config import SymbolConfig
from backtest.numeric_contracts import FinancialMathError, finite_float, finite_vector, positive_float, quantize_price, quantize_volume_down, safe_divide, strict_ratio, validate_trade_geometry
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


@pytest.mark.parametrize("value", [True, None, "1", np.nan, np.inf, -np.inf])
def test_broker_quantization_rejects_non_numeric_and_nonfinite(value) -> None:
    with pytest.raises(FinancialMathError):
        quantize_volume_down(value, 0.1, 0.1, 10)


def test_decimal_quantization_never_increases_volume_risk() -> None:
    assert quantize_volume_down(1.29, 0.1, 0.1, 10.0) == 1.2
    assert quantize_price(100.125, 0.05) == 100.1
    assert safe_divide(1, 0, reason="ZERO_VARIANCE") == (None, "ZERO_VARIANCE")
    assert validate_trade_geometry("long", 100, 99, 103) == (100.0, 99.0, 103.0)
    assert validate_trade_geometry("short", 100, 101, 97) == (100.0, 101.0, 97.0)
    with pytest.raises(FinancialMathError):
        finite_vector([1.0, float("nan")])
