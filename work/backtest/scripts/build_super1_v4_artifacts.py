"""Build the unsigned, internally hash-bound Super1 V4 artifact chain."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from candidate_artifact import payload_sha256
from backtest.engine_pipeline import source_code_hash


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def canonical_hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


FROZEN_INVENTORY = ROOT / "data/provenance/dukascopy_v4/frozen_invalid_leg_days_v4.csv"
FROZEN_INVENTORY_SHA256 = "a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075"


def main() -> None:
    calendar = ROOT / "live_forward/calendars/us_equity_rth_2022_2026_v4.json"
    data_root = ROOT / "data/provenance/dukascopy_v4"
    if not FROZEN_INVENTORY.is_file() or digest(FROZEN_INVENTORY) != FROZEN_INVENTORY_SHA256:
        raise SystemExit("frozen V4 inventory is missing or has the wrong SHA-256")
    manifest_paths = sorted(data_root.glob("reacquired_session/**/*.manifest.json"))
    data_manifest = {
        "schema_version": 4,
        "provider": "Dukascopy",
        "side": "BID",
        "source_granularity": "M1",
        "evaluation_domain": {"start": "2022-01-03", "end": "2026-07-02"},
        "acquisition_envelope": {"start": "2022-01-01", "end_exclusive": "2026-07-03"},
        "frozen_invalid_leg_inventory_path": FROZEN_INVENTORY.relative_to(ROOT).as_posix(),
        "frozen_invalid_leg_inventory_sha256": FROZEN_INVENTORY_SHA256,
        "authoritative_target_count": 113,
        "fresh_leg_day_manifests": [
            {"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path)} for path in manifest_paths
        ],
        "synthetic_bars": False,
        "promotion_status": "PENDING_RESIDUAL_GAP_AUDIT",
    }
    data_manifest_path = data_root / "data_manifest_v4.json"
    write(data_manifest_path, data_manifest)

    schema = {
        "schema_version": 1,
        "title": "Super1 private deployment binding V4",
        "private": True,
        "required": ["schema_version", "binding_id", "nonce", "account_login", "server", "company", "issued_at_utc"],
        "binding_id": {"encoding": "lowercase_hex", "bits": 128},
        "nonce": {"encoding": "lowercase_hex", "bits": 128},
        "signature": {"detached": True, "algorithm": "RSA-PSS-SHA256", "trust_root": "deploy/release-public-key.pem"},
    }
    schema_path = ROOT / "live_forward/account_binding_schema_v4.json"
    write(schema_path, schema)

    old_candidate = json.loads((ROOT / "research_candidates/super1/super1_unsigned_candidate_v3.json").read_text(encoding="utf-8"))
    candidate = deepcopy(old_candidate)
    candidate.update({
        "schema_version": 4,
        "artifact_id": "super1_unsigned_candidate_v4",
        "name": "SUPER1_UNSIGNED_CANDIDATE_V4",
        "status": "UNSIGNED_VALIDATION_ONLY",
        "promotion_bindings": {
            "candidate_path": "research_candidates/super1/super1_unsigned_candidate_v4.json",
            "calendar_path": calendar.relative_to(ROOT).as_posix(),
            "data_manifest_path": data_manifest_path.relative_to(ROOT).as_posix(),
            "config_path": "live_forward/super1_xm_mt5_demo_config_v4.json",
            "signal_contract_path": "research_candidates/super1/super1_signal_contract_v4.json",
            "instrument_registry_path": "live_forward/instrument_registry_v4.json",
            "locked_oos_baseline_path": "research_candidates/super1/super1_locked_oos_baseline_v4.json",
            "account_binding_schema_path": "live_forward/account_binding_schema_v4.json",
            "sandbox_protocol_path": "backtest/live_signal_protocol.py",
            "deal_schema_path": "backtest/live/deal_ingestion.py",
        },
    })
    candidate["artifact_sha256"] = payload_sha256(candidate)
    candidate_path = ROOT / "research_candidates/super1/super1_unsigned_candidate_v4.json"
    write(candidate_path, candidate)

    registry = json.loads((ROOT / "live_forward/instrument_registry_v3.json").read_text(encoding="utf-8"))
    registry.update({"registry_id": "SUPER1_INSTRUMENT_REGISTRY_V4", "schema_version": 4, "status": "UNSIGNED_VALIDATION_ONLY"})
    for row in registry["instruments"]:
        row["session_calendar_id"] = "US_EQUITY_RTH_2022_2026_V4"
        row.pop("expected_server", None)
        row.pop("expected_company", None)
    registry_path = ROOT / "live_forward/instrument_registry_v4.json"
    write(registry_path, registry)

    old_contract = json.loads((ROOT / "research_candidates/super1/super1_signal_contract_v3.json").read_text(encoding="utf-8"))
    contract = deepcopy(old_contract)
    contract.update({"schema_version": 4, "name": "SUPER1_CANONICAL_OVERLAY_FRESH_FORWARD_V4"})
    contract["overlay_candidate"].update({
        "path": candidate_path.relative_to(ROOT).as_posix(),
        "file_sha256": digest(candidate_path),
        "artifact_sha256": candidate["artifact_sha256"],
    })
    contract["signal_source"].update({
        "generator_sha256": digest(ROOT / contract["signal_source"]["generator_path"]),
        "payload_adapter_sha256": digest(ROOT / contract["signal_source"]["payload_adapter_path"]),
        "engine_source_sha256": source_code_hash(),
    })
    contract["demo_order_transport"].update({
        "sha256": digest(ROOT / contract["demo_order_transport"]["path"]),
        "account_identity_gate": "DETACHED_SIGNED_PRIVATE_BINDING_EQUALITY",
    })
    contract["rth_session_calendar"] = {
        "calendar_id": "US_EQUITY_RTH_2022_2026_V4",
        "path": calendar.relative_to(ROOT).as_posix(),
        "sha256": digest(calendar),
    }
    contract_path = ROOT / "research_candidates/super1/super1_signal_contract_v4.json"
    write(contract_path, contract)

    baseline = {
        **json.loads((ROOT / "live_forward/super1_xm_mt5_demo_config_v3.json").read_text(encoding="utf-8"))["strategy_health_baseline"],
        "candidate_hash": candidate["artifact_sha256"],
        "data_manifest_sha256": digest(data_manifest_path),
        "calendar_sha256": digest(calendar),
        "engine_source_sha256": source_code_hash(),
    }
    baseline_path = ROOT / "research_candidates/super1/super1_locked_oos_baseline_v4.json"
    write(baseline_path, baseline)

    old_config = json.loads((ROOT / "live_forward/super1_xm_mt5_demo_config_v3.json").read_text(encoding="utf-8"))
    config = deepcopy(old_config)
    for key in ("account_login", "expected_server", "expected_company"):
        config.pop(key, None)
    config.update({
        "schema_version": 4,
        "status": "UNSIGNED_VALIDATION_ONLY",
        "campaign_id": "SUPER1_UNSIGNED_CANDIDATE_V4",
        "candidate_path": candidate_path.relative_to(ROOT).as_posix(),
        "candidate_file_sha256": digest(candidate_path),
        "candidate_artifact_sha256": candidate["artifact_sha256"],
        "data_manifest_path": data_manifest_path.relative_to(ROOT).as_posix(),
        "data_manifest_sha256": digest(data_manifest_path),
        "signal_contract_path": contract_path.relative_to(ROOT).as_posix(),
        "signal_contract_sha256": digest(contract_path),
        "strategy_health_baseline": baseline,
        "strategy_health_baseline_path": baseline_path.relative_to(ROOT).as_posix(),
        "strategy_health_baseline_sha256": digest(baseline_path),
        "deployment_binding_required": True,
        "deployment_binding_path": r"C:\Super1\runtime_trust\account-binding.json",
        "deployment_binding_signature_path": r"C:\Super1\runtime_trust\account-binding.sig",
        "deployment_binding_public_key_path": "deploy/release-public-key.pem",
        "account_binding_schema_path": schema_path.relative_to(ROOT).as_posix(),
        "account_binding_schema_sha256": digest(schema_path),
        "rth_session_calendar": contract["rth_session_calendar"],
    })
    for leg in config["legs"].values():
        leg["instrument_registry_path"] = registry_path.relative_to(ROOT).as_posix()
        leg["instrument_registry_sha256"] = digest(registry_path)
    config["live_risk_policy"]["instrument_registry_sha256"] = digest(registry_path)
    config_path = ROOT / "live_forward/super1_xm_mt5_demo_config_v4.json"
    write(config_path, config)

    risk_policy_hash = canonical_hash({"risk_limits": config["risk_limits"], "live_risk_policy": config["live_risk_policy"], "risk_rule": config["risk_rule"]})
    manifest = {
        "schema_version": 4,
        "name": "Super1 V4",
        "status": "UNSIGNED_VALIDATION_ONLY",
        "proven": False,
        "fresh_forward_required": True,
        "candidate_path": candidate_path.relative_to(ROOT).as_posix(),
        "candidate_file_sha256": digest(candidate_path),
        "candidate_artifact_sha256": candidate["artifact_sha256"],
        "signal_contract_path": contract_path.relative_to(ROOT).as_posix(),
        "signal_contract_sha256": digest(contract_path),
        "config_path": config_path.relative_to(ROOT).as_posix(),
        "config_sha256": digest(config_path),
        "instrument_registry_path": registry_path.relative_to(ROOT).as_posix(),
        "instrument_registry_sha256": digest(registry_path),
        "calendar_path": calendar.relative_to(ROOT).as_posix(),
        "calendar_sha256": digest(calendar),
        "data_manifest_path": data_manifest_path.relative_to(ROOT).as_posix(),
        "data_manifest_sha256": digest(data_manifest_path),
        "locked_oos_baseline_path": baseline_path.relative_to(ROOT).as_posix(),
        "locked_oos_baseline_sha256": digest(baseline_path),
        "engine_source_sha256": source_code_hash(),
        "sandbox_protocol_path": "backtest/live_signal_protocol.py",
        "sandbox_protocol_sha256": digest(ROOT / "backtest/live_signal_protocol.py"),
        "deal_schema_path": "backtest/live/deal_ingestion.py",
        "deal_schema_sha256": digest(ROOT / "backtest/live/deal_ingestion.py"),
        "risk_policy_sha256": risk_policy_hash,
        "account_binding_schema_sha256": digest(schema_path),
        "account_binding_schema_path": schema_path.relative_to(ROOT).as_posix(),
        "promotion_bindings": {
            **candidate["promotion_bindings"],
            "candidate_file_sha256": digest(candidate_path),
            "config_sha256": digest(config_path),
            "calendar_sha256": digest(calendar),
            "data_manifest_sha256": digest(data_manifest_path),
            "signal_contract_sha256": digest(contract_path),
            "instrument_registry_sha256": digest(registry_path),
            "locked_oos_baseline_sha256": digest(baseline_path),
            "account_binding_schema_sha256": digest(schema_path),
            "sandbox_protocol_sha256": digest(ROOT / "backtest/live_signal_protocol.py"),
            "deal_schema_sha256": digest(ROOT / "backtest/live/deal_ingestion.py"),
            "engine_source_sha256": source_code_hash(),
            "risk_policy_sha256": risk_policy_hash,
            "candidate_artifact_sha256": candidate["artifact_sha256"],
        },
        "reliability_manifest_sha256": None,
        "risk_xray_manifest_sha256": None,
        "promotion_blockers": ["DATA_INCOMPLETE", "SECURITY_HISTORY_UNASSESSED_OR_UNREMEDIATED"],
        "promotion_evidence": {
            "reacquisition_manifest_sha256": None,
            "reacquisition_semantic_root_sha256": None,
            "full_history_determinism_a_sha256": None,
            "full_history_determinism_b_sha256": None,
            "reliability_determinism_a_sha256": None,
            "reliability_determinism_b_sha256": None,
            "risk_xray_sha256": None,
            "security_history_attestation_sha256": None,
            "account_rotation_attestation_sha256": None,
        },
        "deployment": {
            "target": "LOCAL_WINDOWS_PC", "isolation_required": True,
            "demo_order_execution_enabled": True, "real_money_live_enabled": False,
            "real_money_execution_allowed": False, "existing_campaign_must_remain_untouched": True,
            "daily_manual_start_required": True, "unattended_execution_allowed": False,
        },
    }
    write(ROOT / "research_candidates/super1/super1_manifest_v4.json", manifest)


if __name__ == "__main__":
    main()
