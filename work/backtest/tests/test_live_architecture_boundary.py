from __future__ import annotations

import ast
from dataclasses import fields
import json
from pathlib import Path
import sys
import types

import pytest

from backtest.signals import SignalProposal
from backtest.live.contracts import RiskApprovedOrder
from backtest.live.execution import ExecutionBoundaryError, FakeExecutionAdapter, execute


ROOT = Path(__file__).resolve().parents[1]


def test_signal_proposal_has_no_risk_or_broker_fields() -> None:
    names = {item.name for item in fields(SignalProposal)}
    assert not names.intersection(
        {"volume", "lot", "leverage", "risk_percent", "margin", "broker_ticket", "account", "equity"}
    )


def test_strategy_module_has_no_live_or_process_imports() -> None:
    tree = ast.parse((ROOT / "backtest" / "strategy.py").read_text(encoding="utf-8"))
    forbidden = {"MetaTrader5", "os", "subprocess", "socket", "requests", "httpx", "urllib"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert imported.isdisjoint(forbidden)


def test_execution_accepts_approved_order_only() -> None:
    adapter = FakeExecutionAdapter()
    with pytest.raises(ExecutionBoundaryError):
        execute(adapter, object())


def test_super1_normal_constructor_owns_the_single_live_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.run_super1_xm_mt5_forward as super1

    config = json.loads((ROOT / "live_forward/super1_xm_mt5_demo_config.json").read_text(encoding="utf-8"))
    monkeypatch.setitem(sys.modules, "MetaTrader5", types.ModuleType("MetaTrader5"))
    client = super1.Super1XmMt5DemoOrderClient(
        config,
        {"XM_MT5_SERVER": str(config["expected_server"])},
    )

    assert type(client) is super1.Super1XmMt5DemoOrderClient
    assert client._strict_reconciliation is True
    source = (ROOT / "scripts/run_super1_xm_mt5_forward.py").read_text(encoding="utf-8")
    assert "ProductionOrderFlow" in source
    assert "Mt5ExecutionAdapter" not in source


def test_production_has_one_physical_mt5_write_port() -> None:
    writes: list[Path] = []
    for root in (ROOT / "backtest", ROOT / "scripts"):
        for path in root.rglob("*.py"):
            if path.name == "mt5_market_roundtrip_smoke.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "order_send":
                    writes.append(path)
    assert writes == [ROOT / "backtest" / "live" / "execution.py"]
