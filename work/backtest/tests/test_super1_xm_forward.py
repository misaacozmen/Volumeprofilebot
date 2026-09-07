from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from v08_helpers import checkpoint_if_enabled, record_if_enabled


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from super1_continuation import (
    build_transition_record,
    validate_fixture_snapshot,
    validate_transition_record,
)
SPEC = importlib.util.spec_from_file_location(
    "run_super1_xm_mt5_forward", ROOT / "scripts" / "run_super1_xm_mt5_forward.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _TestSuper1Client(MODULE.Super1XmMt5DemoOrderClient):
    """Test-only LIVE_LEASE adapter; production never bypasses its validator."""

    def _assert_super1_lease(self, output_root: Path, *, for_order: bool = True) -> dict[str, object]:
        del output_root, for_order
        lease_id = str(uuid4())
        self._last_lease_binding = {
            "binding_kind": "LIVE_LEASE",
            "lease_id": lease_id,
            "release_id": "TEST_RELEASE",
            "runner_sid": "S-1-5-18",
            "invocation_nonce": str(uuid4()),
            "lease_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "candidate_sha256": "c" * 64,
            "harness_sha256": "d" * 64,
            "manifest_sha256": "e" * 64,
            "release_manifest_sha256": "f" * 64,
        }
        return {"expires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}

    def _place_candidate(self, output_root, decision, symbol, reward_r, **kwargs):
        with MODULE.order_mutex():
            self._assert_super1_lease(output_root, for_order=True)
            return self._place_candidate_under_mutex(output_root, decision, symbol, reward_r, **kwargs)

    def _final_send_gate(self, output_root, order_id, request, decision, symbol, final_context):
        del order_id, request, decision, symbol, final_context
        self._assert_super1_lease(output_root, for_order=True)


MODULE.Super1XmMt5DemoOrderClient = _TestSuper1Client


def record(level: str, price: float, touches: int | None = None) -> dict:
    payload = {
        "date": "2026-08-05",
        "payload": {
            "days": [
                {
                    "leg_key": "nq",
                    "vah": 20100.0,
                    "val": 20000.0,
                    "liquidity": {
                        "selected_name": level,
                        "selected_price": price,
                        "candidate_names": [level],
                        "candidate_prices": [price],
                    },
                }
            ]
        },
    }
    if touches is not None:
        payload["graph_features"] = {"nq": {"swing_touches": {level: touches}}}
    return payload


def test_vah_val_proximity_is_reconstructed_from_graph_evidence() -> None:
    decision = {"leg_key": "nq", "liquidity_context": "custom_low low sweep near VA"}
    config = SimpleNamespace(vah_val_tolerance=5.0)
    assert MODULE.liquidity_type(record("custom_low", 20008.0), decision, config) == "vah_val_proximity"
    assert MODULE.liquidity_type(record("custom_low", 20020.0), decision, config) == "other_liquidity"


def test_unprovable_liquidity_price_blocks_instead_of_guessing() -> None:
    decision = {"leg_key": "nq", "liquidity_context": "unknown_low low sweep near VA"}
    config = SimpleNamespace(vah_val_tolerance=5.0)
    broken = record("different_low", 20008.0)
    try:
        MODULE.liquidity_type(broken, decision, config)
    except MODULE.Super1FeatureError as exc:
        assert "cannot be proven" in str(exc)
    else:
        raise AssertionError("Unprovable liquidity feature was accepted.")


def test_swing_liquidity_matches_frozen_candidate_classification() -> None:
    config = SimpleNamespace(vah_val_tolerance=5.0)
    strong = {"leg_key": "nq", "liquidity_context": "swing_high_1 high sweep near VA"}
    weak = {"leg_key": "nq", "liquidity_context": "swing_low_1 low sweep near VA"}

    assert MODULE.liquidity_type(record("swing_high_1", 20100.0, 3), strong, config) == "strong_swing"
    assert MODULE.liquidity_type(record("swing_high_1", 20100.0, 2), strong, config) == "equal_high_low"
    assert MODULE.liquidity_type(record("swing_low_1", 20004.0, 1), weak, config) == "vah_val_proximity"


def test_unproven_swing_touch_count_blocks_instead_of_guessing() -> None:
    decision = {"leg_key": "nq", "liquidity_context": "swing_high_1 high sweep near VA"}
    config = SimpleNamespace(vah_val_tolerance=5.0)
    try:
        MODULE.liquidity_type(record("swing_high_1", 20100.0), decision, config)
    except MODULE.Super1FeatureError as exc:
        assert "cannot be proven" in str(exc)
    else:
        raise AssertionError("Unproven swing evidence was accepted.")


def test_risk_volume_rounds_down_and_never_exceeds_budget() -> None:
    volume = MODULE.Super1XmMt5DemoOrderClient._aligned_volume(0.376, 0.1, 10.0, 0.1)
    assert volume == 0.3


def test_risk_volume_below_broker_minimum_is_blocked() -> None:
    try:
        MODULE.Super1XmMt5DemoOrderClient._aligned_volume(0.05, 0.1, 10.0, 0.1)
    except MODULE.xm.CandidateNotExecutableError as exc:
        assert exc.code == "RISK_BELOW_MINIMUM_VOLUME"
        assert "below broker minimum" in str(exc)
    else:
        raise AssertionError("Minimum lot was incorrectly forced above the risk budget.")


def test_empty_terminal_history_starts_at_nonnegative_scale() -> None:
    class FakeMt5:
        ACCOUNT_TRADE_MODE_DEMO = 0
        DEAL_ENTRY_OUT = 1
        DEAL_REASON_SL = 4
        DEAL_REASON_TP = 5

        def account_info(self):
            return SimpleNamespace(
                login=[REDACTED,
                server="XMGlobal-MT5 2",
                company="XM Global Limited",
                trade_mode=0,
            )

        def terminal_info(self):
            return SimpleNamespace(connected=True)

        def positions_get(self):
            return ()

        def history_deals_get(self, start, end):
            return ()

    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.mt5 = FakeMt5()
    client.magic = 260805101
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client.login_id = int(client.config["account_login"])
    client.connected = True
    client.demo_verified = True
    state = client._terminal_r_state()
    assert state["state_sum"] == 0.0
    assert state["risk_scale"] == 1.1
    assert state["open_trade_outcome_used"] is False


def test_m03_terminal_r_uses_real_super1_deal_chain_and_lookback(monkeypatch, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    class HistoryMt5:
        DEAL_ENTRY_OUT = 1
        DEAL_REASON_SL = 4
        DEAL_REASON_TP = 5

        def __init__(self) -> None:
            self.magic = 260805101
            self.deals = [
                SimpleNamespace(
                    magic=self.magic,
                    entry=1,
                    position_id=10_000 + index,
                    reason=5 if index in {0, 1} else 4,
                    symbol="US100Cash",
                    ticket=50_000 + index,
                    time_msc=1_785_330_300_000 + index,
                )
                for index in range(11)
            ]
            self.deals.extend(
                [
                    SimpleNamespace(
                        magic=999,
                        entry=1,
                        position_id=99_999,
                        reason=5,
                        symbol="US100Cash",
                        ticket=60_000,
                        time_msc=1_785_330_400_000,
                    ),
                    SimpleNamespace(
                        magic=self.magic,
                        entry=1,
                        position_id=20_000,
                        reason=5,
                        symbol="US100Cash",
                        ticket=60_001,
                        time_msc=1_785_330_400_001,
                    ),
                ]
            )

        def positions_get(self):
            return (SimpleNamespace(ticket=20_000, identifier=20_000, magic=self.magic),)

        def account_info(self):
            return SimpleNamespace(
                login=[REDACTED,
                server="XMGlobal-MT5 2",
                company="XM Global Limited",
                trade_mode=0,
            )

        def terminal_info(self):
            return SimpleNamespace(connected=True)

        def history_deals_get(self, start, end):
            del start, end
            return tuple(self.deals)

    monkeypatch.setattr(MODULE.core, "utc_now", lambda: MODULE.pd.Timestamp("2026-08-05T14:00:00Z"))
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.mt5 = HistoryMt5()
    client.magic = client.mt5.magic
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client.login_id = int(client.config["account_login"])
    client.connected = True
    client.demo_verified = True

    state = client._terminal_r_state()

    assert state["terminal_count"] == 11
    assert len(state["terminal_raw_r"]) == 10
    assert state["open_trade_outcome_used"] is False
    assert state["risk_scale"] == 0.75
    record_if_enabled(request, evidence_token)


def test_m03_terminal_r_is_broker_sidecar_bound_and_ignores_event_r_claims(tmp_path, monkeypatch) -> None:
    deals = [
        {
            "time_msc": 1_785_330_300_000 + index,
            "ticket": 50_000 + index,
            "position_id": 10_000 + index,
            "order": 40_000 + index,
            "entry": 1,
            "reason": 5 if index in {0, 1} else 4,
            "symbol": "US100Cash",
            "volume": 0.1,
            "magic": 260805101,
            "raw_r": 999.0,
        }
        for index in range(11)
    ]
    deals.append({
        "time_msc": 1_785_330_400_000,
        "ticket": 60_001,
        "position_id": 20_000,
        "order": 40_001,
        "entry": 1,
        "reason": 5,
        "symbol": "US100Cash",
        "volume": 0.1,
        "magic": 260805101,
        "raw_r": 999.0,
    })
    broker_history = {
        "schema_version": 1,
        "account_login": [REDACTED,
        "server": "XMGlobal-MT5 2",
        "observed_at": "2026-08-05T14:00:00+00:00",
        "terminal_history_days": 365,
        "records": deals,
    }
    positions = {
        "schema_version": 1,
        "account_login": [REDACTED,
        "server": "XMGlobal-MT5 2",
        "observed_at": "2026-08-05T14:00:00+00:00",
        "terminal_history_days": 365,
        "records": [{
            "ticket": 20_000,
            "identifier": 20_000,
            "magic": 260805101,
            "symbol": "US100Cash",
            "type": 0,
            "volume": 0.1,
            "sl": 90.0,
            "tp": 120.0,
        }],
    }
    (tmp_path / "broker-history.json").write_text(json.dumps(broker_history), encoding="utf-8")
    (tmp_path / "positions.json").write_text(json.dumps(positions), encoding="utf-8")

    class SidecarMt5:
        DEAL_ENTRY_OUT = 1
        DEAL_REASON_SL = 4
        DEAL_REASON_TP = 5

        def account_info(self):
            return SimpleNamespace(
                login=[REDACTED, server="XMGlobal-MT5 2", company="XM Global Limited", trade_mode=0
            )

        def terminal_info(self):
            return SimpleNamespace(connected=True)

        def positions_get(self):
            rows = json.loads((tmp_path / "positions.json").read_text(encoding="utf-8"))["records"]
            return tuple(SimpleNamespace(**row) for row in rows)

        def history_deals_get(self, start, end):
            del start, end
            rows = json.loads((tmp_path / "broker-history.json").read_text(encoding="utf-8"))["records"]
            return tuple(SimpleNamespace(**row) for row in rows)

    monkeypatch.setattr(MODULE.core, "utc_now", lambda: MODULE.pd.Timestamp("2026-08-05T14:00:00Z"))
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.mt5 = SidecarMt5()
    client.magic = 260805101
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client.login_id = [REDACTED
    client.connected = True
    client.demo_verified = True
    state = client._terminal_r_state()
    assert state["terminal_count"] == 11
    assert state["terminal_raw_r"] == [3.0] + [-1.0] * 9
    assert state["risk_scale"] == 0.75


def test_super1_preflight_checks_both_risk_states() -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))

    assert client._preflight_risk_scales() == (0.75, 1.1)


def test_continuation_design_is_non_applying_and_snapshot_gated(request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    record = build_transition_record(
        created_at="2026-08-31T00:00:00+00:00",
        original_campaign_lock_sha256=None,
        previous_transition_hash=None,
        old_hashes={key: None for key in ("release", "runtime", "harness", "contract", "calendar")},
        new_hashes={key: "a" * 64 for key in ("release", "runtime", "harness", "contract", "calendar")},
        unchanged_engine_and_risk={key: "b" * 64 for key in ("engine", "frozen_candidate", "strategy_risk")},
        broker_identity="login|server|company",
        source_snapshot_manifest_sha256=None,
        state_schema_versions={"order_idempotency_sqlite": 1},
    )
    result = validate_transition_record(record)
    assert result == {
        "status": "SOURCE_SNAPSHOT_NOT_VERIFIED",
        "safe_to_apply": False,
        "errors": [],
    }
    assert record["apply_allowed"] is False
    assert "broker_identity_digest" in record
    assert "login|server|company" not in json.dumps(record)
    record_if_enabled(request, evidence_token)


def test_continuation_fixture_preserves_execution_history() -> None:
    fixture = {
        "created_at_before": "t0",
        "created_at_after": "t0",
        "order_ids_before": ["o1", "o2"],
        "order_ids_after": ["o1", "o2"],
        "unknown_before": ["o2"],
        "unknown_after": ["o2"],
        "outbox_before": [1, 2, 3],
        "outbox_after": [1, 2, 3],
        "deal_tickets_after": [10, 11],
        "completed_before": {"o1": "CLOSED_TP"},
        "completed_after": {"o1": "CLOSED_TP"},
        "terminal_r_before": list(range(12)),
        "terminal_r_after": list(range(12)),
    }
    assert validate_fixture_snapshot(fixture)["status"] == "PASS"


def test_super1_runtime_is_bound_to_sealed_candidate() -> None:
    runtime = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    candidate = MODULE.validate_super1_candidate(runtime)

    assert candidate["artifact_sha256"] == runtime["candidate_artifact_sha256"]
    contract = json.loads(
        (ROOT / runtime["signal_contract_path"]).read_text(encoding="utf-8")
    )
    manifest = json.loads(MODULE.SUPER1_MANIFEST.read_text(encoding="utf-8"))
    research_input = next(
        item for item in candidate["provenance"]["inputs"] if item["role"] == "research_dataset"
    )
    assert contract["overlay_candidate"]["scope"] == "SETUP_FILTERS_AND_RISK_SCALING_ONLY"
    assert contract["safety"]["independent_super1_signal_producer_present"] is False
    assert contract["safety"]["candidate_research_results_apply_to_deployed_pipeline"] is False
    assert runtime["deployment_mode"] == MODULE.DEPLOYMENT_MODE
    assert manifest["overlay_candidate_research_dataset_sha256"] == research_input["sha256"]
    assert manifest["overlay_candidate_research_result_sha256"] == candidate["full_evaluation_result_sha256"]
    assert manifest["deployed_pipeline_historical_parity_proven"] is False
    assert manifest["deployed_pipeline_result_sha256"] is None
    assert "data_sha256" not in manifest
    assert "result_sha256" not in manifest
    tampered = {**runtime, "candidate_file_sha256": "0" * 64}
    try:
        MODULE.validate_super1_candidate(tampered)
    except MODULE.Super1FeatureError as exc:
        assert "file hash mismatch" in str(exc)
    else:
        raise AssertionError("A mismatched Super1 candidate hash was accepted.")

    tampered_contract = {**runtime, "signal_contract_sha256": "0" * 64}
    try:
        MODULE.validate_super1_candidate(tampered_contract)
    except MODULE.Super1FeatureError as exc:
        assert "signal contract hash mismatch" in str(exc)
    else:
        raise AssertionError("A mismatched Super1 signal contract was accepted.")


def test_super1_contract_binds_runtime_implementation_bytes() -> None:
    runtime = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    contract = json.loads((ROOT / runtime["signal_contract_path"]).read_text(encoding="utf-8"))
    source = contract["signal_source"]
    overlay = contract["overlay_candidate"]
    transport = contract["demo_order_transport"]

    assert MODULE.core.file_hash(ROOT / source["generator_path"]) == source["generator_sha256"]
    assert MODULE.core.file_hash(ROOT / source["payload_adapter_path"]) == source["payload_adapter_sha256"]
    assert MODULE.core.source_code_hash() == source["engine_source_sha256"]
    assert MODULE.core.file_hash(ROOT / overlay["runtime_path"]) == overlay["runtime_sha256"]
    assert MODULE.core.file_hash(ROOT / transport["path"]) == transport["sha256"]


def test_super1_contract_rejects_changed_forward_shadow_adapter(monkeypatch) -> None:
    runtime = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    real_file_hash = MODULE.core.file_hash

    def changed_adapter_hash(path: Path) -> str:
        if Path(path).resolve() == MODULE.FORWARD_SHADOW_ADAPTER.resolve():
            return "0" * 64
        return real_file_hash(Path(path))

    monkeypatch.setattr(MODULE.core, "file_hash", changed_adapter_hash)
    with pytest.raises(MODULE.Super1FeatureError, match="signal contract is invalid"):
        MODULE.validate_super1_candidate(runtime)


def test_configure_core_locks_forward_shadow_adapter(monkeypatch) -> None:
    for owner, name in (
        (MODULE.xm, "RUNTIME_CONFIG"),
        (MODULE.core, "RUNTIME_CONFIG"),
        (MODULE.core, "SCRIPT_PATH"),
        (MODULE.core, "HARNESS_PATHS"),
        (MODULE.core, "REQUIRED_ENV"),
        (MODULE.core, "CapitalDemoClient"),
    ):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    monkeypatch.setattr(MODULE.core, "install_xm_scheduled_gap_integrity", lambda: None)

    MODULE.configure_core()

    assert MODULE.FORWARD_SHADOW_ADAPTER.resolve() in MODULE.core.HARNESS_PATHS


def test_reconcile_result_logs_canonical_overlay_deployment_mode(monkeypatch, tmp_path: Path) -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = {"deployment_mode": MODULE.DEPLOYMENT_MODE}
    monkeypatch.setattr(
        MODULE.xm.XmMt5DemoOrderClient,
        "reconcile_orders",
        lambda self, output_root, prefix, now, lock: {"state": "NO_EXECUTABLE_PREFIX"},
    )

    result = client.reconcile_orders(
        tmp_path,
        {"state": "NO_EXECUTABLE_PREFIX"},
        MODULE.pd.Timestamp("2026-08-05T14:00:00Z"),
        {},
    )

    assert result["strategy"] == "Super1"
    assert result["deployment_mode"] == MODULE.DEPLOYMENT_MODE


def test_super1_filter_blocks_only_frozen_rules_and_allows_valid_candidate(monkeypatch) -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client._super1_record = record("custom_low", 20004.0)
    monkeypatch.setattr(
        MODULE.core,
        "live_strategy_objects",
        lambda: ({"nq": SimpleNamespace(vah_val_tolerance=5.0)}, None, None),
    )
    client._overnight_direction = lambda symbol, trade_date: "up"

    monday_short = {
        "date": "2026-08-03",
        "leg_key": "nq",
        "direction": "short",
        "liquidity_context": "custom_low low sweep near VA",
    }
    assert client._filter_state(monday_short)["rule"] == "CANDIDATE_RULE_1"

    wednesday = {**monday_short, "date": "2026-08-05", "direction": "long"}
    assert client._filter_state(wednesday)["rule"] == "CANDIDATE_RULE_2"

    client._overnight_direction = lambda symbol, trade_date: "down"
    assert client._filter_state(wednesday)["state"] == "ALLOW"


def test_super1_filter_short_circuits_unmatched_rule_before_broker_feature(monkeypatch) -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client._super1_record = record("custom_low", 20030.0)
    monkeypatch.setattr(
        MODULE.core,
        "live_strategy_objects",
        lambda: ({"nq": SimpleNamespace(vah_val_tolerance=5.0)}, None, None),
    )

    def unexpected_broker_call(symbol, trade_date):
        raise AssertionError("overnight broker feature should have been short-circuited")

    client._overnight_direction = unexpected_broker_call
    decision = {
        "date": "2026-08-05",
        "leg_key": "nq",
        "direction": "long",
        "liquidity_context": "custom_low low sweep away from VA",
    }

    assert client._filter_state(decision)["state"] == "ALLOW"


def test_super1_place_candidate_preserves_block_filter_details_without_typeerror(
    monkeypatch, tmp_path: Path
) -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        client,
        "_filter_state",
        lambda decision: {
            "state": "BLOCK",
            "rule": "CANDIDATE_RULE_1",
            "conditions": {"entry_weekday": "Monday"},
            "features": {"entry_weekday": "Monday"},
        },
    )
    monkeypatch.setattr(client, "_broker_objects", lambda comment: [])

    result = client._place_candidate(
        tmp_path,
        {
            "order_id": "blocked-order",
            "leg_key": "nq",
            "direction": "short",
        },
        "US100Cash",
        3.0,
    )

    assert result["state"] == "SUPER1_FILTER_BLOCKED"
    assert result["filter_state"]["rule"] == "CANDIDATE_RULE_1"


def test_super1_real_overlay_risk_request_reaches_controlled_broker_boundary(
    monkeypatch, tmp_path: Path, request
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    class OverlayMt5:
        ACCOUNT_TRADE_MODE_DEMO = 0
        TRADE_ACTION_PENDING = 5
        ORDER_TYPE_BUY_LIMIT = 2
        ORDER_TYPE_SELL_LIMIT = 3
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_FILLING_RETURN = 2
        TRADE_RETCODE_PLACED = 10008
        TRADE_RETCODE_DONE = 10009
        ORDER_TIME_SPECIFIED = 2

        def __init__(self) -> None:
            self.sent = 0
            self.pending = []

        def initialize(self, **kwargs):
            return kwargs["login"] == [REDACTED and kwargs["server"] == "XMGlobal-MT5 2"

        def shutdown(self):
            pass

        def last_error(self):
            return (0, "ok")

        def account_info(self):
            return SimpleNamespace(
                login=[REDACTED,
                server="XMGlobal-MT5 2",
                company="XM Global Limited",
                trade_mode=0,
                trade_allowed=True,
                trade_expert=True,
                equity=10_000.0,
            )

        def terminal_info(self):
            return SimpleNamespace(
                connected=True,
                trade_allowed=True,
                tradeapi_disabled=False,
            )

        def symbol_select(self, symbol, selected):
            return selected

        def symbol_info(self, symbol):
            return SimpleNamespace(
                digits=2,
                point=0.01,
                trade_tick_size=0.01,
                trade_stops_level=0,
                volume_min=0.1,
                volume_step=0.1,
                volume_max=10.0,
            )

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=99.0, ask=101.0)

        def order_calc_profit(self, order_type, symbol, volume, entry, stop):
            return -100.0

        def orders_get(self, ticket=None):
            return tuple(self.pending)

        def positions_get(self):
            return ()

        def history_orders_get(self, start, end):
            return ()

        def history_deals_get(self, start, end):
            return ()

        def order_check(self, request):
            return SimpleNamespace(retcode=0, comment="Done")

        def order_send(self, request):
            self.sent += 1
            self.pending = [
                SimpleNamespace(ticket=700, magic=request["magic"], comment=request["comment"])
            ]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=700, deal=0)

    configs = {
        "nq": SimpleNamespace(
            timeframe="3m", symbol="US100Cash", trade_window_start="09:30",
            trade_window_end="10:30", reward_r=3.0, vah_val_tolerance=5.0,
        ),
        "spx": SimpleNamespace(
            timeframe="5m", symbol="US500Cash", trade_window_start="09:30",
            trade_window_end="11:00", reward_r=2.5, vah_val_tolerance=5.0,
        ),
    }
    monkeypatch.setattr(MODULE.core, "live_strategy_objects", lambda: (configs, None, None))
    monkeypatch.setattr(MODULE.core, "RUNTIME_CONFIG", MODULE.RUNTIME_CONFIG)
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.mt5 = OverlayMt5()
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client.login_id = int(client.config["account_login"])
    client.server = client.config["expected_server"]
    client.password = ""
    client.terminal_path = ""
    client.connected = False
    client.demo_verified = False
    client.account = None
    client.terminal = None
    client.magic = int(client.config["magic_number"])
    client._super1_record = record("custom_low", 20030.0)

    decision = {
        "order_id": "overlay-order",
        "thesis_id": "overlay-thesis",
        "leg_key": "nq",
        "date": "2026-08-05",
        "direction": "long",
        "liquidity_context": "custom_low low sweep away from VA",
        "stop_price": 90.0,
        "target_price": 120.0,
        "fvg_time": "2026-08-05T13:00:00Z",
        "fvg_known_time": "2026-08-05T13:03:00Z",
        "setup_state": "VALID",
        "order_state": "CANCELLED",
        "terminal_reason": "CANCELLED_TRADE_WINDOW_END",
        "pair_cap_state": "ALLOWED",
        "intrabar_ambiguity": "",
    }
    prefix_path = tmp_path / "overlay-prefix.json"
    prefix_path.write_text(
        json.dumps(
            {
                "date": "2026-08-05",
                "state": "VALID",
                "deterministic_rerun": True,
                "invariant_errors": [],
                "cutoffs": {
                    "nq": "2026-08-05T10:27:00-04:00",
                    "spx": "2026-08-05T10:25:00-04:00",
                },
                "hashes": {
                    "config": "config-hash",
                    "code": MODULE.core.source_code_hash(),
                },
                "payload": {
                    **record("custom_low", 20030.0)["payload"],
                    "decisions": [decision],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: MODULE.pd.Timestamp("2026-08-05T14:28:00Z"))

    result = client.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        MODULE.pd.Timestamp("2026-08-05T14:28:00Z"),
        {"live_config_hash": "config-hash"},
    )

    assert result["state"] == "RECONCILED"
    assert result["results"][0]["state"] == "SUBMITTED"
    assert result["results"][0]["risk_state"]["risk_scale"] == 1.1
    assert client.mt5.sent == 1
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize(
    ("scenario", "trade_date", "direction", "liquidity_price", "overnight_rows", "expected_state", "expected_sent"),
    [
        ("monday-short", "2026-08-03", "short", 20004.0, [], "SUPER1_FILTER_BLOCKED", 0),
        (
            "overnight-up-block",
            "2026-08-05",
            "long",
            20004.0,
            [
                ("2026-08-04T19:59:00Z", 100.0, 100.0),
                ("2026-08-05T13:30:00Z", 101.0, 101.0),
            ],
            "SUPER1_FILTER_BLOCKED",
            0,
        ),
        (
            "proven-overnight-allow",
            "2026-08-05",
            "long",
            20004.0,
            [
                ("2026-08-04T19:59:00Z", 101.0, 101.0),
                ("2026-08-05T13:30:00Z", 100.0, 100.0),
            ],
            "SUBMITTED",
            1,
        ),
        (
            "missing-previous-rth-close",
            "2026-08-05",
            "long",
            20004.0,
            [("2026-08-05T13:30:00Z", 101.0, 101.0)],
            "SUPER1_FILTER_UNRESOLVED_NO_SEND",
            0,
        ),
    ],
)
def test_super1_c02_full_filter_risk_prefix_ledger_and_sdk_boundary(
    scenario,
    trade_date,
    direction,
    liquidity_price,
    overnight_rows,
    expected_state,
    expected_sent,
    monkeypatch,
    tmp_path: Path,
    request,
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    class FullBoundaryMt5:
        ACCOUNT_TRADE_MODE_DEMO = 0
        TRADE_ACTION_PENDING = 5
        ORDER_TYPE_BUY_LIMIT = 2
        ORDER_TYPE_SELL_LIMIT = 3
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_FILLING_RETURN = 2
        ORDER_TIME_SPECIFIED = 2
        TRADE_RETCODE_PLACED = 10008
        TRADE_RETCODE_DONE = 10009

        def __init__(self) -> None:
            self.sent = 0
            self.pending = []
            self.last_request = None

        def initialize(self, **kwargs):
            return kwargs["login"] == [REDACTED and kwargs["server"] == "XMGlobal-MT5 2"

        def shutdown(self):
            pass

        def last_error(self):
            return (0, "ok")

        def account_info(self):
            return SimpleNamespace(
                login=[REDACTED,
                server="XMGlobal-MT5 2",
                company="XM Global Limited",
                trade_mode=0,
                trade_allowed=True,
                trade_expert=True,
                equity=10_000.0,
            )

        def terminal_info(self):
            return SimpleNamespace(connected=True, trade_allowed=True, tradeapi_disabled=False)

        def symbol_select(self, symbol, selected):
            return selected

        def symbol_info(self, symbol):
            return SimpleNamespace(
                digits=2,
                point=0.01,
                trade_tick_size=0.01,
                trade_stops_level=0,
                volume_min=0.1,
                volume_step=0.1,
                volume_max=10.0,
            )

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=99.0, ask=101.0)

        def order_calc_profit(self, order_type, symbol, volume, entry, stop):
            return -100.0

        def order_check(self, request):
            return SimpleNamespace(retcode=0, comment="Done")

        def order_send(self, request):
            assert request["action"] == self.TRADE_ACTION_PENDING
            self.last_request = dict(request)
            self.sent += 1
            self.pending = [SimpleNamespace(
                ticket=900 + self.sent,
                magic=request["magic"],
                comment=request["comment"],
                symbol=request["symbol"],
                type=request["type"],
                volume_initial=request["volume"],
                volume_current=request["volume"],
                price_open=request["price"],
                sl=request["sl"],
                tp=request["tp"],
                time_expiration=request["expiration"],
            )]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=900 + self.sent, deal=0)

        def orders_get(self, ticket=None):
            return tuple(
                item for item in self.pending
                if ticket is None or int(item.ticket) == int(ticket)
            )

        def positions_get(self, ticket=None):
            return ()

        def history_orders_get(self, *args, **kwargs):
            del args, kwargs
            return ()

        def history_deals_get(self, *args, **kwargs):
            del args, kwargs
            return ()

    config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    configs = {
        "nq": SimpleNamespace(
            timeframe="3m", symbol="US100Cash", trade_window_start="09:30",
            trade_window_end="10:30", reward_r=3.0, vah_val_tolerance=5.0,
        ),
        "spx": SimpleNamespace(
            timeframe="5m", symbol="US500Cash", trade_window_start="09:30",
            trade_window_end="11:00", reward_r=2.5, vah_val_tolerance=5.0,
        ),
    }
    monkeypatch.setattr(MODULE.core, "live_strategy_objects", lambda: (configs, None, None))
    monkeypatch.setattr(MODULE.core, "runtime_config", lambda: config)
    now = MODULE.pd.Timestamp(f"{trade_date}T14:28:00Z")
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: now)

    mt5 = FullBoundaryMt5()
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.mt5 = mt5
    client.config = config
    client.login_id = int(config["account_login"])
    client.server = config["expected_server"]
    client.password = ""
    client.terminal_path = ""
    client.connected = False
    client.demo_verified = False
    client.account = None
    client.terminal = None
    client.magic = int(config["magic_number"])
    client._super1_record = None
    client.prices = lambda symbol, start, end: (
        end,
        [
            {
                "snapshotTimeUTC": timestamp,
                "openPrice": {"bid": opening},
                "closePrice": {"bid": closing},
            }
            for timestamp, opening, closing in overnight_rows
        ],
    )

    candidate = {
        "order_id": f"c02-{scenario}",
        "thesis_id": f"thesis-{scenario}",
        "leg_key": "nq",
        "date": trade_date,
        "direction": direction,
        "liquidity_context": "custom_low low sweep near VA",
        "stop_price": 90.0 if direction == "long" else 130.0,
        "target_price": 120.0 if direction == "long" else 90.0,
        "fvg_time": f"{trade_date}T13:00:00Z",
        "fvg_known_time": f"{trade_date}T13:03:00Z",
        "setup_state": "VALID",
        "order_state": "CANCELLED",
        "terminal_reason": "CANCELLED_TRADE_WINDOW_END",
        "pair_cap_state": "ALLOWED",
        "intrabar_ambiguity": "",
    }
    sealed = record("custom_low", liquidity_price)
    sealed["date"] = trade_date
    sealed["payload"]["decisions"] = [candidate]
    prefix = tmp_path / "sealed-prefix.json"
    prefix.write_text(json.dumps({
        "date": trade_date,
        "state": "VALID",
        "deterministic_rerun": True,
        "invariant_errors": [],
        "cutoffs": {
            "nq": f"{trade_date}T10:27:00-04:00",
            "spx": f"{trade_date}T10:25:00-04:00",
        },
        "hashes": {"config": "config-hash", "code": MODULE.core.source_code_hash()},
        "payload": sealed["payload"],
    }), encoding="utf-8")

    result = client.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix)},
        now,
        {"live_config_hash": "config-hash"},
    )

    assert result["state"] == "RECONCILED"
    assert result["candidate_count"] == 1
    assert result["results"][0]["state"] == expected_state
    assert mt5.sent == expected_sent
    item = result["results"][0]
    if scenario == "monday-short":
        assert item["filter_state"] == {
            "state": "BLOCK",
            "rule": "CANDIDATE_RULE_1",
            "conditions": {"entry_weekday": "Monday", "direction": "short"},
            "features": {"entry_weekday": "Monday", "direction": "short"},
        }
    elif scenario == "overnight-up-block":
        assert item["filter_state"]["state"] == "BLOCK"
        assert item["filter_state"]["rule"] == "CANDIDATE_RULE_2"
        assert item["filter_state"]["features"] == {
            "entry_weekday": "Wednesday",
            "direction": "long",
            "liquidity_type": "vah_val_proximity",
            "overnight_direction": "up",
        }
    elif scenario == "proven-overnight-allow":
        assert item["filter_state"] == {
            "state": "ALLOW",
            "rule": None,
            "features": {
                "entry_weekday": "Wednesday",
                "direction": "long",
                "liquidity_type": "vah_val_proximity",
                "overnight_direction": "down",
            },
        }
    else:
        assert item["filter_state"] == {
            "state": "UNRESOLVED",
            "rule": None,
            "features": {"entry_weekday": "Wednesday", "direction": "long"},
        }
    events = client._events(tmp_path)
    if expected_sent:
        assert any(item["event"] == "SUPER1_RISK_SIZING" for item in events)
        assert client._intent_state(tmp_path, candidate["order_id"])["status"] == "SUBMITTED"
        assert len(mt5.pending) == 1
        broker_request = mt5.last_request
        assert broker_request == {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": "US100Cash",
            "volume": 1.1,
            "type": mt5.ORDER_TYPE_BUY_LIMIT,
            "price": 97.5,
            "sl": 90.0,
            "tp": 120.0,
            "deviation": config["deviation_points"],
            "magic": config["magic_number"],
            "comment": client._comment("nq", candidate["order_id"]),
            "type_time": mt5.ORDER_TIME_SPECIFIED,
                "expiration": int(MODULE.pd.Timestamp("2026-08-05 10:30", tz=MODULE.core.TZ).tz_convert("UTC").timestamp()),
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        repeated = client.reconcile_orders(
            tmp_path,
            {"state": "VALID", "path": str(prefix)},
            now,
            {"live_config_hash": "config-hash"},
        )
        assert repeated["results"][0]["state"] == "IDEMPOTENT_ALREADY_SUBMITTED"
        assert mt5.sent == 1
        restarted = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
        restarted.mt5 = mt5
        restarted.config = config
        restarted.login_id = int(config["account_login"])
        restarted.server = config["expected_server"]
        restarted.password = ""
        restarted.terminal_path = ""
        restarted.connected = False
        restarted.demo_verified = False
        restarted.account = None
        restarted.terminal = None
        restarted.magic = int(config["magic_number"])
        restarted._super1_record = None
        restarted.prices = client.prices
        repeated_new_client = restarted.reconcile_orders(
            tmp_path,
            {"state": "VALID", "path": str(prefix)},
            now,
            {"live_config_hash": "config-hash"},
        )
        assert repeated_new_client["results"][0]["state"] == "IDEMPOTENT_ALREADY_SUBMITTED"
        assert mt5.sent == 1
        assert len(mt5.pending) == 1
    else:
        assert not any(item["event"] == "SUBMITTED" for item in events)
        assert not mt5.pending
    record_if_enabled(request, evidence_token)


def test_t01_full_two_leg_fetch_aggregation_prefix_decision_and_real_reconcile(
    monkeypatch,
    tmp_path: Path,
    request,
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    # Other forward test modules load the shared core with different harness
    # settings during collection; this scenario is explicitly NY-time.
    monkeypatch.setattr(MODULE.core, "TZ", "America/New_York")
    runtime = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    runtime["history_days"] = 1
    runtime["legs"]["nq"]["epic"] = "US100"
    runtime["legs"]["spx"]["epic"] = "US500"
    now = MODULE.pd.Timestamp("2026-08-05T14:25:00Z")
    profile_start = MODULE.pd.Timestamp("2026-08-04 18:00:00", tz=MODULE.core.TZ)
    source_times = [
        item
        for item in MODULE.pd.date_range(
            profile_start,
            now.tz_convert(MODULE.core.TZ) - MODULE.pd.Timedelta(minutes=1),
            freq="1min",
        )
        if not (item.strftime("%H:%M") >= "20:00" and item.strftime("%H:%M") < "21:00")
    ]
    def in_bucket(item, clock: str) -> bool:
        start = MODULE.pd.Timestamp(f"2026-08-05 {clock}", tz=MODULE.core.TZ)
        return start <= item < start + MODULE.pd.Timedelta(minutes=3)

    rows = {
        epic: [
            {
                "snapshotTimeUTC": item.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S"),
                "openPrice": {"bid": (
                    90.0 if in_bucket(item, "09:30")
                    else 103.0 if in_bucket(item, "09:36")
                    else 99.0 if in_bucket(item, "09:39")
                    else 104.0 if in_bucket(item, "09:42")
                    else 106.5 if in_bucket(item, "09:45")
                    else 100.0 + index * 0.001
                )},
                "highPrice": {"bid": (
                    91.0 if in_bucket(item, "09:30")
                    else 104.0 if in_bucket(item, "09:36")
                    else 104.5 if in_bucket(item, "09:39")
                    else 105.5 if in_bucket(item, "09:42")
                    else 107.0 if in_bucket(item, "09:45")
                    else 100.4 + index * 0.001
                )},
                "lowPrice": {"bid": (
                    89.0 if in_bucket(item, "09:30")
                    else 98.0 if in_bucket(item, "09:36")
                    else 98.5 if in_bucket(item, "09:39")
                    else 103.5 if in_bucket(item, "09:42")
                    else 106.5 if in_bucket(item, "09:45")
                    else 99.6 + index * 0.001
                )},
                "closePrice": {"bid": (
                    90.0 if in_bucket(item, "09:30")
                    else 99.0 if in_bucket(item, "09:36")
                    else 104.0 if in_bucket(item, "09:39")
                    else 105.0 if in_bucket(item, "09:42")
                    else 106.8 if in_bucket(item, "09:45")
                    else 100.2 + index * 0.001
                )},
                "lastTradedVolume": 1,
            }
            for index, item in enumerate(source_times)
        ]
        for epic in ("US100", "US500")
    }

    class TwoLegClient:
        def prices(self, epic, start, end):
            selected = [
                item
                for item in rows[epic]
                if start <= MODULE.pd.Timestamp(item["snapshotTimeUTC"], tz="UTC") < end
            ]
            observed = (
                MODULE.pd.Timestamp("2026-08-05 10:23:50", tz=MODULE.core.TZ)
                if epic == "US100"
                else MODULE.pd.Timestamp("2026-08-05 10:24:10", tz=MODULE.core.TZ)
            )
            return observed, selected

    monkeypatch.setattr(MODULE.core, "runtime_config", lambda: runtime)
    monkeypatch.setattr(
        MODULE.core,
        "RUNTIME_CONFIG",
        ROOT / "live_forward" / "capital_demo_config.json",
    )
    monkeypatch.setattr(
        MODULE.core,
        "campaign_lock",
        lambda output_root: {"live_config_hash": "t01-config"},
    )
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: now)
    MODULE.core.install_xm_scheduled_gap_integrity()

    store = MODULE.core.BarStore(tmp_path)
    try:
        fetches = {
            key: MODULE.core.fetch_window(
                TwoLegClient(),
                store,
                tmp_path,
                runtime["legs"][key]["epic"],
                profile_start,
                now,
            )
            for key in MODULE.core.LEG_ORDER
        }
        assert all(fetch["inserted"] > 0 for fetch in fetches.values())
        assert MODULE.pd.Timestamp(fetches["nq"]["provider_observation_at"]) == MODULE.pd.Timestamp(
            "2026-08-05 10:23:50", tz=MODULE.core.TZ
        ).tz_convert("UTC")
        assert MODULE.pd.Timestamp(fetches["spx"]["provider_observation_at"]) == MODULE.pd.Timestamp(
            "2026-08-05 10:24:10", tz=MODULE.core.TZ
        ).tz_convert("UTC")
        stale_prefix = MODULE.core.run_prefix(
            tmp_path, store, now, now.tz_convert(MODULE.core.UTC)
        )
        assert stale_prefix["state"] == "VALID"
        stale_record = MODULE.core.read_json(Path(stale_prefix["path"]))
        assert stale_record["state"] == "VALID"
        assert len(stale_record["payload"]["decisions"]) == 1
        candidate = stale_record["payload"]["decisions"][0]
        assert candidate["leg_key"] == "nq"
        assert candidate["order_state"] == "CANCELLED"
        assert candidate["terminal_reason"] == "CANCELLED_TRADE_WINDOW_END"
        assert candidate["order_id"]

        fresh_prefix = stale_prefix
        sealed_prefix = MODULE.core.read_json(Path(fresh_prefix["path"]))
        assert len(sealed_prefix["payload"]["decisions"]) == 1
        assert sealed_prefix["payload"]["decisions"][0]["order_id"] == candidate["order_id"]

        class ReconcileMt5:
            ACCOUNT_TRADE_MODE_DEMO = 0

            def initialize(self, **kwargs):
                return True

            def shutdown(self):
                pass

            def last_error(self):
                return (0, "ok")

            def account_info(self):
                return SimpleNamespace(
                    login=[REDACTED,
                    server="XMGlobal-MT5 2",
                    company="XM Global Limited",
                    trade_mode=0,
                    trade_allowed=True,
                    trade_expert=True,
                    equity=10_000.0,
                )

            def terminal_info(self):
                return SimpleNamespace(connected=True, trade_allowed=True, tradeapi_disabled=False)

            TRADE_ACTION_PENDING = 5
            TRADE_ACTION_REMOVE = 8
            ORDER_TYPE_BUY = 0
            ORDER_TYPE_SELL = 1
            ORDER_TYPE_BUY_LIMIT = 2
            ORDER_TYPE_SELL_LIMIT = 3
            ORDER_FILLING_RETURN = 2
            ORDER_TIME_SPECIFIED = 2
            TRADE_RETCODE_PLACED = 10008

            def __init__(self):
                self.pending = []
                self.sent = 0

            def symbol_select(self, symbol, selected):
                return selected

            def symbol_info(self, symbol):
                return SimpleNamespace(
                    digits=2, point=0.01, trade_tick_size=0.01,
                    trade_stops_level=0, volume_min=0.1,
                    volume_step=0.1, volume_max=10.0,
                )

            def symbol_info_tick(self, symbol):
                return SimpleNamespace(bid=100.0, ask=200.0)

            def order_calc_profit(self, order_type, symbol, volume, entry, stop):
                return -100.0

            def order_check(self, request):
                return SimpleNamespace(retcode=0, comment="Done")

            def order_send(self, request):
                assert request["action"] == self.TRADE_ACTION_PENDING
                self.sent += 1
                ticket = 800 + self.sent
                self.pending = [SimpleNamespace(
                    ticket=ticket, magic=request["magic"], comment=request["comment"],
                    symbol=request["symbol"], type=request["type"],
                    volume_initial=request["volume"], volume_current=request["volume"],
                    price_open=request["price"], sl=request["sl"], tp=request["tp"],
                    time_expiration=request["expiration"],
                )]
                return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=ticket, deal=0)

            def orders_get(self, ticket=None):
                return tuple(item for item in self.pending if ticket is None or item.ticket == ticket)

            def positions_get(self, ticket=None):
                return ()

            def history_orders_get(self, *args, **kwargs):
                return ()

            def history_deals_get(self, *args, **kwargs):
                return ()

        config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
        config["legs"]["nq"]["epic"] = "US100"
        config["legs"]["spx"]["epic"] = "US500"
        client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
        client.mt5 = ReconcileMt5()
        client.config = config
        client.login_id = int(config["account_login"])
        client.server = config["expected_server"]
        client.password = ""
        client.terminal_path = ""
        client.connected = False
        client.demo_verified = False
        client.account = None
        client.terminal = None
        client.magic = int(config["magic_number"])
        stale_now = MODULE.pd.Timestamp("2026-08-05T14:29:00Z")
        monkeypatch.setattr(MODULE.core, "utc_now", lambda: stale_now)
        stale_result = client.reconcile_orders(
            tmp_path,
            {"state": "VALID", "path": str(Path(stale_prefix["path"]))},
            stale_now,
            {"live_config_hash": "t01-config"},
        )
        assert stale_result["candidate_count"] == 1
        assert stale_result["results"][0]["state"] == "STALE_PREFIX_NO_SEND"
        assert client.mt5.sent == 0
        assert not client.mt5.pending
        assert client._intent_state(tmp_path, candidate["order_id"])["status"] == "PRE_SEND_DEFERRED"

        monkeypatch.setattr(MODULE.core, "utc_now", lambda: now)
        fresh_result = client.reconcile_orders(
            tmp_path,
            {"state": "VALID", "path": str(Path(fresh_prefix["path"]))},
            now,
            {"live_config_hash": "t01-config"},
        )
        assert fresh_result["results"][0]["state"] == "SUBMITTED"
        assert client.mt5.sent == 1
        assert len(client.mt5.pending) == 1
        pending = client.mt5.pending[0]
        assert pending.comment.startswith("SUPER1:")
        assert client._intent_state(tmp_path, candidate["order_id"])["status"] == "SUBMITTED"
        assert any(
            item.get("event") == "SUBMITTED" and item.get("order_id") == candidate["order_id"]
            for item in client._events(tmp_path)
        )

        restarted = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
        restarted.mt5 = client.mt5
        restarted.config = config
        restarted.login_id = int(config["account_login"])
        restarted.server = config["expected_server"]
        restarted.password = ""
        restarted.terminal_path = ""
        restarted.connected = False
        restarted.demo_verified = False
        restarted.account = None
        restarted.terminal = None
        restarted.magic = int(config["magic_number"])
        repeated = restarted.reconcile_orders(
            tmp_path,
            {"state": "VALID", "path": str(Path(fresh_prefix["path"]))},
            now,
            {"live_config_hash": "t01-config"},
        )
        assert repeated["results"][0]["state"] == "IDEMPOTENT_ALREADY_SUBMITTED"
        assert client.mt5.sent == 1
        assert len(client.mt5.pending) == 1
    finally:
        store.close()
    record_if_enabled(request, evidence_token)


def test_super1_reconcile_uses_fresh_final_guard_and_preserves_later_spx_candidate(
    monkeypatch, tmp_path: Path, request
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    class Clock:
        now = MODULE.pd.Timestamp("2026-08-05T14:29:59Z")
        moved = False

    class BoundaryMt5:
        ACCOUNT_TRADE_MODE_DEMO = 0
        TRADE_ACTION_PENDING = 5
        TRADE_ACTION_REMOVE = 8
        ORDER_TYPE_BUY_LIMIT = 2
        ORDER_TYPE_SELL_LIMIT = 3
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        POSITION_TYPE_BUY = 0
        POSITION_TYPE_SELL = 1
        DEAL_ENTRY_IN = 0
        DEAL_ENTRY_OUT = 1
        ORDER_FILLING_RETURN = 2
        ORDER_TIME_SPECIFIED = 2
        TRADE_RETCODE_PLACED = 10008
        TRADE_RETCODE_DONE = 10009

        def __init__(self) -> None:
            self.sent = 0
            self.pending = []

        def initialize(self, **kwargs):
            return kwargs["login"] == [REDACTED and kwargs["server"] == "XMGlobal-MT5 2"

        def shutdown(self):
            pass

        def account_info(self):
            return SimpleNamespace(
                login=[REDACTED,
                server="XMGlobal-MT5 2",
                company="XM Global Limited",
                trade_mode=0,
                trade_allowed=True,
                trade_expert=True,
                equity=10000.0,
            )

        def terminal_info(self):
            return SimpleNamespace(
                connected=True,
                trade_allowed=True,
                tradeapi_disabled=False,
            )

        def last_error(self):
            return (0, "ok")

        def symbol_select(self, symbol, selected):
            return selected

        def symbol_info(self, symbol):
            return SimpleNamespace(
                digits=2,
                point=0.01,
                trade_tick_size=0.01,
                trade_stops_level=0,
                volume_min=0.1,
                volume_step=0.1,
                volume_max=10.0,
            )

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=100.0, ask=101.0)

        def order_calc_profit(self, order_type, symbol, volume, entry, stop):
            return -100.0

        def order_check(self, request):
            if not Clock.moved:
                Clock.moved = True
                Clock.now = MODULE.pd.Timestamp("2026-08-05T14:30:01Z")
            return SimpleNamespace(retcode=0, comment="Done")

        def order_send(self, request):
            if request["action"] != self.TRADE_ACTION_PENDING:
                self.pending = []
                return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=request["order"], deal=0)
            self.sent += 1
            self.pending = [SimpleNamespace(
                ticket=700 + self.sent,
                magic=request["magic"],
                comment=request["comment"],
                symbol=request["symbol"],
                type=request["type"],
                volume_initial=request["volume"],
                volume_current=request["volume"],
                price_open=request["price"],
                sl=request["sl"],
                tp=request["tp"],
                time_expiration=request["expiration"],
            )]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=700 + self.sent, deal=0)

        def orders_get(self, ticket=None):
            return tuple(
                item for item in self.pending
                if ticket is None or int(item.ticket) == int(ticket)
            )

        def positions_get(self, ticket=None):
            return ()

        def history_orders_get(self, start=None, end=None, *, ticket=None, position=None):
            return ()

        def history_deals_get(self, start=None, end=None, *, ticket=None, position=None):
            return ()

    configs = {
        "nq": SimpleNamespace(timeframe="3m", symbol="US100Cash", trade_window_start="09:30", trade_window_end="10:30", reward_r=3.0, vah_val_tolerance=5.0),
        "spx": SimpleNamespace(timeframe="5m", symbol="US500Cash", trade_window_start="09:30", trade_window_end="11:00", reward_r=2.5, vah_val_tolerance=5.0),
    }
    monkeypatch.setattr(MODULE.core, "live_strategy_objects", lambda: (configs, None, None))
    monkeypatch.setattr(MODULE.core, "RUNTIME_CONFIG", MODULE.RUNTIME_CONFIG)
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: Clock.now)
    mt5 = BoundaryMt5()
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.mt5 = mt5
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client.login_id = int(client.config["account_login"])
    client.server = client.config["expected_server"]
    client.password = ""
    client.terminal_path = ""
    client.connected = False
    client.demo_verified = False
    client.account = None
    client.terminal = None
    client.magic = int(client.config["magic_number"])
    client._filter_state = lambda decision: {"state": "ALLOW", "rule": None, "features": {}}
    client._terminal_r_state = lambda: {"risk_scale": 1.0, "state": "NONNEGATIVE"}

    def write_prefix(path: Path, decision: dict, cutoffs: dict[str, str]) -> None:
        path.write_text(json.dumps({
            "date": "2026-08-05",
            "state": "VALID",
            "deterministic_rerun": True,
            "invariant_errors": [],
            "cutoffs": cutoffs,
            "hashes": {"config": "config-hash", "code": MODULE.core.source_code_hash()},
            "payload": {"decisions": [decision]},
        }), encoding="utf-8")

    nq = {
        "order_id": "nq-boundary",
        "thesis_id": "nq-thesis",
        "leg_key": "nq",
        "date": "2026-08-05",
        "direction": "long",
        "stop_price": 90.0,
        "target_price": 120.0,
        "fvg_time": "2026-08-05T13:00:00Z",
        "fvg_known_time": "2026-08-05T13:03:00Z",
        "setup_state": "VALID",
        "order_state": "CANCELLED",
        "terminal_reason": "CANCELLED_TRADE_WINDOW_END",
        "pair_cap_state": "ALLOWED",
        "intrabar_ambiguity": "",
        "liquidity_context": "custom_low low sweep away from VA",
    }
    prefix = tmp_path / "prefix.json"
    write_prefix(prefix, nq, {"nq": "2026-08-05T10:27:00-04:00", "spx": "2026-08-05T10:25:00-04:00"})
    first = client.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix)},
        Clock.now,
        {"live_config_hash": "config-hash"},
    )
    assert first["results"][0]["state"] == "WINDOW_EXPIRED_NO_SEND"
    assert mt5.sent == 0
    assert client._intent_state(tmp_path, "nq-boundary")["status"] == "WINDOW_EXPIRED"

    Clock.now = MODULE.pd.Timestamp("2026-08-05T14:35:00Z")
    spx = {**nq, "order_id": "spx-boundary", "thesis_id": "spx-thesis", "leg_key": "spx"}
    write_prefix(prefix, spx, {"nq": "2026-08-05T10:30:00-04:00", "spx": "2026-08-05T10:30:00-04:00"})
    second = client.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix)},
        Clock.now,
        {"live_config_hash": "config-hash"},
    )
    assert second["results"][0]["state"] == "SUBMITTED"
    assert mt5.sent == 1
    record_if_enabled(request, evidence_token)


def test_overnight_direction_consumes_verified_adapter_prices(monkeypatch) -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    seen = []

    def verified_prices(symbol, start, end):
        seen.append(symbol)
        return end, [
            {
                "snapshotTimeUTC": "2026-08-04T19:59:00Z",
                "openPrice": {"bid": 100.0},
                "closePrice": {"bid": 100.0},
            },
            {
                "snapshotTimeUTC": "2026-08-05T13:30:00Z",
                "openPrice": {"bid": 101.0},
                "closePrice": {"bid": 101.0},
                "verifiedNoTick": True,
            },
        ]

    client.prices = verified_prices
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: MODULE.pd.Timestamp("2026-08-05T14:00:00Z"))

    assert client._overnight_direction("US100Cash", "2026-08-05") == "up"
    assert seen == ["US100Cash"]


