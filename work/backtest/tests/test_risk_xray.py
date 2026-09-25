from __future__ import annotations

import json

import pandas as pd
import pytest

from backtest.numeric_contracts import FinancialMathError
from backtest.risk_xray import build_risk_xray, write_risk_xray


def test_risk_xray_is_fill_based_and_includes_zero_trade_eligible_days(tmp_path) -> None:
    fills = pd.DataFrame([
        {"date": "2025-02-03", "symbol": "NQ", "direction": "long", "r_multiple": 2.0, "terminal_known_time": "2025-02-03T15:00:00Z"},
        {"date": "2025-02-05", "symbol": "SPX", "direction": "short", "r_multiple": -1.0, "terminal_known_time": "2025-02-05T15:00:00Z"},
    ])
    xray = build_risk_xray(fills, eligible_dates=["2025-02-03", "2025-02-04", "2025-02-05"])
    assert xray["fill_count"] == 2
    assert xray["net_r"] == 1.0
    assert xray["profit_factor"] == 2.0
    assert xray["daily_net_r_volatility"] is not None
    json_path, md_path = write_risk_xray(xray, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["fill_count"] == 2
    assert md_path.exists()


def test_no_trades_and_no_gross_loss_are_null_not_zero_or_nonfinite() -> None:
    xray = build_risk_xray(pd.DataFrame(columns=["r_multiple", "date", "symbol", "direction"]))
    assert xray["expectancy"] is None
    assert xray["profit_factor_reason"] == "NO_CLOSED_TRADES"
    json.dumps(xray, allow_nan=False)


def test_risk_xray_rejects_closed_fill_without_terminal_time() -> None:
    with pytest.raises(FinancialMathError, match="terminal_known_time"):
        build_risk_xray(pd.DataFrame([{"date": "2025-02-03", "r_multiple": 1.0}]))


def test_risk_xray_does_not_invent_open_interval_evidence() -> None:
    xray = build_risk_xray(
        pd.DataFrame([{"date": "2025-02-03", "r_multiple": 1.0, "terminal_known_time": "2025-02-03T15:00:00Z", "open_risk": 1.0}])
    )
    assert xray["simultaneous_open_risk"] is None
    assert xray["simultaneous_open_risk_reason"] == "NO_OPEN_INTERVAL_EVIDENCE"
