from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "run_super1_xm_mt5_forward", ROOT / "scripts" / "run_super1_xm_mt5_forward.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


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
                login=1301910045,
                server="XMGlobal-MT5 6",
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


def test_super1_preflight_checks_both_risk_states() -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))

    assert client._preflight_risk_scales() == (0.75, 1.1)


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


def test_overnight_direction_consumes_verified_adapter_prices(monkeypatch) -> None:
    client = object.__new__(MODULE.Super1XmMt5DemoOrderClient)
    client.config = {"history_days": 10}
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