def test_overnight_direction_rejects_missing_previous_rth_close(monkeypatch) -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client.prices = lambda symbol, start, end: (
        end,
        [
            {
                "snapshotTimeUTC": "2026-08-04T19:58:00Z",
                "openPrice": {"bid": 100.0},
                "closePrice": {"bid": 100.0},
            },
            {
                "snapshotTimeUTC": "2026-08-05T13:30:00Z",
                "openPrice": {"bid": 101.0},
                "closePrice": {"bid": 101.0},
            },
        ],
    )
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: MODULE.pd.Timestamp("2026-08-05T14:00:00Z"))

    with pytest.raises(MODULE.Super1FeatureError, match="cannot be proven"):
        client._overnight_direction("US100Cash", "2026-08-05")


def test_overnight_direction_does_not_fall_back_to_an_older_session(monkeypatch) -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client.prices = lambda symbol, start, end: (
        end,
        [
            {
                "snapshotTimeUTC": "2026-08-03T19:59:00Z",
                "openPrice": {"bid": 100.0},
                "closePrice": {"bid": 100.0},
            },
            {
                "snapshotTimeUTC": "2026-08-05T13:30:00Z",
                "openPrice": {"bid": 101.0},
                "closePrice": {"bid": 101.0},
            },
        ],
    )
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: MODULE.pd.Timestamp("2026-08-05T14:00:00Z"))

    with pytest.raises(MODULE.Super1FeatureError, match="cannot be proven"):
        client._overnight_direction("US100Cash", "2026-08-05")


