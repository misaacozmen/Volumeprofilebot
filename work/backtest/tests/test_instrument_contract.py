from __future__ import annotations

import pytest
from dataclasses import replace

from backtest.live.instruments import InstrumentContractError, InstrumentRegistry, validate_economic_semantics, validate_metadata
from backtest.live.contracts import InstrumentContract


def contract() -> InstrumentContract:
    return InstrumentContract("XM_MT5_US100CASH_SUPER1", "index", "MT5", "XMGlobal-MT5 2", "US100Cash", "America/New_York", "USD", "USD", "USD", 2, .01, .01, 1., .01, 100., .01, .01, "price", 0)


def metadata(name: str = "US100Cash") -> dict[str, object]:
    return {"name": name, "digits": 2, "point": .01, "tick_size": .01, "contract_size": 1., "volume_min": .01, "volume_max": 100., "volume_step": .01, "base_currency": "USD", "profit_currency": "USD", "margin_currency": "USD", "trade_calc_mode": 0}


def test_broker_pip_size_is_not_required_when_signed_economic_semantics_exist() -> None:
    observed = metadata()
    validate_metadata(contract(), observed)


def test_registry_requires_exact_case_sensitive_symbol_and_metadata() -> None:
    registry = InstrumentRegistry([contract()])
    assert registry.symbol_info("US100Cash", metadata()).broker_symbol == "US100Cash"
    with pytest.raises(InstrumentContractError):
        registry.symbol_info("US100", metadata("US100Cash"))
    with pytest.raises(InstrumentContractError):
        registry.symbol_info("US100Cash", metadata("us100cash"))
    with pytest.raises(InstrumentContractError):
        validate_metadata(contract(), {**metadata(), "digits": 3})


def test_semantic_metadata_and_economic_probe_fail_closed() -> None:
    expected = replace(
        contract(), canonical_underlying="NASDAQ_100", expected_company="XM Global Limited",
        broker_path_regex=r"^Indices\\US Indices$", broker_description_regex=r"^US100 Cash Index$",
        session_calendar_id="US_EQUITY_RTH", expected_one_tick_value_at_min_volume=0.001,
        economic_value_tolerance=0.0001,
    )
    observed = {**metadata(), "path": r"Indices\US Indices", "description": "US100 Cash Index"}
    validate_metadata(expected, observed)
    validate_economic_semantics(expected, lambda *args: 0.001)
    with pytest.raises(InstrumentContractError, match="path"):
        validate_metadata(expected, {**observed, "path": "Forex"})
    with pytest.raises(InstrumentContractError, match="economic"):
        validate_economic_semantics(expected, lambda *args: -0.001)
