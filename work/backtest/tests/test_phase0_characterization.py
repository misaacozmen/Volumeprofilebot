from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from backtest.config import SymbolConfig
from backtest.engine_pipeline import EngineLeg, run_canonical_pair_pipeline
from test_xm_mt5_forward import (
    DocumentedLifecycleMt5,
    FakeOrderMt5,
    PartialFillMt5,
    demo_client,
    order_candidate,
    place_candidate,
)


ROOT = Path(__file__).resolve().parents[1]
CAPITAL_SPEC = importlib.util.spec_from_file_location(
    "phase0_run_capital_forward",
    ROOT / "scripts" / "run_capital_forward.py",
)
assert CAPITAL_SPEC and CAPITAL_SPEC.loader
CAPITAL_MODULE = importlib.util.module_from_spec(CAPITAL_SPEC)
CAPITAL_SPEC.loader.exec_module(CAPITAL_MODULE)


def test_phase0_legacy_send_armed_path_is_blocked_without_durable_write_adapter(tmp_path, monkeypatch) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    original_send = mt5.order_send
    monkeypatch.setattr(
        mt5,
        "order_send",
        lambda request: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(Exception, match="durable write adapter"):
        place_candidate(client, tmp_path)

    assert client._intent_state(tmp_path, "order-1")["status"] == "SEND_ARMED"
    assert mt5.pending_send_count == 0

    monkeypatch.setattr(mt5, "order_send", original_send)
    restarted = demo_client(mt5)
    result = place_candidate(restarted, tmp_path)
    assert result["state"] == "IDEMPOTENT_SEND_ARMED_RECONCILE_REQUIRED"
    assert mt5.pending_send_count == 0


def test_phase0_partial_fill_restart_reconciles_from_broker_state(tmp_path) -> None:
    mt5 = PartialFillMt5()
    client = demo_client(mt5)
    place_candidate(client, tmp_path)

    first = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert first["broker_states"][0]["state"] == "PARTIAL_FILL"
    assert mt5.pending_send_count == 1

    restarted = demo_client(mt5)
    second = restarted.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:06:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert second["broker_states"][0]["state"] == "PARTIAL_FILL"
    assert mt5.pending_send_count == 1


def test_phase0_fatal_latch_blocks_daemon_restart(tmp_path) -> None:
    CAPITAL_MODULE.write_fatal_latch(tmp_path, "UNSAFE_OPEN_ORDERS", "readback failed")

    with pytest.raises(CAPITAL_MODULE.CriticalLiveError, match="Persistent fatal latch"):
        CAPITAL_MODULE.assert_no_fatal_latch(tmp_path)


def test_phase0_missing_pair_leg_has_no_decision_or_order() -> None:
    config_a = SymbolConfig("FEED_A", "5m", 0.0, 0.0, 0.0)
    config_b = SymbolConfig("FEED_B", "5m", 0.0, 0.0, 0.0)
    frame_a = pd.DataFrame(
        {
            "time": [pd.Timestamp("2025-02-03T14:00:00Z")],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1.0],
        }
    )
    frame_b = pd.DataFrame(
        {
            "time": [pd.Timestamp("2025-02-04T14:00:00Z")],
            "open": [200.0],
            "high": [201.0],
            "low": [199.0],
            "close": [200.5],
            "volume": [1.0],
        }
    )

    with pytest.raises(ValueError, match="missing leg data"):
        run_canonical_pair_pipeline(
            [EngineLeg("a", frame_a, config_a), EngineLeg("b", frame_b, config_b)],
            [date(2025, 2, 3)],
            require_same_feed=False,
        )


def test_phase0_broker_deal_and_position_are_live_truth(tmp_path) -> None:
    mt5 = DocumentedLifecycleMt5("long")
    client = demo_client(mt5)
    decision = {
        **order_candidate(),
        "direction": "long",
        "stop_price": 90.0,
        "target_price": 120.0,
    }
    assert place_candidate(client, tmp_path, decision)["state"] == "SUBMITTED"

    result = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["broker_states"][0]["state"] == "OPEN_PROTECTED"
    assert any(call["ticket"] == 1001 for call in mt5.history_deal_calls)
    assert any(call["position"] == 3001 for call in mt5.history_deal_calls)