def test_overnight_direction_requires_verified_rth_calendar(monkeypatch) -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = {"history_days": 10}
    client.prices = lambda symbol, start, end: (end, [])
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: MODULE.pd.Timestamp("2026-08-05T14:00:00Z"))

    with pytest.raises(MODULE.Super1FeatureError, match="calendar reference is missing"):
        client._overnight_direction("US100Cash", "2026-08-05")


def _make_filter_client(monkeypatch):
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    client.magic = int(client.config["magic_number"])
    client._super1_record = record("custom_low", 20004.0)
    monkeypatch.setattr(
        MODULE.core,
        "live_strategy_objects",
        lambda: ({"nq": SimpleNamespace(vah_val_tolerance=5.0)}, None, None),
    )
    return client


@pytest.mark.parametrize(
    "case",
    ["broker_execution_state", "current_order", "economic_outbox", "history_deal", "history_order", "order_intent", "position"],
    ids=lambda value: value,
)
def test_c02_filter_clean_gate_defers_to_base_lifecycle(case: str, tmp_path: Path, monkeypatch, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    client = _make_filter_client(monkeypatch)
    client._overnight_direction = lambda symbol, trade_date: "down"
    client._terminal_r_state = lambda: {"risk_scale": 1.1}
    broker_kind = {
        "broker_execution_state": "execution",
        "current_order": "order",
        "economic_outbox": "outbox",
        "history_deal": "deal",
        "history_order": "history",
        "order_intent": "intent",
        "position": "position",
    }[case]
    client._broker_objects = lambda comment: [(broker_kind, SimpleNamespace(ticket=7001))]
    decision = {
        "order_id": f"clean-{case}", "leg_key": "nq", "direction": "long",
        "date": "2026-08-05", "liquidity_context": "custom_low low sweep near VA",
    }
    with pytest.raises(MODULE.core.CriticalLiveError, match="broker object"):
        client._place_candidate(tmp_path, decision, "US100Cash", 3.0)
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize(
    "case",
    ["blocked_mismatch", "unresolved_newer_after_cutoff", "unresolved_newer_before_cutoff"],
    ids=lambda value: value,
)
def test_c02_filter_persistence_transition(case: str, tmp_path: Path, monkeypatch, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    client = _make_filter_client(monkeypatch)
    client._broker_objects = lambda comment: []
    state = "FILTER_BLOCKED" if case == "blocked_mismatch" else "FILTER_UNRESOLVED_DEFERRED"
    result = client._persist_filter_terminal(
        tmp_path,
        {"order_id": f"persist-{case}", "leg_key": "nq", "direction": "long"},
        {"state": "BLOCK" if state == "FILTER_BLOCKED" else "UNRESOLVED", "rule": None, "features": {}},
        state,
        f"C02_{case.upper()}",
        None,
    )
    assert result["persistent_state"] == state
    assert client._intent_state(tmp_path, f"persist-{case}")["status"] == state
    assert (tmp_path / "orders" / "events.jsonl").is_file()
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize(
    "case",
    ["cutoff_closed", "global_entry_blocked", "risk_blocked"],
    ids=lambda value: value,
)
def test_c02_promotion_rechecks_entry_gates(case: str, monkeypatch, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    client = _make_filter_client(monkeypatch)
    client._overnight_direction = lambda symbol, trade_date: "up"
    if case == "cutoff_closed":
        decision = {"date": "2026-08-03", "leg_key": "nq", "direction": "short", "liquidity_context": "custom_low low sweep near VA"}
        assert client._filter_state(decision)["state"] == "BLOCK"
    elif case == "global_entry_blocked":
        decision = {"date": "2026-08-05", "leg_key": "nq", "direction": "long", "liquidity_context": "custom_low low sweep near VA"}
        assert client._filter_state(decision)["rule"] == "CANDIDATE_RULE_2"
    else:
        with pytest.raises(MODULE.xm.CandidateNotExecutableError) as exc_info:
            client._aligned_volume(0.05, 0.1, 10.0, 0.1)
        assert exc_info.value.code == "RISK_BELOW_MINIMUM_VOLUME"
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize(
    "case",
    ["cutoff_regression", "observed_at_not_later", "raw_sha_unchanged", "revision_not_higher"],
    ids=lambda value: value,
)
def test_c02_unresolved_requires_strictly_newer_evidence(case: str, tmp_path: Path, monkeypatch, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    client = _make_filter_client(monkeypatch)
    client._broker_objects = lambda comment: []
    order_id = f"newer-{case}"
    decision = {
        "order_id": order_id,
        "leg_key": "nq",
        "direction": "long",
        "date": "2026-08-05",
    }
    initial_prefix = {
        "date": "2026-08-05",
        "recorded_at": "2026-08-05T14:20:00Z",
        "cutoffs": {
            "nq": "2026-08-05T10:26:00-04:00",
            "spx": "2026-08-05T10:24:00-04:00",
        },
    }
    initial_raw = b'{"prefix":"initial"}'
    initial = client._persist_filter_terminal(
        tmp_path,
        decision,
        {"state": "UNRESOLVED", "rule": None, "features": {"case": case}},
        "FILTER_UNRESOLVED_DEFERRED",
        "FILTER_UNRESOLVED_DEFERRED",
        initial_prefix,
        initial_raw,
    )
    assert initial["persistent_state"] == "FILTER_UNRESOLVED_DEFERRED"
    newer_prefix = {
        **initial_prefix,
        "recorded_at": "2026-08-05T14:30:00Z",
        "cutoffs": {
            "nq": "2026-08-05T10:27:00-04:00",
            "spx": "2026-08-05T10:25:00-04:00",
        },
    }
    newer_raw = b'{"prefix":"new"}'
    if case == "cutoff_regression":
        newer_prefix["cutoffs"]["nq"] = "2026-08-05T10:25:00-04:00"
    elif case == "observed_at_not_later":
        newer_prefix["recorded_at"] = initial_prefix["recorded_at"]
    elif case == "raw_sha_unchanged":
        newer_raw = initial_raw
    elif case == "revision_not_higher":
        newer_prefix["cutoffs"] = dict(initial_prefix["cutoffs"])

    promotion = client._promote_filter_if_newer(
        tmp_path,
        decision,
        {"state": "ALLOW", "rule": None, "features": {"case": case}},
        "NEWER_COMPLETE_FILTER_EVIDENCE",
        newer_prefix,
        newer_raw,
    )
    assert promotion["promoted"] is False
    assert client._intent_state(tmp_path, order_id)["status"] == "FILTER_UNRESOLVED_DEFERRED"
    event_rows = [
        json.loads(line)
        for line in (tmp_path / "orders" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event"] for row in event_rows] == ["SUPER1_FILTER_DEFERRED"]
    assert len(event_rows[-1]["filter_evidence_sha256"]) == 64
    evidence = sqlite3.connect(client._order_db(tmp_path))
    try:
        stored = evidence.execute(
            "SELECT state, raw_sha256 FROM strategy_filter_evidence WHERE strategy = 'Super1' AND order_id = ?",
            (order_id,),
        ).fetchone()
    finally:
        evidence.close()
    assert stored == (
        "UNRESOLVED",
        hashlib.sha256(initial_raw).hexdigest(),
    )
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize("window_open", [True, False], ids=["window-open", "window-closed"])
def test_c02_strictly_newer_filter_evidence_promotes_only_inside_window(
    window_open: bool, tmp_path: Path, monkeypatch, request
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    monkeypatch.setattr(MODULE.core, "TZ", "America/New_York")
    config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    configs = {
        "nq": SimpleNamespace(
            timeframe="3m", symbol="US100Cash", trade_window_start="09:30",
            trade_window_end="10:30", reward_r=3.0, vah_val_tolerance=5.0,
        ),
        "spx": SimpleNamespace(
            timeframe="5m", symbol="US500Cash", trade_window_start="09:30",
            trade_window_end="11:00", reward_r=2.5, vah_val_tolerance=5.0,
        ),
    }
    monkeypatch.setattr(MODULE.core, "live_strategy_objects", lambda: (configs, None, None))
    now = MODULE.pd.Timestamp("2026-08-05T14:28:00Z" if window_open else "2026-08-05T14:31:00Z")
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: now)

    class PromotionMt5:
        ACCOUNT_TRADE_MODE_DEMO = 0
        TRADE_ACTION_PENDING = 5
        ORDER_TYPE_BUY_LIMIT = 2
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_FILLING_RETURN = 2
        ORDER_TIME_SPECIFIED = 2
        TRADE_RETCODE_PLACED = 10008

        def __init__(self) -> None:
            self.sent = 0
            self.pending = []

        def account_info(self):
            return SimpleNamespace(
                login=[REDACTED, server="XMGlobal-MT5 2", company="XM Global Limited",
                trade_mode=0, trade_allowed=True, trade_expert=True, equity=10_000.0,
            )

        def terminal_info(self):
            return SimpleNamespace(connected=True, trade_allowed=True, tradeapi_disabled=False)

        def symbol_select(self, symbol, selected):
            return selected

        def symbol_info(self, symbol):
            return SimpleNamespace(
                digits=2, point=0.01, trade_tick_size=0.01, trade_stops_level=0,
                volume_min=0.1, volume_step=0.1, volume_max=10.0,
            )

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=99.0, ask=101.0)

        def order_calc_profit(self, order_type, symbol, volume, entry, stop):
            return -100.0

        def order_check(self, request):
            return SimpleNamespace(retcode=0, comment="Done")

        def order_send(self, request):
            self.sent += 1
            self.pending = [SimpleNamespace(
                ticket=900 + self.sent,
                magic=request["magic"], comment=request["comment"], symbol=request["symbol"],
                type=request["type"], volume_initial=request["volume"],
                volume_current=request["volume"], price_open=request["price"],
                sl=request["sl"], tp=request["tp"], time_expiration=request["expiration"],
            )]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=900 + self.sent, deal=0)

        def orders_get(self, ticket=None):
            return tuple(self.pending)

        def positions_get(self, ticket=None):
            return ()

        def history_orders_get(self, *args, **kwargs):
            return ()

        def history_deals_get(self, *args, **kwargs):
            return ()

        def initialize(self, **kwargs):
            return True

        def shutdown(self):
            pass

        def last_error(self):
            return (0, "ok")

    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.mt5 = PromotionMt5()
    client.config = config
    client.login_id = int(config["account_login"])
    client.server = config["expected_server"]
    client.password = ""
    client.terminal_path = ""
    client.connected = True
    client.demo_verified = True
    client.account = client.mt5.account_info()
    client.terminal = client.mt5.terminal_info()
    client.magic = int(config["magic_number"])
    client._super1_record = record("custom_low", 20004.0)
    client._filter_state = lambda decision: {
        "state": "ALLOW", "rule": None, "features": {"test": "strictly-newer"}
    }
    client._terminal_r_state = lambda: {"risk_scale": 1.0}

    decision = {
        "order_id": "promote-window-order", "thesis_id": "promote-thesis",
        "leg_key": "nq", "date": "2026-08-05", "direction": "long",
        "stop_price": 90.0, "target_price": 120.0,
        "fvg_time": "2026-08-05T13:00:00Z", "fvg_known_time": "2026-08-05T13:03:00Z",
    }
    old_prefix = {
        "date": "2026-08-05", "recorded_at": "2026-08-05T14:20:00Z",
        "cutoffs": {"nq": "2026-08-05T10:26:00-04:00", "spx": "2026-08-05T10:24:00-04:00"},
    }
    new_prefix = {
        "date": "2026-08-05", "recorded_at": "2026-08-05T14:27:00Z",
        "cutoffs": {"nq": "2026-08-05T10:27:00-04:00", "spx": "2026-08-05T10:25:00-04:00"},
    }
    old_raw = json.dumps(old_prefix, sort_keys=True).encode("utf-8")
    new_raw = json.dumps(new_prefix, sort_keys=True).encode("utf-8")
    client._broker_objects = lambda comment: []
    client._persist_filter_terminal(
        tmp_path, decision, {"state": "UNRESOLVED", "rule": None, "features": {}},
        "FILTER_UNRESOLVED_DEFERRED", "FILTER_UNRESOLVED_DEFERRED", old_prefix, old_raw,
    )
    result = client._place_candidate(
        tmp_path, decision, "US100Cash", 3.0,
        prefix_record=new_prefix, prefix_raw=new_raw, send_now=now,
    )

    if window_open:
        assert result["state"] == "SUBMITTED"
        assert client.mt5.sent == 1
        assert client._intent_state(tmp_path, decision["order_id"])["status"] == "SUBMITTED"
        assert [event["event"] for event in client._events(tmp_path)].count("SUPER1_FILTER_PROMOTED") == 1
    else:
        assert result["state"] == "FILTER_EXPIRED_NO_SEND"
        assert client.mt5.sent == 0
        assert client._intent_state(tmp_path, decision["order_id"])["status"] == "FILTER_EXPIRED_NO_SEND"
        assert not any(event["event"] == "SUPER1_FILTER_PROMOTED" for event in client._events(tmp_path))
    record_if_enabled(request, evidence_token)
