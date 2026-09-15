from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any, Mapping
from uuid import uuid4

import pandas as pd

from backtest.live.approval import ApprovalError, ApprovalStore, proposal_hash, wire_request_hash
from backtest.live.audit_ledger import AUDIT_SCHEMA_VERSION, GENESIS_HASH, AuditLedger, event_hash
from backtest.live.broker_facts import BrokerFactsBuilder, BrokerFactsError
from backtest.live.contracts import BrokerEvidence, BrokerSnapshot, InstrumentContract, LiveRiskPolicy, RiskApprovedOrder
from backtest.live.execution import Mt5WritePort
from backtest.live.deal_ingestion import (
    TerminalDealIngestor,
    build_terminal_query_batch,
)
from backtest.live.deployment_binding import DeploymentBindingError, load_verified_deployment_binding
from backtest.market_calendar import MarketCalendarError, load_signed_calendar
from backtest.live.production_flow import ProductionDependencies, ProductionOrderFlow
from backtest.live.risk_guard import RiskGuard
from backtest.live.halt import HaltController, emergency_flatten_cycle
from backtest.live.instruments import InstrumentRegistry, validate_current_tick, validate_economic_semantics
from backtest.live.order_state import OrderStateMachine
from backtest.live.settings import PlatformPaths, RuntimeSettings
from backtest.live.strategy_health import LockedOOSBaseline, StrategyHealth
from backtest.signals import SignalProposal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_capital_forward as core
import run_xm_mt5_forward as xm
from candidate_artifact import ArtifactValidationError, load_artifact
from backtest.candidate_validation import CandidateValidationError, validate_super1_v4_candidate, validate_super1_v5_candidate
from super1_runtime_guard import (
    AccountBindingMismatchError,
    Super1RuntimeError,
    assert_account_binding,
    file_sha256,
    load_lease,
    load_runtime_config,
    load_runtime_manifest,
    runtime_binding_expectations,
    order_mutex,
)


RUNTIME_CONFIG = ROOT / "live_forward" / "super1_xm_mt5_demo_config_v5.json"
SUPER1_MANIFEST = ROOT / "research_candidates" / "super1" / "super1_manifest_v5.json"
FORWARD_SHADOW_ADAPTER = ROOT / "scripts" / "run_forward_shadow.py"
DEPLOYMENT_MODE = "FROZEN_CANONICAL_PAIR_PIPELINE_WITH_SUPER1_OVERLAY"
REQUIRED_ENV: tuple[str, ...] = ()
RTH_CALENDAR_RELATIVE = "live_forward/calendars/us_equity_rth_2026.json"
RTH_CALENDAR_STATES = {"OPEN", "EARLY_CLOSE", "CLOSED"}
AUDIT_EVENT_SOURCE = "Super1AuditAnchor"
AUDIT_EVENT_LOG = "APPLICATION"
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def write_event_log_anchor(event_hash: str, event_id: str) -> None:
    """Write the hash anchor to the installer-registered Windows Event Log source."""
    if not _SHA256_PATTERN.fullmatch(str(event_hash)) or not str(event_id).strip():
        raise Super1RuntimeError("audit anchor payload is invalid")
    if sys.platform != "win32":
        raise Super1RuntimeError("Windows Event Log audit anchor is unavailable")
    eventcreate = PlatformPaths.current().system_root / "System32" / "eventcreate.exe"
    if not eventcreate.is_file():
        raise Super1RuntimeError("trusted eventcreate.exe is missing")
    message = f"sequence_anchor event_id={event_id}; event_hash={event_hash.lower()}"
    result = subprocess.run(
        [
            str(eventcreate),
            "/T", "INFORMATION",
            "/ID", "1000",
            "/L", AUDIT_EVENT_LOG,
            "/SO", AUDIT_EVENT_SOURCE,
            "/D", message,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise Super1RuntimeError(f"Windows Event Log audit anchor failed: {detail}")


class Super1FeatureError(core.CriticalLiveError):
    pass


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Super1FeatureError(f"RTH calendar contains duplicate JSON key: {key}.")
        result[key] = value
    return result


def _safe_repo_file(relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise Super1FeatureError(f"{label} must be a repository-relative file path.")
    candidate = ROOT / Path(relative)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise Super1FeatureError(f"{label} escapes the repository.") from exc
    if any(part.is_symlink() for part in [candidate, *candidate.parents]):
        raise Super1FeatureError(f"{label} resolves through a reparse/symlink path.")
    if not resolved.is_file():
        raise Super1FeatureError(f"{label} is missing.")
    return resolved


def _signed_health_baseline(config: dict[str, Any], candidate_hash: str) -> LockedOOSBaseline:
    raw = config.get("strategy_health_baseline")
    if not isinstance(raw, dict):
        raise Super1RuntimeError("signed strategy-health baseline is missing")
    required = ("candidate_hash", "closed_trades", "valid_sessions", "years", "rolling_net_r_p05", "drawdown_p95", "drawdown_p99", "seed", "locked")
    if any(key not in raw for key in required):
        raise Super1RuntimeError("signed strategy-health baseline is incomplete")
    try:
        baseline = LockedOOSBaseline(
            candidate_hash=str(raw["candidate_hash"]),
            closed_trades=int(raw["closed_trades"]),
            valid_sessions=int(raw["valid_sessions"]),
            years=tuple(int(year) for year in raw["years"]),
            rolling_net_r_p05=float(raw["rolling_net_r_p05"]),
            drawdown_p95=float(raw["drawdown_p95"]),
            drawdown_p99=float(raw["drawdown_p99"]),
            seed=int(raw["seed"]),
            locked=bool(raw["locked"]),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise Super1RuntimeError("signed strategy-health baseline is malformed") from exc
    if not baseline.locked or baseline.candidate_hash != candidate_hash:
        raise Super1RuntimeError("signed strategy-health baseline is not bound to the candidate")
    return baseline


def _signed_risk_limits(config: dict[str, Any]) -> dict[str, float]:
    raw = config.get("risk_limits")
    required = ("daily_loss_cap_r", "max_total_stop_risk_percent", "max_margin_fraction", "max_leverage", "max_pair_exposure_percent", "max_concentration_percent")
    if not isinstance(raw, dict) or any(key not in raw for key in required):
        raise Super1RuntimeError("signed risk limits are missing or incomplete")
    try:
        values = {key: float(raw[key]) for key in required}
    except (TypeError, ValueError, OverflowError) as exc:
        raise Super1RuntimeError("signed risk limits are malformed") from exc
    if not all(math.isfinite(value) for value in values.values()):
        raise Super1RuntimeError("signed risk limits contain non-finite values")
    return values


def _signed_live_risk_policy(config: dict[str, Any]) -> LiveRiskPolicy:
    raw = config.get("live_risk_policy")
    limits = _signed_risk_limits(config)
    if not isinstance(raw, dict):
        raise Super1RuntimeError("signed live risk policy is missing")
    legs = config.get("legs")
    if not isinstance(legs, dict):
        raise Super1RuntimeError("signed live risk instrument bindings are missing")
    whitelist = tuple(sorted((str(value["instrument_id"]), str(value["epic"])) for value in legs.values()))
    try:
        return LiveRiskPolicy(
            float(raw["daily_loss_cap_r"]),
            tuple(sorted((str(key), int(value)) for key, value in raw["max_trades_per_day_by_instrument"].items())),
            int(raw["max_total_trades_per_day"]),
            whitelist,
            int(raw["max_open_positions_by_instrument"]),
            int(raw["max_total_open_positions"]),
            int(raw["entry_cooldown_seconds"]),
            tuple(sorted((str(key), float(value)) for key, value in raw["max_position_volume_by_instrument"].items())),
            limits["max_total_stop_risk_percent"], limits["max_margin_fraction"],
            limits["max_leverage"], limits["max_pair_exposure_percent"], limits["max_concentration_percent"],
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise Super1RuntimeError("signed live risk policy is invalid") from exc


def _load_canonical_rth_calendar(runtime: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    path = _safe_repo_file(reference.get("path"), "canonical RTH calendar")
    try:
        calendar, calendar_hash = load_signed_calendar(
            artifact=path, expected_calendar_id=str(reference.get("calendar_id") or ""),
            expected_sha256=str(reference.get("sha256") or ""),
        )
    except MarketCalendarError as exc:
        raise Super1FeatureError(f"canonical RTH calendar verification failed: {exc}") from exc
    coverage = calendar.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("start") != "2022-01-01" or coverage.get("end") != "2026-12-31":
        raise Super1FeatureError("canonical RTH calendar coverage is invalid")
    source_records = calendar.get("source_records")
    if not isinstance(source_records, list):
        raise Super1FeatureError("canonical RTH calendar must contain all official source records")
    closed = {str(value) for value in calendar.get("closed_dates", [])}
    early = {str(value) for value in calendar.get("early_close_dates", [])}
    start = pd.Timestamp(coverage["start"]).date()
    end = pd.Timestamp(coverage["end"]).date()
    sessions: dict[str, dict[str, Any]] = {}
    for day in pd.date_range(start, end, freq="1D"):
        key = day.strftime("%Y-%m-%d")
        if day.weekday() >= 5 or key in closed:
            sessions[key] = {"date": key, "state": "CLOSED"}
        elif key in early:
            sessions[key] = {"date": key, "state": "EARLY_CLOSE", "start": "09:30", "end": "13:00"}
        else:
            sessions[key] = {"date": key, "state": "OPEN", "start": "09:30", "end": "16:00"}
    return {"path": str(path), "sha256": calendar_hash, "calendar_id": calendar["calendar_id"], "coverage": coverage, "sessions": sessions, "source_records": source_records}


def load_verified_rth_calendar(runtime: dict[str, Any]) -> dict[str, Any]:
    reference = runtime.get("rth_session_calendar")
    if not isinstance(reference, dict):
        raise Super1FeatureError("Verified RTH calendar reference is missing.")
    if reference.get("calendar_id") in {"US_EQUITY_RTH_2022_2026_V2", "US_EQUITY_RTH_2022_2026_V3"}:
        raise Super1FeatureError("canonical RTH calendar raw source bytes are not sealed")
    if reference.get("calendar_id") == "US_EQUITY_RTH_2022_2026_V4":
        return _load_canonical_rth_calendar(runtime, reference)
    if (
        reference.get("path") != RTH_CALENDAR_RELATIVE
        or reference.get("calendar_id") != "US_EQUITY_RTH_2026"
        or not isinstance(reference.get("sha256"), str)
    ):
        raise Super1FeatureError("RTH calendar reference is not the sealed production format.")
    path = _safe_repo_file(reference["path"], "RTH calendar")
    raw = path.read_bytes()
    actual_hash = core.file_hash(path)
    if actual_hash != reference["sha256"]:
        raise Super1FeatureError("RTH calendar raw hash mismatch.")
    try:
        calendar = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_pairs
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Super1FeatureError("RTH calendar JSON is invalid.") from exc
    if not isinstance(calendar, dict):
        raise Super1FeatureError("RTH calendar root must be an object.")
    if (
        calendar.get("schema_version") != 1
        or calendar.get("calendar_id") != "US_EQUITY_RTH_2026"
        or calendar.get("timezone") != core.TZ
    ):
        raise Super1FeatureError("RTH calendar identity or timezone is invalid.")
    coverage = calendar.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != {"start", "end"}:
        raise Super1FeatureError("RTH calendar coverage is invalid.")
    try:
        coverage_start = pd.Timestamp(coverage["start"]).date()
        coverage_end = pd.Timestamp(coverage["end"]).date()
    except (TypeError, ValueError) as exc:
        raise Super1FeatureError("RTH calendar coverage dates are invalid.") from exc
    expected_dates = pd.date_range(coverage_start, coverage_end, freq="1D")
    session_rows = calendar.get("sessions")
    if not isinstance(session_rows, list) or len(session_rows) != len(expected_dates):
        raise Super1FeatureError("RTH calendar does not cover every date exactly once.")
    sessions: dict[str, dict[str, Any]] = {}
    for row in session_rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise Super1FeatureError("RTH calendar contains an invalid session row.")
        date_key = row["date"]
        if date_key in sessions:
            raise Super1FeatureError(f"RTH calendar contains duplicate date: {date_key}.")
        try:
            parsed_date = pd.Timestamp(date_key).date()
        except (TypeError, ValueError) as exc:
            raise Super1FeatureError(f"RTH calendar date is invalid: {date_key}.") from exc
        if parsed_date < coverage_start or parsed_date > coverage_end:
            raise Super1FeatureError(f"RTH calendar date is outside coverage: {date_key}.")
        state = str(row.get("state") or "")
        if state not in RTH_CALENDAR_STATES:
            raise Super1FeatureError(f"RTH calendar state is invalid: {date_key}.")
        if state == "CLOSED":
            if "start" in row or "end" in row:
                raise Super1FeatureError(f"Closed RTH row must not contain hours: {date_key}.")
        else:
            start = row.get("start")
            end = row.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                raise Super1FeatureError(f"Open RTH row has no complete hours: {date_key}.")
            try:
                start_time = pd.Timestamp(f"{date_key} {start}", tz=core.TZ)
                end_time = pd.Timestamp(f"{date_key} {end}", tz=core.TZ)
            except (TypeError, ValueError) as exc:
                raise Super1FeatureError(f"RTH hours are invalid: {date_key}.") from exc
            if start_time.strftime("%H:%M") != start or end_time.strftime("%H:%M") != end:
                raise Super1FeatureError(f"RTH hours are not canonical local times: {date_key}.")
            if start_time >= end_time or start != "09:30" or end not in {"13:00", "16:00"}:
                raise Super1FeatureError(f"RTH hours are outside the US equity core session: {date_key}.")
            if state == "EARLY_CLOSE" and end != "13:00":
                raise Super1FeatureError(f"Early-close row has the wrong end time: {date_key}.")
            if state == "OPEN" and end != "16:00":
                raise Super1FeatureError(f"Open row has the wrong end time: {date_key}.")
        sessions[date_key] = row
    if set(sessions) != {item.strftime("%Y-%m-%d") for item in expected_dates}:
        raise Super1FeatureError("RTH calendar date coverage has gaps or extra dates.")
    source_records = calendar.get("source_records")
    if not isinstance(source_records, list) or not source_records:
        raise Super1FeatureError("RTH calendar source provenance is missing.")
    source_ids = [source.get("id") for source in source_records if isinstance(source, dict)]
    if source_ids != ["NASDAQ_TRADING_CALENDAR_2026", "NYSE_TRADING_CALENDAR_2026"]:
        raise Super1FeatureError("RTH calendar source provenance must contain both official 2026 records in order.")
    expected_urls = {
        "NASDAQ_TRADING_CALENDAR_2026": "https://www.nasdaqtrader.com/Trader.aspx?id=calendar",
        "NYSE_TRADING_CALENDAR_2026": "https://www.nyse.com/publicdocs/nyse/ICE_NYSE_2026_Yearly_Trading_Calendar.pdf",
    }
    for source in source_records:
        if not isinstance(source, dict):
            raise Super1FeatureError("RTH calendar source provenance row is invalid.")
        source_path = _safe_repo_file(source.get("provenance_path"), "RTH source provenance")
        if source.get("sha256") != core.file_hash(source_path):
            raise Super1FeatureError(
                f"RTH source provenance hash mismatch: {source_path.name}."
            )
        if source.get("bytes") != source_path.stat().st_size:
            raise Super1FeatureError(
                f"RTH source provenance byte count mismatch: {source_path.name}."
            )
        if source.get("url") != expected_urls.get(source.get("id")):
            raise Super1FeatureError("RTH source provenance URL is invalid.")
    return {
        "path": str(path),
        "sha256": actual_hash,
        "calendar_id": calendar["calendar_id"],
        "coverage": coverage,
        "sessions": sessions,
        "source_records": source_records,
    }


def validate_super1_candidate(runtime: dict[str, Any]) -> dict[str, Any]:
    if runtime.get("schema_version") == 5:
        relative = runtime.get("candidate_path")
        if not isinstance(relative, str) or not relative:
            raise Super1FeatureError("Super1 V5 runtime has no candidate_path.")
        candidate_path = _safe_repo_file(relative, "Super1 V5 candidate")
        try:
            signal_probe = core.read_json(ROOT / str(runtime.get("signal_contract_path") or ""))
            signal_source_probe = signal_probe.get("signal_source", {})
            if signal_source_probe.get("engine_source_sha256") != core.source_code_hash() or core.file_hash(FORWARD_SHADOW_ADAPTER) != signal_source_probe.get("payload_adapter_sha256"):
                raise Super1FeatureError("Super1 signal contract is invalid or does not match runtime bytes.")
            candidate = load_artifact(
                candidate_path,
                ROOT,
                artifact_type="strategy_candidate",
                verify_inputs=True,
            )
            return validate_super1_v5_candidate(
                candidate,
                root=ROOT,
                runtime=runtime,
                candidate_path=candidate_path,
                manifest_path=SUPER1_MANIFEST,
            )
        except (ArtifactValidationError, CandidateValidationError, OSError, ValueError) as exc:
            raise Super1FeatureError(f"Super1 V5 artifact chain is invalid: {exc}") from exc
    return _validate_super1_v4_candidate(runtime)


def _validate_super1_v4_candidate(runtime: dict[str, Any]) -> dict[str, Any]:
    relative = runtime.get("candidate_path")
    if not isinstance(relative, str) or not relative:
        raise Super1FeatureError("Super1 runtime has no candidate_path.")
    candidate_path = (ROOT / relative).resolve()
    try:
        candidate_path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise Super1FeatureError("Super1 candidate path escapes the repository.") from exc
    expected_file_hash = str(runtime.get("candidate_file_sha256") or "")
    if not candidate_path.is_file() or core.file_hash(candidate_path) != expected_file_hash:
        raise Super1FeatureError("Super1 candidate file hash mismatch.")
    try:
        candidate = load_artifact(
            candidate_path,
            ROOT,
            artifact_type="strategy_candidate",
            verify_inputs=True,
        )
    except (ArtifactValidationError, OSError, ValueError) as exc:
        raise Super1FeatureError(f"Super1 candidate artifact is invalid: {exc}") from exc
    if candidate.get("artifact_sha256") != runtime.get("candidate_artifact_sha256"):
        raise Super1FeatureError("Super1 candidate artifact SHA mismatch.")
    if candidate.get("setup_rules") != runtime.get("setup_rules"):
        raise Super1FeatureError("Super1 setup rules differ from the sealed candidate.")
    candidate_risk = candidate.get("risk_rule")
    runtime_risk = runtime.get("risk_rule")
    if not isinstance(candidate_risk, dict) or not isinstance(runtime_risk, dict):
        raise Super1FeatureError("Super1 risk rule is missing.")
    if {key: runtime_risk.get(key) for key in candidate_risk} != candidate_risk:
        raise Super1FeatureError("Super1 risk rule differs from the sealed candidate.")
    if (
        runtime.get("schema_version") != 4
        or runtime.get("status") != "UNSIGNED_VALIDATION_ONLY"
        or candidate.get("status") != "UNSIGNED_VALIDATION_ONLY"
        or candidate.get("unsigned") is not True
        or candidate.get("live_enabled") is not False
        or candidate.get("proven") is not False
        or candidate.get("fresh_forward_required") is not True
        or candidate.get("development_result_sha256") is not None
        or candidate.get("full_evaluation_result_sha256") is not None
        or not all(bool(value) for value in candidate.get("checks", {}).values())
    ):
        raise Super1FeatureError("Super1 candidate safety status is invalid.")
    protocol = candidate.get("selection_protocol", {})
    if protocol.get("final_holdout_used_for_selection") is not False or protocol.get(
        "final_holdout_used_for_publication"
    ) is not False:
        raise Super1FeatureError("Super1 candidate holdout contract is invalid.")

    contract_relative = runtime.get("signal_contract_path")
    if not isinstance(contract_relative, str) or not contract_relative:
        raise Super1FeatureError("Super1 runtime has no signal contract.")
    contract_path = (ROOT / contract_relative).resolve()
    try:
        contract_path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise Super1FeatureError("Super1 signal contract escapes the repository.") from exc
    expected_contract_hash = str(runtime.get("signal_contract_sha256") or "")
    if not contract_path.is_file() or core.file_hash(contract_path) != expected_contract_hash:
        raise Super1FeatureError("Super1 signal contract hash mismatch.")
    contract = core.read_json(contract_path)
    signal_source = contract.get("signal_source", {})
    overlay = contract.get("overlay_candidate", {})
    order_transport = contract.get("demo_order_transport", {})
    calendar_contract = contract.get("rth_session_calendar", {})
    safety = contract.get("safety", {})
    source_config = (ROOT / str(signal_source.get("config_path") or "")).resolve()
    source_generator = (ROOT / str(signal_source.get("generator_path") or "")).resolve()
    source_adapter = (ROOT / str(signal_source.get("payload_adapter_path") or "")).resolve()
    overlay_runtime = (ROOT / str(overlay.get("runtime_path") or "")).resolve()
    order_transport_path = (ROOT / str(order_transport.get("path") or "")).resolve()
    try:
        source_config.relative_to(ROOT.resolve())
        source_generator.relative_to(ROOT.resolve())
        source_adapter.relative_to(ROOT.resolve())
        overlay_runtime.relative_to(ROOT.resolve())
        order_transport_path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise Super1FeatureError("Super1 signal contract path escapes the repository.") from exc
    if (
        int(contract.get("schema_version", 0)) != 4
        or contract.get("name") != "SUPER1_CANONICAL_OVERLAY_CURRENT_SOURCE_CHAIN_V4"
        or contract.get("status") != "UNSIGNED_VALIDATION_ONLY"
        or contract.get("execution_scope") != "XM_MT5_DEMO_ONLY"
        or contract.get("deployment_mode") != DEPLOYMENT_MODE
        or signal_source.get("kind") != "FROZEN_CANONICAL_PAIR_PIPELINE"
        or not source_config.is_file()
        or core.file_hash(source_config) != signal_source.get("config_sha256")
        or not source_generator.is_file()
        or core.file_hash(source_generator) != signal_source.get("generator_sha256")
        or not source_adapter.is_file()
        or source_adapter != FORWARD_SHADOW_ADAPTER.resolve()
        or core.file_hash(source_adapter) != signal_source.get("payload_adapter_sha256")
        or core.source_code_hash() != signal_source.get("engine_source_sha256")
        or overlay.get("path") != relative
        or overlay.get("file_sha256") != expected_file_hash
        or overlay.get("artifact_sha256") != candidate.get("artifact_sha256")
        or overlay.get("scope") != "SETUP_FILTERS_AND_RISK_SCALING_ONLY"
        or overlay.get("defines_base_signals") is not False
        or overlay_runtime != Path(__file__).resolve()
        or "runtime_sha256" in overlay
        or order_transport_path != Path(xm.__file__).resolve()
        or core.file_hash(order_transport_path) != order_transport.get("sha256")
            or order_transport.get("account_identity_gate")
            != "DETACHED_SIGNED_PRIVATE_BINDING_EQUALITY"
            or calendar_contract != runtime.get("rth_session_calendar")
            or safety.get("independent_super1_signal_producer_present") is not False
        or safety.get("demo_order_execution_enabled") is not True
        or safety.get("real_money_live_enabled") is not False
        or safety.get("fresh_forward_required") is not True
        or safety.get("real_money_execution_allowed") is not False
        or safety.get("deployed_pipeline_historical_parity_proven") is not False
        or safety.get("candidate_research_results_apply_to_deployed_pipeline") is not False
        or "live_enabled" in safety
            or runtime.get("execution") != "MT5_DEMO_ORDERS"
            or runtime.get("account_mode") != "DEMO_ORDER"
            or runtime.get("live_order_approval_required") is not True
            or runtime.get("deployment_mode") != DEPLOYMENT_MODE
            or runtime.get("deployment_binding_required") is not True
            or any(key in runtime for key in ("account_login", "expected_server", "expected_company"))
    ):
        raise Super1FeatureError("Super1 signal contract is invalid or does not match runtime bytes.")

    manifest = core.read_json(SUPER1_MANIFEST)
    research_inputs = [
        item
        for item in candidate.get("provenance", {}).get("inputs", [])
        if item.get("role") == "research_dataset"
    ]
    if len(research_inputs) != 1:
        raise Super1FeatureError("Super1 candidate research dataset provenance is not unique.")
    deployment = manifest.get("deployment", {})
    if (
        manifest.get("schema_version") != 4
        or manifest.get("name") != "Super1 V4"
        or manifest.get("status") != "UNSIGNED_VALIDATION_ONLY"
        or manifest.get("candidate_path") != relative
        or manifest.get("candidate_file_sha256") != expected_file_hash
        or manifest.get("candidate_artifact_sha256") != candidate.get("artifact_sha256")
        or manifest.get("signal_contract_path") != contract_relative
        or manifest.get("signal_contract_sha256") != expected_contract_hash
        or manifest.get("config_sha256") != core.file_hash(RUNTIME_CONFIG)
        or manifest.get("calendar_sha256") != calendar_contract.get("sha256")
        or manifest.get("engine_source_sha256") != core.source_code_hash()
        or manifest.get("account_binding_schema_sha256") != runtime.get("account_binding_schema_sha256")
        or manifest.get("overlay_candidate_research_dataset_sha256")
        != research_inputs[0].get("sha256")
        or manifest.get("overlay_candidate_research_result_sha256") is not None
        or manifest.get("deployed_pipeline_historical_parity_proven") is not False
        or "deployed_pipeline_result_sha256" not in manifest
        or manifest.get("deployed_pipeline_result_sha256") is not None
        or "data_sha256" in manifest
        or "result_sha256" in manifest
        or deployment.get("demo_order_execution_enabled") is not True
        or deployment.get("real_money_live_enabled") is not False
        or deployment.get("real_money_execution_allowed") is not False
        or "live_enabled" in deployment
        or manifest.get("fresh_forward_required") is not True
        or manifest.get("proven") is not False
    ):
        raise Super1FeatureError("Super1 manifest is not bound to runtime and candidate bytes.")
    calendar_path = (ROOT / str(calendar_contract.get("path") or "")).resolve()
    if (
        not calendar_path.is_file()
        or core.file_hash(calendar_path) != calendar_contract.get("sha256")
        or core.read_json(calendar_path).get("status") != "UNSIGNED_VALIDATION_ONLY"
    ):
        raise Super1FeatureError("Unsigned v4 calendar binding is invalid.")
    return candidate


def _day_row(record: dict[str, Any], leg_key: str) -> dict[str, Any]:
    matches = [row for row in record["payload"]["days"] if str(row.get("leg_key")) == leg_key]
    if len(matches) != 1:
        raise Super1FeatureError(f"{leg_key}: current-day feature row is not unique.")
    return matches[0]


def _level_price(day: dict[str, Any], level_name: str) -> float:
    liquidity = day.get("liquidity") or {}
    if str(liquidity.get("selected_name") or "") == level_name and liquidity.get("selected_price") is not None:
        return float(liquidity["selected_price"])
    names = list(liquidity.get("candidate_names") or [])
    prices = list(liquidity.get("candidate_prices") or [])
    if len(names) != len(prices):
        raise Super1FeatureError("Liquidity candidate names/prices are inconsistent.")
    exact = [float(price) for name, price in zip(names, prices) if str(name) == level_name]
    if len(exact) == 1:
        return exact[0]
    if level_name.lower() == "vah" and day.get("vah") is not None:
        return float(day["vah"])
    if level_name.lower() == "val" and day.get("val") is not None:
        return float(day["val"])
    raise Super1FeatureError(f"Liquidity level price cannot be proven for {level_name}.")


def liquidity_type(record: dict[str, Any], decision: dict[str, Any], config: Any) -> str:
    context = str(decision.get("liquidity_context") or "").strip()
    if not context:
        raise Super1FeatureError("Decision has no liquidity context.")
    level = context.split()[0]
    if level.startswith("asia_"):
        return "asia_high_low"
    if level.startswith("london_"):
        return "london_high_low"
    if level.startswith("ny_am_"):
        return "ny_am_high_low"
    if level.startswith(("ny_pm_", "previous_day_")):
        return "session_high_low"
    if level.startswith("swing_"):
        touches = (
            record.get("graph_features", {})
            .get(str(decision["leg_key"]), {})
            .get("swing_touches", {})
            .get(level)
        )
        if touches is None:
            raise Super1FeatureError(f"Swing touch count cannot be proven for {level}.")
        if int(touches) >= 3:
            return "strong_swing"
        if int(touches) >= 2:
            return "equal_high_low"
    day = _day_row(record, str(decision["leg_key"]))
    if day.get("vah") is None or day.get("val") is None:
        raise Super1FeatureError("VAH/VAL is unavailable for the liquidity filter.")
    price = _level_price(day, level)
    tolerance = float(config.vah_val_tolerance) * 2.0
    near = min(abs(price - float(day["vah"])), abs(price - float(day["val"]))) <= tolerance
    return "vah_val_proximity" if near else "other_liquidity"


class _Super1ProductionSmokeAdapter:
    def __init__(self, client: "Super1XmMt5DemoOrderClient", output_root: Path, request: dict[str, object], contract: InstrumentContract):
        self.client = client
        self.output_root = output_root
        self.request = dict(request)
        self.contract = contract
        self.entry_writes = 0
        verifier = client._verify_private_binding if client.config.get("deployment_binding_required") is True else None
        self.write_port = Mt5WritePort(client.mt5, client._order_db(output_root), mutex=order_mutex, binding_verifier=verifier)

    def wire_request_hash(self, _order: RiskApprovedOrder) -> str:
        return wire_request_hash(self.request)

    def proposal_wire_request_hash(self, _proposal: SignalProposal) -> str:
        return wire_request_hash(self.request)

    def request_for_order(self, _order: RiskApprovedOrder) -> dict[str, Any]:
        return dict(self.request)

    def validate_request(self, order: RiskApprovedOrder) -> None:
        if wire_request_hash(self.request) != order.request_hash:
            raise core.CriticalLiveError("SMOKE request hash differs from the approved wire request")

    def send(self, order: RiskApprovedOrder) -> Any:
        if order.volume != self.contract.volume_min:
            raise core.CriticalLiveError("SMOKE request must use the signed broker minimum volume")
        if wire_request_hash(self.request) != order.request_hash:
            raise core.CriticalLiveError("SMOKE request hash differs from the approved wire request")
        self.entry_writes += 1
        return self.write_port.send(order.proposal_id)

    def reconcile(self, response: Any, order: RiskApprovedOrder) -> BrokerEvidence:
        try:
            retcode = int(getattr(response, "retcode"))
            ticket = int(getattr(response, "order"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise core.CriticalLiveError("SMOKE broker response is unreadable") from exc
        accepted = {
            int(getattr(self.client.mt5, "TRADE_RETCODE_PLACED", 10008)),
            int(getattr(self.client.mt5, "TRADE_RETCODE_DONE", 10009)),
        }
        if retcode not in accepted or ticket <= 0:
            raise core.CriticalLiveError("SMOKE broker response was not an accepted pending-order acknowledgement")
        observed = self.client._mt5_collection("orders_get", ticket=ticket)
        exact = [
            item for item in observed
            if int(getattr(item, "ticket", 0) or 0) == ticket
            and str(getattr(item, "symbol", "")) == self.contract.broker_symbol
            and str(getattr(item, "comment", "")) == str(self.request.get("comment"))
        ]
        if len(exact) != 1:
            raise core.CriticalLiveError("SMOKE pending-order readback is not exact")
        return BrokerEvidence(
            "SEND", retcode, ticket=ticket,
            deal_id=(int(getattr(response, "deal", 0) or 0) or None),
            broker_state="READBACK_CONFIRMED",
            raw={"ticket": ticket, "proposal_id": order.proposal_id},
        )


class _Super1ProductionAdapter:
    """Build the final MT5 request from a persisted RiskApprovedOrder."""

    def __init__(self, client: "Super1XmMt5DemoOrderClient", output_root: Path, request: dict[str, object], contract: InstrumentContract):
        self.client = client
        self.output_root = output_root
        self.base_request = dict(request)
        self.contract = contract
        self.last_request: dict[str, object] | None = None
        self.entry_writes = 0
        verifier = client._verify_private_binding if client.config.get("deployment_binding_required") is True else None
        self.write_port = Mt5WritePort(client.mt5, client._order_db(output_root), mutex=order_mutex, binding_verifier=verifier)

    def request_for_order(self, order: RiskApprovedOrder) -> dict[str, Any]:
        request = {**self.base_request, "volume": float(order.volume)}
        request["symbol"] = self.contract.broker_symbol
        return request

    def wire_request_hash(self, order: RiskApprovedOrder) -> str:
        return wire_request_hash(self.request_for_order(order))

    def validate_request(self, order: RiskApprovedOrder) -> None:
        request = self.request_for_order(order)
        if str(request.get("symbol") or "") != self.contract.broker_symbol:
            raise core.CriticalLiveError("production request symbol is not the signed exact symbol")
        volume = float(request.get("volume") or 0.0)
        if volume < self.contract.volume_min or volume > self.contract.volume_max:
            raise core.CriticalLiveError("production request volume is outside the signed registry")
        check = self.client._retry_mt5_read("order_check", request)
        retcode = None if check is None else getattr(check, "retcode", None)
        if retcode is None or int(retcode) != 0:
            raise core.CriticalLiveError("production broker order_check did not pass")

    def send(self, order: RiskApprovedOrder) -> Any:
        request = self.request_for_order(order)
        self.last_request = dict(request)
        self.entry_writes += 1
        return self.write_port.send(order.proposal_id)

    def reconcile(self, response: Any, order: RiskApprovedOrder) -> BrokerEvidence:
        try:
            retcode = int(getattr(response, "retcode"))
            ticket = int(getattr(response, "order"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise core.CriticalLiveError("production broker response is unreadable") from exc
        accepted = {
            int(getattr(self.client.mt5, "TRADE_RETCODE_PLACED", 10008)),
            int(getattr(self.client.mt5, "TRADE_RETCODE_DONE", 10009)),
        }
        if retcode not in accepted or ticket <= 0:
            raise core.CriticalLiveError("production broker response was not accepted")
        observed = self.client._mt5_collection("orders_get", ticket=ticket)
        request = self.request_for_order(order)
        exact = [
            item for item in observed
            if int(getattr(item, "ticket", 0) or 0) == ticket
            and self.client._broker_request_matches(item, request)
        ]
        if len(exact) != 1:
            raise core.CriticalLiveError("production broker order readback is not exact")
        return BrokerEvidence(
            "SEND", retcode, ticket=ticket,
            deal_id=(int(getattr(response, "deal", 0) or 0) or None),
            broker_state="READBACK_CONFIRMED",
            raw={"ticket": ticket, "proposal_id": order.proposal_id},
        )


class _Super1AuthorizedMaintenanceAdapter:
    """Stages a typed maintenance operation under the consumed smoke approval."""

    def __init__(self, client: "Super1XmMt5DemoOrderClient", output_root: Path, approval_id: str, campaign_id: str, account_key: str) -> None:
        verifier = client._verify_private_binding if client.config.get("deployment_binding_required") is True else None
        self.port = Mt5WritePort(client.mt5, client._order_db(output_root), mutex=order_mutex, binding_verifier=verifier)
        self.approval_id = approval_id
        self.campaign_id = campaign_id
        self.account_key = account_key

    def send(self, request: dict[str, object]) -> Any:
        ticket = int(request.get("order", request.get("position", 0)) or 0)
        operation_type = "CANCEL" if "order" in request else "CLOSE"
        operation_id = hashlib.sha256(
            f"{self.approval_id}\0{operation_type}\0{ticket}".encode("utf-8")
        ).hexdigest()
        try:
            self.port.arm_operator_operation(
                operation_id,
                operation_type=operation_type,
                request=request,
                approval_id=self.approval_id,
                campaign_id=self.campaign_id,
                account_key=self.account_key,
            )
            return self.port.send(operation_id)
        except Exception as exc:
            raise core.CriticalLiveError(f"durable maintenance operation failed: {exc}") from exc

class Super1XmMt5DemoOrderClient(xm.XmMt5DemoOrderClient):
    def __init__(self, config: dict[str, Any], secrets: dict[str, str], *, mt5_module: Any | None = None):
        expected_server = str(config.get("expected_server") or "")
        supplied_server = str(secrets.get("XM_MT5_SERVER") or "")
        if supplied_server and supplied_server != expected_server:
            raise AccountBindingMismatchError(
                "XM server input differs from the signed Super1 runtime config."
            )
        # The signed config, never the environment, owns the broker server.
        super().__init__(config, {**secrets, "XM_MT5_SERVER": expected_server}, mt5_module=mt5_module)
        self._super1_record: dict[str, Any] | None = None
        self._last_lease_binding: dict[str, str] | None = None
        self._runtime_secrets = dict(secrets)
        self._strict_reconciliation = True
        self._audit_anchor_callback = write_event_log_anchor

    def _terminal_binding_hash(self) -> str:
        configured = str(self.config.get("account_binding_schema_sha256") or "").lower()
        if len(configured) == 64 and all(char in "0123456789abcdef" for char in configured):
            return configured
        if self.config.get("deployment_binding_required") is True:
            raise Super1RuntimeError("private terminal account binding hash is unavailable")
        fixture_identity = {
            "account_login": self.config.get("account_login"),
            "expected_server": self.config.get("expected_server"),
            "expected_company": self.config.get("expected_company"),
        }
        return hashlib.sha256(json.dumps(fixture_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _verify_private_binding(self) -> None:
        binding = self.config.get("_verified_deployment_binding")
        if binding is None:
            raise AccountBindingMismatchError("private signed deployment binding is unavailable")
        refreshed = load_verified_deployment_binding(
            self.config["deployment_binding_path"],
            self.config["deployment_binding_signature_path"],
            ROOT / self.config["deployment_binding_public_key_path"],
        )
        if refreshed.binding_sha256 != binding.binding_sha256 or refreshed.signature_sha256 != binding.signature_sha256:
            raise AccountBindingMismatchError("private signed deployment binding changed")
        account = self._retry_mt5_read("account_info")
        try:
            refreshed.assert_broker_account(account)
        except DeploymentBindingError as exc:
            raise AccountBindingMismatchError(str(exc)) from exc

    def _account_key(self) -> str:
        return str(self.config.get("account_key") or self.config.get("account_login") or "")

    def _assert_super1_lease(self, output_root: Path, *, for_order: bool = True) -> dict[str, Any]:
        app_root = Path(__file__).resolve().parents[1]
        _, _, manifest_sha256 = load_runtime_manifest(app_root)
        bindings = runtime_binding_expectations(Path(output_root).resolve().parent, self.config)
        lease = load_lease(
            Path(output_root).resolve().parent,
            self.config,
            manifest_sha256,
            for_order=for_order,
            expected_bindings=bindings,
        )
        lease_path = Path(output_root).resolve().parent / "control" / "session-lease.json"
        self._last_lease_binding = {
            "binding_kind": "LIVE_LEASE",
            "lease_id": str(lease.get("lease_id") or ""),
            "release_id": str(lease.get("release_id") or ""),
            "runner_sid": str(lease.get("runner_sid") or ""),
            "invocation_nonce": str(lease.get("invocation_nonce") or ""),
            "lease_sha256": file_sha256(lease_path),
            "config_sha256": str(lease.get("config_sha256") or ""),
            "candidate_sha256": str(lease.get("candidate_sha256") or ""),
            "harness_sha256": str(lease.get("harness_sha256") or ""),
            "manifest_sha256": str(lease.get("app_manifest_sha256") or ""),
            "release_manifest_sha256": str(lease.get("release_manifest_sha256") or ""),
        }
        self._active_lease = dict(lease)
        return lease

    def _approval_store(self, output_root: Path) -> ApprovalStore:
        return ApprovalStore(
            self._order_db(output_root),
            halt=lambda reason, **details: core.write_fatal_latch(
                output_root, reason, reason, **details
            ),
        )

    def _proposal_record(
        self,
        decision: dict[str, Any],
        *,
        symbol: str,
        reward_r: float,
        filter_state: dict[str, Any],
        prefix_record: dict[str, Any] | None,
        prefix_raw: bytes | None,
        send_now: pd.Timestamp | None,
        pending_request: dict[str, object],
    ) -> dict[str, Any]:
        """Build the byte-stable proposal from signed leg/prefix evidence."""
        leg_key = str(decision.get("leg_key") or "")
        leg = self.config.get("legs", {}).get(leg_key)
        if not isinstance(leg, dict):
            raise Super1RuntimeError("signed leg contract is missing")
        signed_instrument = str(leg.get("symbol") or "").strip()
        signed_symbol = str(leg.get("epic") or "").strip()
        if not signed_instrument or not signed_symbol or signed_symbol != symbol:
            raise Super1RuntimeError("decision leg does not match the signed instrument contract")
        if not isinstance(prefix_record, dict):
            raise Super1RuntimeError("proposal requires a persisted prefix record")
        recorded_at = str(prefix_record.get("recorded_at") or prefix_record.get("decision_produced_at") or "").strip()
        if not recorded_at:
            raise Super1RuntimeError("proposal decision time is missing from prefix evidence")
        context = self._candidate_send_context(decision, prefix_record, send_now)
        expiration = pending_request.get("expiration")
        if expiration is None or context.get("expiration") is None or int(expiration) != int(context["expiration"]):
            raise Super1RuntimeError("proposal expiration is not the exact pending-request trade-window expiration")
        try:
            decision_time = pd.Timestamp(recorded_at).tz_convert("UTC").isoformat()
            expires_at = pd.Timestamp(int(expiration), unit="s", tz="UTC").isoformat()
            entry = self._derived_entry(decision, reward_r)
        except (TypeError, ValueError, OverflowError) as exc:
            raise Super1RuntimeError("proposal time or geometry is invalid") from exc
        fields = self._filter_evidence_fields(decision, filter_state, "FILTER_ALLOW", prefix_record, prefix_raw)
        evidence = {
            "order_id": str(decision.get("order_id") or ""),
            "thesis_id": str(decision.get("thesis_id") or ""),
            "prefix_sha256": str(fields["raw_sha256"]),
            "filter_evidence_sha256": str(fields["filter_evidence_sha256"]),
        }
        if not all(str(evidence[key]).strip() for key in evidence):
            raise Super1RuntimeError("proposal evidence binding is incomplete")
        candidate_hash = str(self.config.get("candidate_artifact_sha256") or "").strip()
        if not candidate_hash:
            raise Super1RuntimeError("signed candidate artifact hash is missing")
        return {
            "proposal_id": str(decision.get("order_id") or ""),
            "candidate_hash": candidate_hash,
            "instrument_id": signed_instrument,
            "broker_symbol": signed_symbol,
            "direction": str(decision.get("direction") or ""),
            "entry_price": entry,
            "stop_price": float(decision["stop_price"]),
            "target_price": float(decision["target_price"]),
            "decision_time": decision_time,
            "expires_at": expires_at,
            "evidence_hash": hashlib.sha256(core.canonical_json(evidence).encode("utf-8")).hexdigest(),
            "prefix_sha256": evidence["prefix_sha256"],
            "filter_evidence_sha256": evidence["filter_evidence_sha256"],
            "thesis_id": evidence["thesis_id"],
            "approval_type": "limit",
            "wire_request_hash": wire_request_hash(pending_request),
        }

    def _stage_or_load_approval(
        self,
        output_root: Path,
        proposal: dict[str, Any],
    ) -> tuple[ApprovalStore, Any | None, dict[str, Any], dict[str, Any] | None]:
        campaign_id = str(self.config.get("campaign_id") or "")
        account_key = self._account_key()
        release_id = str(self.config.get("release_id") or "")
        candidate_hash = str(proposal["candidate_hash"])
        store = self._approval_store(output_root)
        current = store.get_proposal(str(proposal["proposal_id"]))
        if current is None:
            store.stage(
                proposal,
                campaign_id=campaign_id,
                account_key=account_key,
                release_id=release_id,
                candidate_hash=candidate_hash,
                approval_type="limit",
            )
            current = store.get_proposal(str(proposal["proposal_id"]))
        elif current.get("proposal_hash") != proposal_hash(proposal) or current.get("proposal") != proposal or any(
            current.get(key) != value for key, value in {
                "campaign_id": campaign_id,
                "account_key": account_key,
                "release_id": release_id,
                "candidate_hash": candidate_hash,
            }.items()
        ):
            store.stage(
                proposal,
                campaign_id=campaign_id,
                account_key=account_key,
                release_id=release_id,
                candidate_hash=candidate_hash,
                approval_type="limit",
            )
            current = store.get_proposal(str(proposal["proposal_id"]))
        approval = store.find_approved(
            str(proposal["proposal_id"]),
            campaign_id=campaign_id,
            account_key=account_key,
            release_id=release_id,
            candidate_hash=candidate_hash,
        )
        return store, approval, proposal, current

    def _insert_canonical_audit(self, connection: Any, order_id: str, event_type: str, payload: dict[str, Any]) -> str:
        """Append the lifecycle audit and anchor-outbox rows in one transaction.

        The external Windows Event Log delivery is deliberately performed only
        after the caller commits this transaction.
        """
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_chain_events (
                schema_version INTEGER NOT NULL,
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                occurred_at_utc TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                account_key TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_canonical_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_anchor_outbox (
                event_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL UNIQUE,
                event_hash TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                queued_at_utc TEXT NOT NULL,
                attempted_at_utc TEXT,
                acked_at_utc TEXT,
                last_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        row = connection.execute("SELECT sequence,event_hash FROM audit_chain_events ORDER BY sequence DESC LIMIT 1").fetchone()
        sequence = 1 if row is None else int(row[0]) + 1
        previous = GENESIS_HASH if row is None else str(row[1])
        event_id = uuid4().hex
        occurred = core.utc_now().isoformat()
        body = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "sequence": sequence,
            "event_id": event_id,
            "occurred_at_utc": occurred,
            "campaign_id": str(payload.get("campaign_id") or ""),
            "account_key": str(payload.get("account_key") or ""),
            "entity_type": "order",
            "entity_id": order_id,
            "event_type": event_type,
            "payload": payload,
            "previous_hash": previous,
        }
        digest = event_hash(previous, body)
        connection.execute(
            "INSERT INTO audit_chain_events(schema_version,sequence,event_id,occurred_at_utc,campaign_id,account_key,entity_type,entity_id,event_type,payload_canonical_json,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (AUDIT_SCHEMA_VERSION, sequence, event_id, occurred, body["campaign_id"], body["account_key"], "order", order_id, event_type, core.canonical_json(payload), previous, digest),
        )
        connection.execute(
            "INSERT INTO audit_anchor_outbox(event_id,sequence,event_hash,state,queued_at_utc) VALUES(?,?,?,?,?)",
            (event_id, sequence, digest, "PENDING", occurred),
        )
        return digest

    def _deliver_audit_anchors(self, output_root: Path) -> None:
        """Deliver committed anchors and persist their ACKs, never in a DB transaction."""
        callback = getattr(self, "_audit_anchor_callback", None)
        if not callable(callback):
            raise core.CriticalLiveError("production audit anchor callback is required")
        connection = self._ready_order_connection(output_root)
        try:
            rows = connection.execute(
                "SELECT event_id,sequence,event_hash FROM audit_anchor_outbox "
                "WHERE state IN ('PENDING','DELIVERY_ATTEMPTED') ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()
        for event_id, sequence, digest in rows:
            connection = self._ready_order_connection(output_root)
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT state,event_hash FROM audit_anchor_outbox WHERE event_id=?",
                    (str(event_id),),
                ).fetchone()
                if current is None or str(current[1]) != str(digest):
                    raise core.CriticalLiveError("audit anchor outbox binding is corrupt")
                if str(current[0]) == "ACKED":
                    connection.commit()
                    continue
                connection.execute(
                    "UPDATE audit_anchor_outbox SET state='DELIVERY_ATTEMPTED',attempted_at_utc=?,last_error='' WHERE event_id=?",
                    (core.utc_now().isoformat(), str(event_id)),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                connection.close()
                raise
            finally:
                if not connection.in_transaction:
                    connection.close()
            try:
                callback(str(digest), str(event_id))
            except Exception as exc:
                failed = self._ready_order_connection(output_root)
                try:
                    failed.execute(
                        "UPDATE audit_anchor_outbox SET state='FAILED',last_error=? WHERE event_id=?",
                        (f"{type(exc).__name__}: {exc}", str(event_id)),
                    )
                finally:
                    failed.close()
                raise core.CriticalLiveError("production audit anchor delivery failed") from exc
            ack = self._ready_order_connection(output_root)
            try:
                ack.execute("BEGIN IMMEDIATE")
                if ack.execute(
                    "UPDATE audit_anchor_outbox SET state='ACKED',acked_at_utc=? WHERE event_id=? AND event_hash=? AND state='DELIVERY_ATTEMPTED'",
                    (core.utc_now().isoformat(), str(event_id), str(digest)),
                ).rowcount != 1:
                    raise core.CriticalLiveError("audit anchor ACK persistence failed")
                ack.commit()
            except Exception:
                ack.rollback()
                raise
            finally:
                ack.close()

    def _ensure_demo(self) -> None:
        super()._ensure_demo()

    def preflight_order_transport(self, output_root: Path, now: pd.Timestamp) -> dict[str, object]:
        with order_mutex():
            self._assert_super1_lease(output_root, for_order=False)
            assert_account_binding(self.mt5, self.config)
            return super().preflight_order_transport(output_root, now)

    def _signed_production_contract(
        self, leg_key: str, symbol: str
    ) -> tuple[InstrumentRegistry, InstrumentContract, str]:
        leg = self.config.get("legs", {}).get(leg_key)
        if not isinstance(leg, dict):
            raise Super1RuntimeError("signed production leg is missing")
        instrument_id = str(leg.get("instrument_id") or "").strip()
        registry_path = str(leg.get("instrument_registry_path") or "").strip()
        registry_hash = str(leg.get("instrument_registry_sha256") or "").strip()
        if (
            not instrument_id
            or not registry_path
            or not _SHA256_PATTERN.fullmatch(registry_hash)
            or str(leg.get("epic") or "") != symbol
        ):
            raise Super1RuntimeError("signed instrument registry binding is incomplete")
        identity = None
        if self.config.get("expected_server") and self.config.get("expected_company"):
            identity = {"server": str(self.config["expected_server"]), "company": str(self.config["expected_company"])}
        registry = InstrumentRegistry.from_signed_json(
            _safe_repo_file(registry_path, "instrument registry"), registry_hash,
            broker_identity=identity,
        )
        info = self._retry_mt5_read("symbol_info", symbol)
        if info is None:
            raise xm.BrokerStateUnknownError("signed production symbol metadata is unavailable")
        contract = registry.symbol_info(symbol, info)
        tick = self._retry_mt5_read("symbol_info_tick", symbol)
        reference_price = validate_current_tick(contract, tick)
        validate_economic_semantics(contract, self.mt5.order_calc_profit, reference_price=reference_price)
        if contract.instrument_id != instrument_id:
            raise Super1RuntimeError("signed instrument ID does not match the registry")
        return registry, contract, registry_hash

    def _production_snapshot(
        self,
        output_root: Path,
        symbol: str,
        contract: InstrumentContract,
        registry: InstrumentRegistry,
        health: StrategyHealth,
        now: pd.Timestamp,
        approval: bool,
    ) -> BrokerSnapshot:
        controller = HaltController(output_root)
        ingestor = self._terminal_deal_ingestor(output_root, now)
        ingestor.ingest(observed_at=now.to_pydatetime())
        history_days = int(self.config.get("risk_rule", {}).get("terminal_history_days", 365))
        history_start = (now - pd.Timedelta(days=history_days)).to_pydatetime()
        history_end = now.to_pydatetime()

        def belongs_to_super1(row: dict[str, Any]) -> bool:
            return (
                int(row.get("magic", -1) or -1) == self.magic
                and str(row.get("comment") or "").startswith(str(self.config.get("order_comment_prefix") or ""))
            )

        try:
            policy = _signed_live_risk_policy(self.config)
            return BrokerFactsBuilder(
                read=self._retry_mt5_read,
                order_calc_profit=self.mt5.order_calc_profit,
                order_calc_margin=self.mt5.order_calc_margin,
                halt_reader=lambda: controller.read() is not None,
                strategy_health_reader=lambda: health.state,
                starting_risk_reader=ingestor.starting_risk_cash,
                strategy_matcher=belongs_to_super1,
                mutex=order_mutex,
                whitelist_instrument_ids=tuple(dict(policy.instrument_whitelist)),
                contract_resolver=lambda broker_symbol: registry.symbol_info(
                    broker_symbol, self._retry_mt5_read("symbol_info", broker_symbol)
                ),
            ).build(
                now=now.to_pydatetime(),
                contract=contract,
                candidate_hash=str(self.config.get("candidate_artifact_sha256") or ""),
                deals_start=history_start,
                deals_end=history_end,
                approval=approval,
                policy_hash=policy.policy_hash,
            )
        except BrokerFactsError as exc:
            controller.trigger("BROKER_FACTS_UNKNOWN", error=str(exc), symbol=symbol)
            self._emergency_flatten(output_root, "BROKER_FACTS_UNKNOWN")
            raise xm.BrokerStateUnknownError(str(exc)) from exc

    def _terminal_query_batch(self, output_root: Path, start: object, end: object, queried_at: object | None = None):
        """Read and attest one closed range before allowing deal ingestion to commit."""
        # This order is part of the attestation contract.  The second range
        # query is deliberately the final broker query in the window.
        first = self._retry_mt5_read("history_deals_get", start, end)
        orders = self._retry_mt5_read("history_orders_get", start, end)
        order_rows = tuple(orders)
        tickets = {
            int(getattr(row, "ticket", 0) or getattr(row, "order", 0) or 0)
            for row in order_rows
        }
        positions = {
            str(getattr(row, "position_id", "") or getattr(row, "position", "") or "")
            for row in (*tuple(first), *order_rows)
        }
        tickets.discard(0)
        positions.discard("")
        ticket_deals = tuple(
            deal
            for ticket in sorted(tickets)
            for deal in tuple(self._retry_mt5_read("history_deals_get", ticket=ticket))
        )
        position_deals = tuple(
            deal
            for position in sorted(positions)
            for deal in tuple(self._retry_mt5_read("history_deals_get", position=position))
        )
        local_intents: list[tuple[object, ...]] = []
        local_snapshot: dict[str, object] = {"counts": {}, "hashes": {}}
        database = self._order_db(output_root)
        if database.is_file():
            connection = sqlite3.connect(database)
            try:
                tables = ("order_intents", "entry_risk_intents", "daily_risk_slots", "broker_order_bindings", "position_entry_allocations")
                for table_name in tables:
                    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
                    if not exists:
                        continue
                    rows = connection.execute(f"SELECT * FROM {table_name} ORDER BY rowid").fetchall()
                    encoded = [tuple(row) for row in rows]
                    local_snapshot["counts"][table_name] = len(encoded)
                    local_snapshot["hashes"][table_name] = hashlib.sha256(json.dumps(encoded, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()
                    if table_name == "order_intents":
                        local_intents = [(row[0], row[1]) for row in rows]
            finally:
                connection.close()
        repeated = self._retry_mt5_read("history_deals_get", start, end)
        queried_at = pd.Timestamp.now(tz="UTC").to_pydatetime()
        return build_terminal_query_batch(
            rows=tuple(first), repeated_rows=tuple(repeated), orders=order_rows,
            ticket_deals=ticket_deals, position_deals=position_deals,
            local_intents=local_intents, local_snapshot=local_snapshot,
            query_context={
                "account_key": self._account_key(),
                "campaign_id": str(self.config.get("campaign_id") or ""),
                "candidate_hash": str(self.config.get("candidate_artifact_sha256") or ""),
                "magic": self.magic,
                "comment_prefix": str(self.config.get("order_comment_prefix") or ""),
                "private_terminal_binding_hash": self._terminal_binding_hash(),
                "query_method_contract_version": "TERMINAL_DEAL_QUERY_V5",
                "reconciliation_type": "FULL",
            },
            start_utc=pd.Timestamp(start).to_pydatetime(),
            end_utc=pd.Timestamp(end).to_pydatetime(), queried_at_utc=queried_at,
        )

    def _terminal_deal_ingestor(self, output_root: Path, now: pd.Timestamp) -> TerminalDealIngestor:
        database = self._order_db(output_root)
        connection = sqlite3.connect(database, timeout=30.0)
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'order_intents'"
            ).fetchone()
            row = connection.execute("SELECT MIN(created_at) FROM order_intents").fetchone() if table else None
        finally:
            connection.close()
        campaign_start = pd.Timestamp(row[0]).tz_convert("UTC") if row and row[0] else now.tz_convert("UTC") - pd.Timedelta(days=365)
        if campaign_start >= now.tz_convert("UTC"):
            campaign_start = now.tz_convert("UTC") - pd.Timedelta(days=365)
        return TerminalDealIngestor(
            database,
            read_deals=lambda start, end: self._terminal_query_batch(output_root, start, end),
            account_key=self._account_key(),
            campaign_id=str(self.config.get("campaign_id") or ""),
            candidate_hash=str(self.config.get("candidate_artifact_sha256") or ""),
            campaign_start=campaign_start.to_pydatetime(), magic=self.magic,
            comment_prefix=str(self.config.get("order_comment_prefix") or ""),
            now=lambda: now.to_pydatetime(),
            terminal_history_days=365,
            private_terminal_binding_hash=self._terminal_binding_hash(),
        )

    def _emergency_flatten(self, output_root: Path, reason: str) -> dict[str, Any]:
        """Flatten every exact broker ticket after a production HALT."""
        controller = HaltController(output_root)
        controller.trigger(reason)

        def read_exposure() -> dict[str, Any]:
            actions: list[dict[str, Any]] = []
            for order in self._mt5_collection("orders_get"):
                ticket = int(getattr(order, "ticket", 0) or 0)
                if ticket <= 0:
                    raise xm.BrokerStateUnknownError("pending order has no exact ticket")
                actions.append({"kind": "CANCEL", "order": ticket})
            for position in self._mt5_collection("positions_get"):
                ticket = int(getattr(position, "ticket", 0) or getattr(position, "position_id", 0) or 0)
                if ticket <= 0:
                    raise xm.BrokerStateUnknownError("open position has no exact position ticket")
                actions.append({
                    "kind": "CLOSE", "position": ticket,
                    "symbol": str(getattr(position, "symbol", "")),
                    "volume": float(getattr(position, "volume", 0.0) or 0.0),
                    "type": int(getattr(position, "type", -1)),
                })
            return {"actions": actions}

        def cancel_or_close(action: Mapping[str, Any]) -> Any:
            if "position" in action:
                request = {
                    "action": int(getattr(self.mt5, "TRADE_ACTION_DEAL", 1)),
                    "position": int(action["position"]),
                    "symbol": str(action.get("symbol") or ""),
                    "volume": float(action["volume"]),
                    "type": 1 if int(action.get("type", 0)) == 0 else 0,
                }
            else:
                request = {"action": int(getattr(self.mt5, "TRADE_ACTION_REMOVE", 6)), "order": int(action["order"])}
            return self._order_send_checked(request, require_open_permission=False)

        return emergency_flatten_cycle(controller, read_exposure, cancel_or_close)

    def _send_via_production_flow(
        self,
        output_root: Path,
        decision: dict[str, Any],
        symbol: str,
        reward_r: float,
        *,
        filter_state: dict[str, Any],
        prefix_record: dict[str, Any] | None,
        prefix_raw: bytes | None,
        send_now: pd.Timestamp | None,
    ) -> dict[str, object]:
        order_id = str(decision["order_id"])
        comment = self._comment(str(decision["leg_key"]), order_id)
        registry, contract, registry_hash = self._signed_production_contract(str(decision["leg_key"]), symbol)
        request = self._pending_request(
            symbol,
            str(decision["direction"]),
            self._derived_entry(decision, reward_r),
            float(decision["stop_price"]),
            float(decision["target_price"]),
            comment,
        )
        proposal_data = self._proposal_record(
            decision,
            symbol=symbol,
            reward_r=reward_r,
            filter_state=filter_state,
            prefix_record=prefix_record,
            prefix_raw=prefix_raw,
            send_now=send_now,
            pending_request=request,
        )
        proposal_data.pop("wire_request_hash", None)
        proposal_data.pop("approval_type", None)
        proposal_data["instrument_registry_sha256"] = registry_hash
        proposal = SignalProposal(
            str(proposal_data["proposal_id"]),
            str(proposal_data["candidate_hash"]),
            str(proposal_data["instrument_id"]),
            str(proposal_data["direction"]),
            float(proposal_data["entry_price"]),
            float(proposal_data["stop_price"]),
            float(proposal_data["target_price"]),
            pd.Timestamp(proposal_data["decision_time"]).to_pydatetime(),
            pd.Timestamp(proposal_data["expires_at"]).to_pydatetime(),
            str(proposal_data["evidence_hash"]),
            broker_symbol=symbol,
            instrument_registry_sha256=registry_hash,
        )
        campaign_id = str(self.config.get("campaign_id") or "")
        account_key = self._account_key()
        release_id = str(self.config.get("release_id") or "")
        candidate_hash = str(self.config.get("candidate_artifact_sha256") or "")
        if not all((campaign_id, account_key, release_id, candidate_hash)):
            raise Super1RuntimeError("signed production campaign/release binding is incomplete")
        staged = {
            "proposal_id": proposal.proposal_id,
            "candidate_hash": proposal.candidate_hash,
            "instrument_id": proposal.instrument_id,
            "direction": proposal.direction,
            "entry_price": proposal.entry_price,
            "stop_price": proposal.stop_price,
            "target_price": proposal.target_price,
            "decision_time": proposal.decision_time.isoformat(),
            "expires_at": proposal.expires_at.isoformat(),
            "evidence_hash": proposal.evidence_hash,
            "broker_symbol": proposal.broker_symbol,
            "instrument_registry_sha256": proposal.instrument_registry_sha256,
        }
        store = self._approval_store(output_root)
        current = store.get_proposal(order_id)
        if current is None:
            store.stage(staged, campaign_id=campaign_id, account_key=account_key, release_id=release_id, candidate_hash=candidate_hash)
            current = store.get_proposal(order_id)
        elif current.get("proposal") != staged or current.get("state") not in {"STAGED", "APPROVED"}:
            store.close()
            raise Super1RuntimeError("persisted production proposal is not the exact current proposal")
        approval = store.find_approved(order_id, campaign_id=campaign_id, account_key=account_key, release_id=release_id, candidate_hash=candidate_hash)
        approval_id = "" if approval is None else approval.approval_id
        if approval is None or not approval_id:
            store.close()
            return {
                "state": "STAGED_NO_SEND",
                "persistent_state": str((current or {}).get("state") or "STAGED"),
                "order_id": order_id,
                "proposal_id": order_id,
                "proposal_hash": proposal_hash(staged),
                "filter_state": filter_state,
                "cancelled_pending": [],
            }
        store.close()
        health_path = output_root / "strategy_health.json"
        if not health_path.is_file():
            raise Super1RuntimeError("production strategy-health state is missing")
        health = StrategyHealth.load_canonical(
            self._order_db(output_root),
            baseline=_signed_health_baseline(self.config, candidate_hash),
            expected_candidate_hash=candidate_hash,
            strict_broker_order=True,
        )
        limits = _signed_risk_limits(self.config)
        policy = _signed_live_risk_policy(self.config)
        adapter = _Super1ProductionAdapter(self, output_root, request, contract)
        ledger = AuditLedger(
            self._order_db(output_root), campaign_id=campaign_id, account_key=account_key,
            anchor=getattr(self, "_audit_anchor_callback"),
        )
        snapshot_now = pd.Timestamp(core.utc_now()).tz_convert("UTC")
        dependencies = ProductionDependencies(
            risk_guard=RiskGuard(
                policy=policy,
                pair_cap_r=limits["daily_loss_cap_r"],
                daily_loss_cap_r=limits["daily_loss_cap_r"],
                base_risk_percent=float(self.config["base_risk_percent"]),
                max_total_stop_risk_percent=limits["max_total_stop_risk_percent"],
                max_margin_fraction=limits["max_margin_fraction"],
                max_leverage=limits["max_leverage"],
                max_pair_exposure_percent=limits["max_pair_exposure_percent"],
                max_concentration_percent=limits["max_concentration_percent"],
                clock=lambda: (core.utc_now() if send_now is None else pd.Timestamp(send_now)).to_pydatetime(),
            ),
            order_state=OrderStateMachine(order_id=order_id),
            audit_ledger=ledger,
            halt_controller=HaltController(output_root),
            instrument_registry=registry,
            approval_store=ApprovalStore(self._order_db(output_root), halt=lambda reason, **details: core.write_fatal_latch(output_root, reason, reason, **details)),
            runtime_settings=RuntimeSettings("DEMO_ORDER"),
            strategy_health=health,
            execution_adapter=adapter,
            deal_ingestor=self._terminal_deal_ingestor(output_root, snapshot_now),
        )
        snapshots = 0

        def snapshot_provider() -> BrokerSnapshot:
            nonlocal snapshots
            snapshots += 1
            return self._production_snapshot(output_root, symbol, contract, registry, health, snapshot_now, snapshots >= 2)

        try:
            result = ProductionOrderFlow(dependencies).send(
                proposal,
                snapshot_provider=snapshot_provider,
                approval_id=approval_id,
                campaign_id=campaign_id,
                account_key=account_key,
                release_id=release_id,
                candidate_hash=candidate_hash,
            )
        except Exception as exc:
            core.write_no_send_sentinel(output_root, "PRODUCTION_FLOW_FAILURE", order_id=order_id, error=str(exc))
            raise
        finally:
            dependencies.approval_store.close()
            ledger.close()
        ticket = int(getattr(result, "order", 0) or 0)
        if ticket <= 0 or adapter.last_request is None:
            raise core.CriticalLiveError("production accepted response has no exact persisted broker ticket")
        self._adopt_order_intent(
            output_root,
            order_id,
            comment,
            "SUBMITTED",
            {"event": "SUBMITTED", "ticket": ticket, "symbol": symbol, "production_flow": True},
            broker_ticket=ticket,
            request=adapter.last_request,
        )
        return {
            "state": "SUBMITTED",
            "order_id": order_id,
            "ticket": ticket,
            "production_flow": True,
            "filter_state": filter_state,
        }

    def _place_candidate(
        self,
        output_root: Path,
        decision: dict[str, Any],
        symbol: str,
        reward_r: float,
        **kwargs: Any,
    ) -> dict[str, object]:
        """Single live entry: serialize, verify lease/account, then legacy gate."""
        with order_mutex():
            core.assert_no_fatal_latch(output_root)
            self._assert_super1_lease(output_root, for_order=True)
            assert_account_binding(self.mt5, self.config)
            leg_key = str(decision.get("leg_key") or "")
            leg = self.config.get("legs", {}).get(leg_key)
            if not isinstance(leg, dict) or not str(leg.get("epic") or ""):
                raise Super1RuntimeError("signed leg broker symbol is missing")
            return self._place_candidate_under_mutex(
                output_root,
                decision,
                str(leg["epic"]),
                reward_r,
                **kwargs,
            )

    def _arm_send(
        self,
        output_root: Path,
        order_id: str,
        request: dict[str, object],
        event: dict[str, object],
    ) -> dict[str, object]:
        binding = getattr(self, "_last_lease_binding", None)
        lease = getattr(self, "_active_lease", None)
        approval = getattr(self, "_active_approval", None)
        if not binding or binding.get("binding_kind") != "LIVE_LEASE" or not isinstance(lease, dict) or approval is None:
            raise Super1RuntimeError("SEND_ARMED requires verified lease and approval bindings.")
        if str(approval.lease_id) != str(lease.get("lease_id")) or str(approval.lease_nonce) != str(lease.get("invocation_nonce")):
            raise Super1RuntimeError("current lease differs from approval lease")
        store = self._approval_store(output_root)

        def arm(connection: Any, request_digest: str, approval_type: str) -> None:
            active_proposal = getattr(self, "_active_proposal", {})
            if str(active_proposal.get("wire_request_hash") or "") != request_digest:
                raise core.CriticalLiveError(f"{order_id}: wire request is not the staged proposal request")
            row = connection.execute(
                "SELECT status,request_json FROM order_intents WHERE order_id=?", (order_id,)
            ).fetchone()
            if row is None or str(row[0]) not in {"INTENT", "CHECK_RETRYABLE", "PRE_SEND_DEFERRED"}:
                raise core.CriticalLiveError(f"{order_id}: durable intent is not armable")
            if row[1] is None or core.canonical_json(json.loads(str(row[1]))) != core.canonical_json(request):
                raise core.CriticalLiveError(f"{order_id}: wire request differs from durable intent")
            controls = connection.execute(
                "SELECT order_id FROM order_intents WHERE status IN ('CANCEL_ARMED','CANCEL_ACKNOWLEDGED','CANCEL_UNKNOWN','CANCEL_REJECTED')"
            ).fetchall()
            if controls:
                raise core.CriticalLiveError("durable cancellation control blocks SEND_ARMED")
            if connection.execute(
                "UPDATE order_intents SET status='SEND_ARMED',updated_at=? WHERE order_id=? AND status IN ('INTENT','CHECK_RETRYABLE','PRE_SEND_DEFERRED')",
                (core.utc_now().isoformat(), order_id),
            ).rowcount != 1:
                raise core.CriticalLiveError("SEND_ARMED CAS lost")
            payload = {
                "recorded_at": core.utc_now().isoformat(),
                "magic": self.magic,
                **event,
                "approval_id": str(approval.approval_id),
                "approval_type": approval_type,
                "wire_request_hash": request_digest,
                "campaign_id": str(self.config.get("campaign_id") or ""),
                "account_key": self._account_key(),
                "lease_binding": dict(binding),
            }
            self._insert_outbox(connection, order_id, payload)
            self._insert_canonical_audit(connection, order_id, "SEND_ARMED", payload)

        try:
            consumed = store.consume_and_arm(
                str(approval.approval_id),
                proposal=getattr(self, "_active_proposal", {}),
                campaign_id=str(self.config.get("campaign_id") or ""),
                account_key=self._account_key(),
                release_id=str(self.config.get("release_id") or ""),
                candidate_hash=str(getattr(self, "_active_proposal", {}).get("candidate_hash") or self.config.get("candidate_artifact_sha256") or ""),
                lease_nonce=str(approval.lease_nonce),
                operator_sid=str(approval.operator_sid),
                order_id=order_id,
                request=request,
                arm=arm,
            )
        except core.CriticalLiveError as exc:
            core.write_no_send_sentinel(output_root, "AUDIT_OR_ARM_FAILURE", order_id=order_id, error=str(exc))
            core.write_fatal_latch(output_root, "AUDIT_OR_ARM_FAILURE", str(exc), order_id=order_id)
            raise
        finally:
            store.close()
        try:
            self._deliver_audit_anchors(output_root)
        except Exception as exc:
            core.write_no_send_sentinel(output_root, "AUDIT_ANCHOR_UNACKED", order_id=order_id, error=str(exc))
            core.write_fatal_latch(output_root, "AUDIT_ANCHOR_UNACKED", str(exc), order_id=order_id)
            raise
        return {"armed": True, "status": "SEND_ARMED", "order_id": order_id, "approval": consumed.approval_id}

    def _transition_order_intent(
        self,
        output_root: Path,
        order_id: str,
        status: str,
        event: dict[str, object],
        broker_ticket: int | None = None,
    ) -> None:
        binding = getattr(self, "_last_lease_binding", None)
        if status in {"SUBMITTED", "SEND_UNKNOWN"}:
            if not binding or binding.get("binding_kind") != "LIVE_LEASE":
                raise Super1RuntimeError(f"{status} requires a verified LIVE_LEASE binding.")
            event = {**event, "lease_binding": dict(binding)}
        return super()._transition_order_intent(
            output_root,
            order_id,
            status,
            event,
            broker_ticket=broker_ticket,
        )

    def _final_send_gate(
        self,
        output_root: Path,
        order_id: str,
        request: dict[str, object],
        decision: dict[str, Any],
        symbol: str,
        final_context: dict[str, Any],
    ) -> None:
        """Revalidate every mutable broker/runtime fact at the send boundary."""
        del order_id, final_context
        lease = self._assert_super1_lease(output_root, for_order=True)
        expires = pd.Timestamp(str(lease["expires_at_utc"])).tz_convert("UTC")
        if (expires - pd.Timestamp(core.utc_now())).total_seconds() < 10:
            raise core.CriticalLiveError("Super1 lease has less than ten seconds remaining.")
        assert_account_binding(self.mt5, self.config)

        tick = self._retry_mt5_read("symbol_info_tick", symbol)
        info = self._retry_mt5_read("symbol_info", symbol)
        if tick is None or info is None:
            raise xm.BrokerStateUnknownError(f"{symbol}: final tick or symbol state is unavailable.")
        bid = float(getattr(tick, "bid", float("nan")))
        ask = float(getattr(tick, "ask", float("nan")))
        if not math.isfinite(bid) or not math.isfinite(ask) or bid >= ask:
            raise core.CriticalLiveError(f"{symbol}: final tick is stale, non-finite, or crossed.")
        tick_msc = getattr(tick, "time_msc", None)
        if tick_msc is None:
            tick_seconds = getattr(tick, "time", None)
            if tick_seconds is None:
                raise core.CriticalLiveError(f"{symbol}: final tick has no UTC timestamp.")
            tick_at = pd.Timestamp(int(tick_seconds), unit="s", tz="UTC")
        else:
            tick_at = pd.Timestamp(int(tick_msc), unit="ms", tz="UTC")
        age = (pd.Timestamp(core.utc_now()) - tick_at).total_seconds()
        if age < -1 or age > 5:
            raise core.CriticalLiveError(f"{symbol}: final tick is {age:.3f}s from UTC now.")

        try:
            volume = float(request["volume"])
            entry = float(request["price"])
            stop = float(request["sl"])
            target = float(request["tp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise core.CriticalLiveError(f"{symbol}: final request is incomplete.") from exc
        if not all(math.isfinite(value) and value > 0 for value in (volume, entry, stop, target)):
            raise core.CriticalLiveError(f"{symbol}: final request contains non-finite prices or volume.")
        point = float(getattr(info, "point", 0.0) or 0.0)
        tick_size = float(getattr(info, "trade_tick_size", 0.0) or point)
        if point <= 0 or tick_size <= 0:
            raise core.CriticalLiveError(f"{symbol}: final broker tick size is invalid.")
        if abs(entry / tick_size - round(entry / tick_size)) > 1e-7 or any(
            abs(value / tick_size - round(value / tick_size)) > 1e-7
            for value in (stop, target)
        ):
            raise core.CriticalLiveError(f"{symbol}: final prices are not tick aligned.")
        direction = str(decision.get("direction") or "")
        if direction == "long":
            if not stop < entry < target or not entry < ask:
                raise core.CriticalLiveError(f"{symbol}: final long limit geometry is invalid.")
            stop_distance = entry - stop
            reward_distance = target - entry
        elif direction == "short":
            if not target < entry < stop or not entry > bid:
                raise core.CriticalLiveError(f"{symbol}: final short limit geometry is invalid.")
            stop_distance = stop - entry
            reward_distance = entry - target
        else:
            raise core.CriticalLiveError(f"{symbol}: final direction is invalid.")
        broker_distance = max(
            float(getattr(info, "trade_stops_level", 0) or 0),
            float(getattr(info, "trade_freeze_level", 0) or 0),
        ) * point
        if min(stop_distance, reward_distance) < broker_distance:
            raise core.CriticalLiveError(f"{symbol}: final stops/freeze distance is too small.")
        if ask - bid > stop_distance * 0.10:
            raise core.CriticalLiveError(f"{symbol}: final spread exceeds ten percent of stop distance.")

        full_trade_mode = getattr(self.mt5, "SYMBOL_TRADE_MODE_FULL", None)
        if full_trade_mode is not None and int(getattr(info, "trade_mode", full_trade_mode)) != int(full_trade_mode):
            raise core.CriticalLiveError(f"{symbol}: final symbol trade mode is not FULL.")
        limit_flag = getattr(self.mt5, "SYMBOL_ORDER_LIMIT", None)
        if limit_flag is not None and not (int(getattr(info, "order_mode", 0)) & int(limit_flag)):
            raise core.CriticalLiveError(f"{symbol}: final symbol does not allow limit orders.")
        if int(request.get("type_filling", -1)) != int(getattr(self.mt5, "ORDER_FILLING_RETURN", request.get("type_filling", -1))):
            raise core.CriticalLiveError(f"{symbol}: final filling mode is not RETURN.")
        if int(request.get("type_time", -1)) != int(getattr(self.mt5, "ORDER_TIME_SPECIFIED", request.get("type_time", -1))):
            raise core.CriticalLiveError(f"{symbol}: final time mode is not SPECIFIED.")

        account = self._retry_mt5_read("account_info")
        if account is None:
            raise xm.BrokerStateUnknownError("Final account state is unavailable.")
        equity = float(getattr(account, "equity", 0.0) or 0.0)
        free_margin = float(getattr(account, "margin_free", float("nan")))
        if not math.isfinite(equity) or equity <= 0 or not math.isfinite(free_margin) or free_margin < 0:
            raise core.CriticalLiveError("Final equity/free-margin state is invalid.")
        order_type = int(request["type"])
        stop_loss = self.mt5.order_calc_profit(order_type, symbol, volume, entry, stop)
        margin = self.mt5.order_calc_margin(order_type, symbol, volume, entry)
        if stop_loss is None or margin is None:
            raise xm.BrokerStateUnknownError("Final profit or margin calculation is unavailable.")
        stop_risk = abs(float(stop_loss))
        margin_need = float(margin)
        if not math.isfinite(stop_risk) or not math.isfinite(margin_need) or margin_need < 0:
            raise core.CriticalLiveError("Final profit or margin calculation is invalid.")
        if margin_need > free_margin * 0.25:
            raise core.CriticalLiveError("Final margin need exceeds 25 percent of free margin.")

        orders = self._mt5_collection("orders_get")
        positions = self._mt5_collection("positions_get")
        duplicate_comments = {
            str(getattr(item, "comment", ""))
            for item in (*orders, *positions)
            if int(getattr(item, "magic", -1)) == self.magic
        }
        if duplicate_comments:
            raise core.CriticalLiveError("Duplicate Super1 thesis/leg exposure is present.")
        configs, _, runtime = core.live_strategy_objects()
        realized = self._pair_cap_state(pd.Timestamp(core.utc_now()), configs, runtime)
        if str(realized.get("state")) != "ALLOWED":
            raise core.CriticalLiveError("Daily Super1 pair-cap or unresolved broker state blocks new orders.")
        if stop_risk > equity * 0.022:
            raise core.CriticalLiveError("Aggregate Super1 worst-case stop risk exceeds 2.2 percent of equity.")
        checked = self._retry_mt5_read("order_check", request)
        if checked is None or int(getattr(checked, "retcode", -1)) != 0:
            raise core.CriticalLiveError("Final order_check failed immediately before SEND_ARMED.")

    def smoke_order(self, output_root: Path, lock: dict[str, Any]) -> dict[str, object]:
        with order_mutex():
            core.assert_no_fatal_latch(output_root)
            self._assert_super1_lease(output_root, for_order=True)
            assert_account_binding(self.mt5, self.config)
            self._require_order_permission()
            exposure_before = self._smoke_exposure()
            if any(exposure_before.values()):
                raise core.CriticalLiveError(
                    f"SMOKE requires a flat dedicated demo account: {exposure_before}"
                )
            leg = self.config.get("legs", {}).get("nq")
            if not isinstance(leg, dict):
                raise core.CriticalLiveError("SMOKE signed NQ leg is missing")
            symbol = str(leg.get("epic") or "")
            instrument_id = str(leg.get("instrument_id") or "")
            registry_path = str(leg.get("instrument_registry_path") or "")
            registry_hash = str(leg.get("instrument_registry_sha256") or "")
            if not symbol or not instrument_id or not registry_path or not _SHA256_PATTERN.fullmatch(registry_hash):
                raise core.CriticalLiveError("SMOKE signed instrument registry binding is missing")
            registry = InstrumentRegistry.from_signed_json(
                _safe_repo_file(registry_path, "SMOKE instrument registry"), registry_hash
            )
            info = self._retry_mt5_read("symbol_info", symbol)
            if info is None:
                raise xm.BrokerStateUnknownError("SMOKE symbol metadata is unavailable")
            contract = registry.symbol_info(symbol, info)
            reference_price = validate_current_tick(contract, self._retry_mt5_read("symbol_info_tick", symbol))
            validate_economic_semantics(contract, self.mt5.order_calc_profit, reference_price=reference_price)
            if contract.instrument_id != instrument_id:
                raise core.CriticalLiveError("SMOKE instrument ID does not match the signed registry")
            tick = self._retry_mt5_read("symbol_info_tick", symbol)
            if tick is None:
                raise xm.BrokerStateUnknownError("SMOKE tick is unavailable")
            now = pd.Timestamp(core.utc_now()).tz_convert("UTC")
            proposal_id = str(lock.get("smoke_proposal_id") or "smoke-" + hashlib.sha256(
                str(lock.get("runtime_config_hash") or "").encode("utf-8")
            ).hexdigest()[:24])
            comment = f"{self.config['order_comment_prefix']}:SMOKE:{proposal_id[-12:]}"[:31]
            entry = float(tick.bid) * 0.5
            request = self._pending_request(symbol, "long", entry, entry * 0.9, entry * 1.1, comment)
            proposal = SignalProposal(
                proposal_id,
                str(self.config.get("candidate_artifact_sha256") or ""),
                instrument_id,
                "long",
                float(request["price"]),
                float(request["sl"]),
                float(request["tp"]),
                (now - pd.Timedelta(seconds=1)).to_pydatetime(),
                (now + pd.Timedelta(seconds=45)).to_pydatetime(),
                hashlib.sha256(core.canonical_json({"lock": lock, "symbol": symbol}).encode("utf-8")).hexdigest(),
            )

            snapshot_calls = 0

            def snapshot(approval: bool) -> BrokerSnapshot:
                try:
                    policy = _signed_live_risk_policy(self.config)
                    ingestor = self._terminal_deal_ingestor(output_root, now)
                    ingestor.ingest(observed_at=now.to_pydatetime())
                    history_days = int(self.config.get("risk_rule", {}).get("terminal_history_days", 365))
                    return BrokerFactsBuilder(
                        read=self._retry_mt5_read,
                        order_calc_profit=self.mt5.order_calc_profit,
                        order_calc_margin=self.mt5.order_calc_margin,
                        halt_reader=lambda: HaltController(output_root).read() is not None,
                        strategy_health_reader=lambda: "ACTIVE",
                        starting_risk_reader=ingestor.starting_risk_cash,
                        strategy_matcher=lambda _row: True,
                        mutex=order_mutex,
                        whitelist_instrument_ids=tuple(dict(policy.instrument_whitelist)),
                    ).build(
                        now=now.to_pydatetime(),
                        contract=contract,
                        candidate_hash=candidate_hash,
                        deals_start=(now - pd.Timedelta(days=history_days)).to_pydatetime(),
                        deals_end=now.to_pydatetime(),
                        approval=approval,
                        policy_hash=policy.policy_hash,
                    )
                except BrokerFactsError as exc:
                    raise core.CriticalLiveError(f"SMOKE broker facts are unknown: {exc}") from exc

            campaign_id = str(self.config.get("campaign_id") or "")
            account_key = self._account_key()
            release_id = str(self.config.get("release_id") or "")
            candidate_hash = str(self.config.get("candidate_artifact_sha256") or "")
            if not all((campaign_id, account_key, release_id, candidate_hash)):
                raise core.CriticalLiveError("SMOKE signed campaign/release binding is incomplete")
            store = self._approval_store(output_root)
            existing = store.get_proposal(proposal_id)
            if existing is not None:
                existing_proposal = existing.get("proposal")
                if not isinstance(existing_proposal, dict):
                    store.close()
                    raise core.CriticalLiveError("SMOKE persisted proposal is malformed")
                persisted_wire_hash = str(existing_proposal.get("wire_request_hash") or "")
                if persisted_wire_hash and persisted_wire_hash != wire_request_hash(request):
                    store.close()
                    core.write_no_send_sentinel(output_root, "SMOKE_REQUEST_HASH_MISMATCH", proposal_id=proposal_id)
                    core.write_fatal_latch(output_root, "SMOKE_REQUEST_HASH_MISMATCH", "SMOKE request changed after staging", proposal_id=proposal_id)
                    raise core.CriticalLiveError("SMOKE request hash mismatch; HALT is active")
                if str(existing.get("state")) not in {"CONSUMED", "REJECTED"}:
                    try:
                        proposal = SignalProposal(
                            str(existing_proposal["proposal_id"]), str(existing_proposal["candidate_hash"]),
                            str(existing_proposal["instrument_id"]), str(existing_proposal["direction"]),
                            float(existing_proposal["entry_price"]), float(existing_proposal["stop_price"]),
                            float(existing_proposal["target_price"]), pd.Timestamp(existing_proposal["decision_time"]).to_pydatetime(),
                            pd.Timestamp(existing_proposal["expires_at"]).to_pydatetime(), str(existing_proposal["evidence_hash"]),
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        store.close()
                        raise core.CriticalLiveError("SMOKE persisted proposal cannot be reconstructed") from exc
            if existing is not None and str(existing.get("state")) in {"CONSUMED", "REJECTED"}:
                store.close()
                return {"state": "REPLAY_NO_SEND", "proposal_id": proposal_id, "entry_order_send_count": 0}
            approval_id = str(lock.get("approval_id") or "")
            if not approval_id:
                store.stage(
                    {**{
                        "proposal_id": proposal.proposal_id,
                        "candidate_hash": proposal.candidate_hash,
                        "instrument_id": proposal.instrument_id,
                        "direction": proposal.direction,
                        "entry_price": proposal.entry_price,
                        "stop_price": proposal.stop_price,
                        "target_price": proposal.target_price,
                        "decision_time": proposal.decision_time.isoformat(),
                        "expires_at": proposal.expires_at.isoformat(),
                        "evidence_hash": proposal.evidence_hash,
                        "approval_type": "SMOKE",
                        "wire_request": dict(request),
                        "wire_request_hash": wire_request_hash(request),
                    }},
                    campaign_id=campaign_id, account_key=account_key, release_id=release_id,
                    candidate_hash=candidate_hash, approval_type="SMOKE",
                )
                store.close()
                return {"state": "STAGED_NO_SEND", "proposal_id": proposal_id, "entry_order_send_count": 0}
            store.close()
            health_path = output_root / "strategy_health.json"
            if not health_path.is_file():
                raise core.CriticalLiveError("SMOKE strategy-health state is missing")
            health = StrategyHealth.load_canonical(
                self._order_db(output_root),
            baseline=_signed_health_baseline(self.config, candidate_hash),
            expected_candidate_hash=candidate_hash,
            strict_broker_order=True,
        )
            limits = _signed_risk_limits(self.config)
            policy = _signed_live_risk_policy(self.config)
            adapter = _Super1ProductionSmokeAdapter(self, output_root, request, contract)
            ledger = AuditLedger(
                self._order_db(output_root), campaign_id=campaign_id, account_key=account_key,
                anchor=getattr(self, "_audit_anchor_callback"),
            )
            dependencies = ProductionDependencies(
                risk_guard=RiskGuard(
                    policy=policy,
                    pair_cap_r=limits["daily_loss_cap_r"],
                    daily_loss_cap_r=limits["daily_loss_cap_r"],
                    base_risk_percent=float(self.config["base_risk_percent"]),
                    max_total_stop_risk_percent=limits["max_total_stop_risk_percent"],
                    max_margin_fraction=limits["max_margin_fraction"],
                    max_leverage=limits["max_leverage"],
                    max_pair_exposure_percent=limits["max_pair_exposure_percent"],
                    max_concentration_percent=limits["max_concentration_percent"],
                    clock=lambda: now.to_pydatetime(),
                ),
                order_state=OrderStateMachine(), audit_ledger=ledger, halt_controller=HaltController(output_root),
                instrument_registry=registry, approval_store=ApprovalStore(self._order_db(output_root), halt=lambda reason, **details: core.write_fatal_latch(output_root, reason, reason, **details)),
                runtime_settings=RuntimeSettings("SMOKE"), strategy_health=health, execution_adapter=adapter,
                deal_ingestor=self._terminal_deal_ingestor(output_root, now),
            )
            try:
                def fresh_snapshot() -> BrokerSnapshot:
                    nonlocal snapshot_calls
                    snapshot_calls += 1
                    return snapshot(snapshot_calls >= 2)

                result = ProductionOrderFlow(dependencies).send(
                    proposal, snapshot_provider=fresh_snapshot, approval_id=approval_id,
                    campaign_id=campaign_id, account_key=account_key, release_id=release_id,
                    candidate_hash=candidate_hash, approval_type="SMOKE",
                )
            except Exception as exc:
                core.write_no_send_sentinel(output_root, "SMOKE_PRODUCTION_FLOW_FAILURE", error=str(exc), proposal_id=proposal_id)
                core.write_fatal_latch(output_root, "SMOKE_PRODUCTION_FLOW_FAILURE", str(exc), proposal_id=proposal_id)
                raise
            finally:
                dependencies.approval_store.close()
                ledger.close()
            ticket = int(getattr(result, "order", 0) or 0)
            if ticket <= 0:
                raise core.CriticalLiveError("SMOKE accepted response has no exact ticket")
            self._adopt_order_intent(
                output_root, proposal_id, comment, "SUBMITTED",
                {"event": "SMOKE_SUBMITTED", "symbol": symbol, "ticket": ticket, "runtime_hash": lock.get("runtime_config_hash")},
                broker_ticket=ticket, request=request,
            )
            current = next((item for item in self._mt5_collection("orders_get", ticket=ticket) if int(getattr(item, "ticket", 0) or 0) == ticket), None)
            if current is None:
                raise core.CriticalLiveError("SMOKE pending-order readback disappeared before cancellation")
            self._write_adapter = _Super1AuthorizedMaintenanceAdapter(
                self, output_root, approval_id, campaign_id, account_key
            )
            try:
                cancelled = self._remove_order(output_root, current, "SMOKE_TEST", order_id=proposal_id)
            finally:
                del self._write_adapter
            if str(cancelled.get("state")) != "CANCELLED":
                reason = f"SMOKE cancellation did not reach a terminal CANCELLED state: {cancelled}"
                core.write_no_send_sentinel(output_root, "SMOKE_CANCEL_FAILURE", proposal_id=proposal_id, error=reason)
                core.write_fatal_latch(output_root, "SMOKE_CANCEL_FAILURE", reason, proposal_id=proposal_id)
                raise core.CriticalLiveError(reason)
            exposure_after = self._smoke_exposure()
            if any(exposure_after.values()):
                reason = f"SMOKE did not return the dedicated demo account to flat: {exposure_after}"
                core.write_no_send_sentinel(output_root, "SMOKE_FINAL_FLAT_FAILURE", proposal_id=proposal_id, error=reason)
                core.write_fatal_latch(output_root, "SMOKE_FINAL_FLAT_FAILURE", reason, proposal_id=proposal_id)
                raise core.CriticalLiveError(reason)
            return {"state": "PASS", "demo_verified": True, "symbol": symbol, "minimum_volume": contract.volume_min, "submitted_ticket": ticket, "cancelled": cancelled, "entry_order_send_count": adapter.entry_writes, **{f"{key}_after": value for key, value in exposure_after.items()}}

    def cancel_all_pending(self, output_root: Path, reason: str) -> list[dict[str, object]]:
        # Safe-stop cancellation is allowed without an active lease, but it
        # still shares the single cross-process transport mutex.
        with order_mutex():
            return super().cancel_all_pending(output_root, reason)

    def stop_reconciliation(self, output_root: Path, reason: str) -> dict[str, object]:
        with order_mutex():
            return self._stop_reconciliation_under_mutex(output_root, reason)

    def _stop_reconciliation_under_mutex(self, output_root: Path, reason: str) -> dict[str, object]:
        """Prove that the dedicated demo account is safe after cancellation."""
        del reason
        self._ensure_demo()
        expected_symbols = {
            str(self.config["legs"][key]["epic"])
            for key in core.LEG_ORDER
        }
        comment_prefix = str(self.config["order_comment_prefix"])
        orders = self._mt5_collection("orders_get")
        positions = self._mt5_collection("positions_get")
        owned_pending = 0
        foreign_exposure = 0
        unknown = 0
        for order in orders:
            try:
                owned = self._is_owned_pending_order(order)
                symbol = str(getattr(order, "symbol", ""))
                comment = str(getattr(order, "comment", ""))
                magic = int(getattr(order, "magic", -1))
            except (TypeError, ValueError):
                unknown += 1
                continue
            if owned and symbol in expected_symbols and magic == self.magic and comment.startswith(comment_prefix):
                owned_pending += 1
            else:
                foreign_exposure += 1

        protected_open = 0
        owned_positions = 0
        request_by_comment: dict[str, dict[str, Any]] = {}
        connection = self._ready_order_connection(output_root)
        try:
            for comment, request_json in connection.execute(
                "SELECT comment, request_json FROM order_intents "
                "WHERE request_json IS NOT NULL ORDER BY updated_at DESC"
            ).fetchall():
                try:
                    parsed = json.loads(str(request_json))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict) and str(comment) not in request_by_comment:
                    request_by_comment[str(comment)] = parsed
        finally:
            connection.close()
        for position in positions:
            try:
                symbol = str(getattr(position, "symbol", ""))
                comment = str(getattr(position, "comment", ""))
                magic = int(getattr(position, "magic", -1))
                stop_loss = float(getattr(position, "sl", 0.0) or 0.0)
                take_profit = float(getattr(position, "tp", 0.0) or 0.0)
            except (TypeError, ValueError):
                unknown += 1
                continue
            owned = symbol in expected_symbols and magic == self.magic and comment.startswith(comment_prefix)
            if not owned:
                foreign_exposure += 1
                continue
            owned_positions += 1
            original = request_by_comment.get(comment)
            info = self._retry_mt5_read("symbol_info", symbol)
            tick_size = float(getattr(info, "trade_tick_size", 0.0) or getattr(info, "point", 0.0) or 0.0)
            expected_sl = None if original is None else original.get("sl")
            expected_tp = None if original is None else original.get("tp")
            protection_matches = (
                original is not None
                and tick_size > 0
                and math.isfinite(stop_loss)
                and math.isfinite(take_profit)
                and expected_sl is not None
                and expected_tp is not None
                and abs(stop_loss - float(expected_sl)) <= tick_size + 1e-9
                and abs(take_profit - float(expected_tp)) <= tick_size + 1e-9
            )
            if protection_matches:
                protected_open += 1
            else:
                unknown += 1

        open_positions = len(positions)
        safe = (
            owned_pending == 0
            and foreign_exposure == 0
            and unknown == 0
            and (open_positions == 0 or protected_open == owned_positions)
        )
        return {
            "owned_pending": owned_pending,
            "open_positions": open_positions,
            "owned_positions": owned_positions,
            "protected_open": protected_open,
            "foreign_exposure": foreign_exposure,
            "unknown": unknown,
            "safe_stop": "PASS" if safe else "FAIL",
        }

    def _overnight_direction(self, symbol: str, trade_date: str) -> str:
        calendar = load_verified_rth_calendar(self.config)
        end = core.utc_now()
        start = end - pd.Timedelta(days=max(10, int(self.config.get("history_days", 10))))
        _, rates = self.prices(symbol, start, end)
        rows: list[tuple[str, pd.Timestamp, float, float]] = []
        for row in rates:
            try:
                timestamp = pd.Timestamp(str(row["snapshotTimeUTC"])).tz_convert(core.TZ)
                open_price = float(row["openPrice"]["bid"])
                close_price = float(row["closePrice"]["bid"])
            except (KeyError, TypeError, ValueError) as exc:
                raise Super1FeatureError(f"{symbol}: RTH adapter price row is invalid.") from exc
            rows.append(
                (
                    str(timestamp.date()),
                    timestamp,
                    open_price,
                    close_price,
                )
            )

        sessions: dict[str, list[tuple[pd.Timestamp, float, float]]] = {}
        for date_key, timestamp, open_price, close_price in rows:
            sessions.setdefault(date_key, []).append((timestamp, open_price, close_price))

        def session_spec(day: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp] | None:
            entry = calendar["sessions"].get(day.strftime("%Y-%m-%d"))
            if entry is None:
                raise Super1FeatureError(
                    f"{symbol}: RTH calendar has no verified session for {day.date()}"
                )
            if entry["state"] == "CLOSED":
                return None
            return (
                pd.Timestamp(f"{day.date()} {entry['start']}", tz=core.TZ),
                pd.Timestamp(f"{day.date()} {entry['end']}", tz=core.TZ),
            )

        def verified_session(
            day: pd.Timestamp,
            *,
            require_open: bool,
            require_close: bool,
        ) -> tuple[float, float] | None:
            spec = session_spec(day)
            if spec is None:
                return None
            session_start, session_end = spec
            selected = [
                item
                for item in sessions.get(str(day.date()), [])
                if session_start <= item[0] < session_end
            ]
            timestamps = [item[0] for item in selected]
            if len(timestamps) != len(set(timestamps)) or timestamps != sorted(timestamps):
                raise Super1FeatureError(
                    f"{symbol}: duplicate or out-of-order RTH bars cannot be proven."
                )
            opening = [item for item in selected if item[0] == session_start]
            closing = [item for item in selected if item[0] == session_end - pd.Timedelta(minutes=1)]
            if (require_open and len(opening) != 1) or (require_close and len(closing) != 1):
                raise Super1FeatureError(
                    f"{symbol}: causal RTH open/previous close cannot be proven for {day.date()}."
                )
            return (
                float(opening[0][1]) if opening else float("nan"),
                float(closing[0][2]) if closing else float("nan"),
            )

        current_day = pd.Timestamp(trade_date, tz=core.TZ)
        current = verified_session(current_day, require_open=True, require_close=False)
        if current is None:
            raise Super1FeatureError(f"{symbol}: current RTH opening cannot be proven.")

        previous_day = current_day - pd.Timedelta(days=1)
        previous_spec: tuple[pd.Timestamp, pd.Timestamp] | None = None
        for _ in range(370):
            previous_spec = session_spec(previous_day)
            if previous_spec is not None:
                break
            previous_day -= pd.Timedelta(days=1)
        if previous_spec is None:
            raise Super1FeatureError(f"{symbol}: previous RTH session cannot be proven.")
        previous = verified_session(previous_day, require_open=False, require_close=True)
        if previous is None:
            raise Super1FeatureError(f"{symbol}: previous RTH close cannot be proven.")
        change = current[0] - previous[1]
        return "up" if change >= 0 else "down"

    def _pending_request(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop: float,
        target: float,
        comment: str,
    ) -> dict[str, object]:
        # Strategy geometry produces only a broker-independent minimum-volume
        # request.  RiskGuard owns final sizing from the fresh broker snapshot.
        return super()._pending_request(symbol, direction, entry, stop, target, comment)

    def _filter_state(self, decision: dict[str, Any]) -> dict[str, Any]:
        if self._super1_record is None:
            raise Super1FeatureError("Super1 prefix record is unavailable.")
        trade_date = str(decision.get("date") or self._super1_record["date"])
        weekday = pd.Timestamp(trade_date).day_name()
        features: dict[str, Any] = {
            "entry_weekday": weekday,
            "direction": str(decision["direction"]),
        }
        configs, _, _ = core.live_strategy_objects()
        leg_key = str(decision["leg_key"])
        for index, rule in enumerate(self.config["setup_rules"], 1):
            if rule.get("action") != "BLOCK" or not isinstance(rule.get("conditions"), dict):
                raise Super1FeatureError("Unsupported sealed Super1 setup rule.")
            matched = True
            for feature, expected in rule["conditions"].items():
                if feature in features:
                    value = features[feature]
                elif feature == "liquidity_type":
                    value = features[feature] = liquidity_type(
                        self._super1_record, decision, configs[leg_key]
                    )
                elif feature == "overnight_direction":
                    value = features[feature] = self._overnight_direction(
                        str(self.config["legs"][leg_key]["epic"]), trade_date
                    )
                else:
                    raise Super1FeatureError(f"Unsupported Super1 candidate feature: {feature}.")
                if value != expected:
                    matched = False
                    break
            if matched:
                return {
                    "state": "BLOCK",
                    "rule": f"CANDIDATE_RULE_{index}",
                    "conditions": rule["conditions"],
                    "features": features,
                }
        return {"state": "ALLOW", "rule": None, "features": features}

    def _filter_evidence_fields(
        self,
        decision: dict[str, Any],
        filter_state: dict[str, Any],
        reason: str,
        prefix_record: dict[str, Any] | None,
        prefix_raw: bytes | None,
    ) -> dict[str, object]:
        raw = (
            prefix_raw
            if isinstance(prefix_raw, bytes)
            else (
                core.canonical_json(prefix_record).encode("utf-8")
                if isinstance(prefix_record, dict)
                else b""
            )
        )
        cutoffs = (
            dict(prefix_record.get("cutoffs") or {})
            if isinstance(prefix_record, dict)
            else {}
        )
        observed_at = (
            str(prefix_record.get("recorded_at") or prefix_record.get("decision_produced_at"))
            if isinstance(prefix_record, dict)
            else ""
        ) or core.utc_now().isoformat()
        trade_date = str(
            (prefix_record or {}).get("date") or decision.get("date") or ""
        )
        targeted_cutoff = str(cutoffs.get(str(decision["leg_key"]), ""))
        full_cutoff = core.canonical_json(cutoffs)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        evidence = {
            "filter_state": filter_state,
            "reason": reason,
            "decision": decision,
            "cutoffs": cutoffs,
            "raw_sha256": raw_sha256,
        }
        return {
            "strategy": "Super1",
            "order_id": str(decision["order_id"]),
            "state": str(filter_state["state"]),
            "trade_date": trade_date,
            "targeted_cutoff": targeted_cutoff,
            "full_cutoff": full_cutoff,
            "full_cutoffs_json": full_cutoff,
            "observed_at": observed_at,
            "raw_byte_count": len(raw),
            "raw_sha256": raw_sha256,
            "filter_evidence_sha256": hashlib.sha256(
                core.canonical_json(evidence).encode("utf-8")
            ).hexdigest(),
        }

    def _upsert_filter_evidence(self, fields: dict[str, object], output_root: Path) -> None:
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO strategy_filter_evidence (
                    strategy, order_id, state, trade_date, targeted_cutoff,
                    full_cutoff, full_cutoffs_json, observed_at, raw_byte_count,
                    raw_sha256, filter_evidence_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy, order_id) DO UPDATE SET
                    state=excluded.state,
                    trade_date=excluded.trade_date,
                    targeted_cutoff=excluded.targeted_cutoff,
                    full_cutoff=excluded.full_cutoff,
                    full_cutoffs_json=excluded.full_cutoffs_json,
                    observed_at=excluded.observed_at,
                    raw_byte_count=excluded.raw_byte_count,
                    raw_sha256=excluded.raw_sha256,
                    filter_evidence_sha256=excluded.filter_evidence_sha256,
                    updated_at=excluded.updated_at
                """,
                (
                    fields["strategy"],
                    fields["order_id"],
                    fields["state"],
                    fields["trade_date"],
                    fields["targeted_cutoff"],
                    fields["full_cutoff"],
                    fields["full_cutoffs_json"],
                    fields["observed_at"],
                    fields["raw_byte_count"],
                    fields["raw_sha256"],
                    fields["filter_evidence_sha256"],
                    now,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _promote_filter_if_newer(
        self,
        output_root: Path,
        decision: dict[str, Any],
        filter_state: dict[str, Any],
        reason: str,
        prefix_record: dict[str, Any] | None,
        prefix_raw: bytes | None,
    ) -> dict[str, object]:
        fields = self._filter_evidence_fields(
            decision, filter_state, reason, prefix_record, prefix_raw
        )
        order_id = str(decision["order_id"])
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                "SELECT status FROM order_intents WHERE order_id = ?", (order_id,)
            ).fetchone()
            old = connection.execute(
                "SELECT observed_at, full_cutoffs_json, full_cutoff, raw_sha256 "
                "FROM strategy_filter_evidence WHERE strategy = 'Super1' AND order_id = ?",
                (order_id,),
            ).fetchone()
            if intent is None or str(intent[0]) != "FILTER_UNRESOLVED_DEFERRED" or old is None:
                connection.commit()
                return {"promoted": False, "state": None if intent is None else str(intent[0])}
            try:
                old_observed = pd.Timestamp(str(old[0]))
                new_observed = pd.Timestamp(str(fields["observed_at"]))
                old_cutoffs = json.loads(str(old[1] or old[2] or "{}"))
                new_cutoffs = json.loads(str(fields["full_cutoffs_json"]))
                cutoff_non_regression = all(
                    pd.Timestamp(new_cutoffs[key]) >= pd.Timestamp(old_cutoffs[key])
                    for key in ("nq", "spx")
                )
                cutoff_advanced = any(
                    pd.Timestamp(new_cutoffs[key]) > pd.Timestamp(old_cutoffs[key])
                    for key in ("nq", "spx")
                )
                strictly_newer = (
                    new_observed > old_observed
                    and cutoff_non_regression
                    and cutoff_advanced
                    and str(fields["raw_sha256"]) != str(old[3])
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                strictly_newer = False
            if not strictly_newer:
                connection.commit()
                return {
                    "promoted": False,
                    "state": "FILTER_UNRESOLVED_DEFERRED",
                    "reason": "FILTER_EVIDENCE_NOT_STRICTLY_NEWER",
                }
            connection.execute(
                "UPDATE order_intents SET status = 'PRE_SEND_DEFERRED', updated_at = ? "
                "WHERE order_id = ? AND status = 'FILTER_UNRESOLVED_DEFERRED'",
                (now, order_id),
            )
            connection.execute(
                """
                UPDATE strategy_filter_evidence SET
                    state = 'PRE_SEND_DEFERRED', trade_date = ?, targeted_cutoff = ?,
                    full_cutoff = ?, full_cutoffs_json = ?, observed_at = ?,
                    raw_byte_count = ?, raw_sha256 = ?, filter_evidence_sha256 = ?,
                    updated_at = ?
                WHERE strategy = 'Super1' AND order_id = ?
                """,
                (
                    fields["trade_date"],
                    fields["targeted_cutoff"],
                    fields["full_cutoff"],
                    fields["full_cutoffs_json"],
                    fields["observed_at"],
                    fields["raw_byte_count"],
                    fields["raw_sha256"],
                    fields["filter_evidence_sha256"],
                    now,
                    order_id,
                ),
            )
            event = {
                "event": "SUPER1_FILTER_PROMOTED",
                "order_id": order_id,
                "reason": "NEWER_COMPLETE_FILTER_EVIDENCE",
                "observed_at": fields["observed_at"],
                "cutoffs": json.loads(str(fields["full_cutoffs_json"])),
                "raw_sha256": fields["raw_sha256"],
                "prefix_sha256": fields["raw_sha256"],
                "raw_byte_count": fields["raw_byte_count"],
                "filter_evidence_sha256": fields["filter_evidence_sha256"],
            }
            if not self._outbox_event_exists(connection, order_id, "SUPER1_FILTER_PROMOTED"):
                self._insert_outbox(connection, order_id, {"recorded_at": now, "magic": self.magic, **event})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)
        return {"promoted": True, "state": "PRE_SEND_DEFERRED"}

    def _persist_filter_terminal(
        self,
        output_root: Path,
        decision: dict[str, Any],
        filter_state: dict[str, Any],
        state: str,
        reason: str,
        prefix_record: dict[str, Any] | None,
        prefix_raw: bytes | None = None,
    ) -> dict[str, object]:
        """Persist a filter terminal outcome atomically before any broker call."""
        order_id = str(decision["order_id"])
        comment = self._comment(str(decision["leg_key"]), order_id)
        event_name = {
            "FILTER_BLOCKED": "SUPER1_FILTER_TERMINAL",
            "FILTER_UNRESOLVED_DEFERRED": "SUPER1_FILTER_DEFERRED",
            "FILTER_EXPIRED_NO_SEND": "SUPER1_FILTER_EXPIRED",
        }.get(state, "SUPER1_FILTER_TERMINAL")
        public_state = (
            "SUPER1_FILTER_BLOCKED"
            if state == "FILTER_BLOCKED"
            else "SUPER1_FILTER_UNRESOLVED_NO_SEND"
            if state == "FILTER_UNRESOLVED_DEFERRED"
            else "FILTER_EXPIRED_NO_SEND"
        )
        fields = self._filter_evidence_fields(
            decision, filter_state, reason, prefix_record, prefix_raw
        )
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT status FROM order_intents WHERE order_id = ?", (order_id,)
            ).fetchone()
            execution = connection.execute(
                "SELECT state FROM broker_execution_states WHERE order_id = ?", (order_id,)
            ).fetchone()
            if existing is not None or execution is not None:
                current = str(existing[0]) if existing is not None else str(execution[0])
                if current in {"FILTER_BLOCKED", "FILTER_UNRESOLVED_DEFERRED", "FILTER_EXPIRED_NO_SEND"}:
                    if current == "FILTER_UNRESOLVED_DEFERRED" and state == "FILTER_EXPIRED_NO_SEND":
                        connection.execute(
                            "UPDATE order_intents SET status = ?, updated_at = ? WHERE order_id = ?",
                            (state, now, order_id),
                        )
                        connection.execute(
                            """
                            UPDATE strategy_filter_evidence SET state = ?, trade_date = ?,
                                targeted_cutoff = ?, full_cutoff = ?, full_cutoffs_json = ?,
                                observed_at = ?, raw_byte_count = ?, raw_sha256 = ?,
                                filter_evidence_sha256 = ?, updated_at = ?
                            WHERE strategy = 'Super1' AND order_id = ?
                            """,
                            (
                                state,
                                fields["trade_date"],
                                fields["targeted_cutoff"],
                                fields["full_cutoff"],
                                fields["full_cutoffs_json"],
                                fields["observed_at"],
                                fields["raw_byte_count"],
                                fields["raw_sha256"],
                                fields["filter_evidence_sha256"],
                                now,
                                order_id,
                            ),
                        )
                        self._insert_outbox(
                            connection,
                            order_id,
                            {
                                "recorded_at": now,
                                "magic": int(getattr(self, "magic", self.config.get("magic_number", 0))),
                                "event": event_name,
                                "order_id": order_id,
                                "comment": comment,
                                "reason": reason,
                                "raw_sha256": fields["raw_sha256"],
                                "prefix_sha256": fields["raw_sha256"],
                                "raw_byte_count": fields["raw_byte_count"],
                                "cutoffs": json.loads(str(fields["full_cutoffs_json"])),
                                "filter_evidence_sha256": fields["filter_evidence_sha256"],
                            },
                        )
                        connection.commit()
                        transitioned = True
                    else:
                        transitioned = False
                    if transitioned:
                        pass
                    else:
                        connection.commit()
                    if transitioned:
                        result = {
                            "state": public_state,
                            "persistent_state": state,
                            "order_id": order_id,
                            "idempotent": False,
                            "filter_state": filter_state,
                            "cancelled_pending": [],
                        }
                    else:
                        result = {
                            "state": public_state,
                            "persistent_state": current,
                            "order_id": order_id,
                            "idempotent": True,
                            "filter_state": filter_state,
                            "cancelled_pending": [],
                        }
                    if transitioned:
                        self._drain_order_outbox(output_root)
                    return result
                connection.commit()
                return {
                    "state": "FILTER_DEFERRED_TO_BASE_LIFECYCLE",
                    "order_id": order_id,
                    "existing_state": current,
                }
            broker_objects = self._broker_objects(comment)
            if broker_objects:
                connection.commit()
                return {
                    "state": "FILTER_DEFERRED_TO_BASE_LIFECYCLE",
                    "order_id": order_id,
                    "reason": "broker_object_exists_before_filter_terminal",
                }
            connection.execute(
                """INSERT INTO order_intents
                   (order_id, status, comment, request_json, broker_ticket, created_at, updated_at)
                   VALUES (?, ?, ?, NULL, NULL, ?, ?)""",
                (order_id, state, comment, now, now),
            )
            connection.execute(
                """
                INSERT INTO strategy_filter_evidence (
                    strategy, order_id, state, trade_date, targeted_cutoff,
                    full_cutoff, full_cutoffs_json, observed_at, raw_byte_count,
                    raw_sha256, filter_evidence_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["strategy"], fields["order_id"], fields["state"], fields["trade_date"],
                    fields["targeted_cutoff"], fields["full_cutoff"], fields["full_cutoffs_json"],
                    fields["observed_at"], fields["raw_byte_count"], fields["raw_sha256"],
                    fields["filter_evidence_sha256"], now, now,
                ),
            )
            self._insert_outbox(
                connection,
                order_id,
                {
                    "recorded_at": now,
                    "magic": int(getattr(self, "magic", self.config.get("magic_number", 0))),
                    "event": event_name,
                    "order_id": order_id,
                    "comment": comment,
                    "reason": reason,
                    "raw_sha256": fields["raw_sha256"],
                    "prefix_sha256": fields["raw_sha256"],
                    "raw_byte_count": fields["raw_byte_count"],
                    "prefix_bytes": fields["raw_byte_count"],
                    "cutoffs": json.loads(str(fields["full_cutoffs_json"])),
                    "filter_evidence_sha256": fields["filter_evidence_sha256"],
                    "request_json": None,
                    "broker_ticket": None,
                },
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)
        return {
            "state": public_state,
            "persistent_state": state,
            "order_id": order_id,
            "filter_state": filter_state,
            "cancelled_pending": [],
        }

    def _place_candidate_under_mutex(
        self,
        output_root: Path,
        decision: dict[str, Any],
        symbol: str,
        reward_r: float,
        *,
        prefix_record: dict[str, Any] | None = None,
        prefix_raw: bytes | None = None,
        send_now: pd.Timestamp | None = None,
    ) -> dict[str, object]:
        order_id = str(decision["order_id"])
        comment = self._comment(str(decision["leg_key"]), order_id)

        try:
            filter_state = self._filter_state(decision)
        except Super1FeatureError as exc:
            unresolved = {
                    "state": "UNRESOLVED",
                    "rule": None,
                    "features": {
                        "entry_weekday": pd.Timestamp(
                            str(decision.get("date") or "")
                        ).day_name(),
                        "direction": str(decision.get("direction") or ""),
                    },
                }
            guard = self._candidate_send_context(
                decision,
                prefix_record,
                pd.Timestamp(send_now()) if callable(send_now) else send_now,
            )
            unresolved_state = (
                "FILTER_EXPIRED_NO_SEND"
                if guard["state"] == "WINDOW_EXPIRED"
                else "FILTER_UNRESOLVED_DEFERRED"
            )
            return self._persist_filter_terminal(
                output_root,
                decision,
                unresolved,
                unresolved_state,
                str(exc),
                prefix_record,
                prefix_raw,
            )
        if filter_state["state"] == "BLOCK":
            return self._persist_filter_terminal(
                output_root,
                decision,
                filter_state,
                "FILTER_BLOCKED",
                "FILTER_RULE_BLOCK",
                prefix_record,
                prefix_raw,
            )
        prior_filter = self._intent_state(output_root, order_id)
        if prior_filter and prior_filter["status"] == "FILTER_UNRESOLVED_DEFERRED":
            guard = self._candidate_send_context(
                decision,
                prefix_record,
                pd.Timestamp(send_now()) if callable(send_now) else send_now,
            )
            if guard["state"] == "WINDOW_EXPIRED":
                return self._persist_filter_terminal(
                    output_root,
                    decision,
                    filter_state,
                    "FILTER_EXPIRED_NO_SEND",
                    "FILTER_WINDOW_EXPIRED",
                    prefix_record,
                    prefix_raw,
                )
            promotion = self._promote_filter_if_newer(
                output_root,
                decision,
                filter_state,
                "NEWER_COMPLETE_FILTER_EVIDENCE",
                prefix_record,
                prefix_raw,
            )
            if not promotion.get("promoted"):
                return {
                    "state": "SUPER1_FILTER_UNRESOLVED_NO_SEND",
                    "persistent_state": "FILTER_UNRESOLVED_DEFERRED",
                    "order_id": order_id,
                    "reason": promotion.get("reason", "FILTER_EVIDENCE_NOT_STRICTLY_NEWER"),
                    "filter_state": filter_state,
                    "cancelled_pending": [],
                }
        elif not prior_filter or prior_filter["status"] not in {
            "FILTER_BLOCKED",
            "FILTER_EXPIRED_NO_SEND",
        }:
            self._upsert_filter_evidence(
                self._filter_evidence_fields(
                    decision,
                    filter_state,
                    "FILTER_ALLOW",
                    prefix_record,
                    prefix_raw,
                ),
                output_root,
            )
        broker_objects = self._broker_objects(comment)
        if broker_objects:
            raise core.CriticalLiveError(
                f"{order_id}: broker object exists before the canonical production flow: "
                f"{[kind for kind, _ in broker_objects]}"
            )
        # Risk, approval, health, and broker writes are owned by
        # ProductionOrderFlow.  Super1 supplies only the filtered signal and
        # its causal evidence to that coordinator.
        try:
            proposal_context = self._candidate_send_context(
                decision,
                prefix_record,
                pd.Timestamp(send_now()) if callable(send_now) else send_now,
            )
            if proposal_context["state"] != "ALLOW":
                state = str(proposal_context["state"])
                if state == "STALE_PREFIX":
                    self._defer_pre_send(output_root, order_id, comment, proposal_context)
                elif state in {"WINDOW_EXPIRED", "WINDOW_NOT_OPEN"}:
                    self._terminal_no_send(
                        output_root,
                        order_id,
                        comment,
                        state,
                        {"event": state, "order_id": order_id, "comment": comment, **proposal_context},
                    )
                elif state == "PREFIX_MISSING":
                    self._terminal_no_send(
                        output_root,
                        order_id,
                        comment,
                        "PREFIX_REQUIRED",
                        {"event": "PREFIX_REQUIRED", "order_id": order_id, "comment": comment, **proposal_context},
                    )
                return {
                    "state": f"{state}_NO_SEND",
                    "order_id": order_id,
                    "reason_code": state,
                    "filter_state": filter_state,
                }
            try:
                result = self._send_via_production_flow(
                    output_root,
                    decision,
                    symbol,
                    reward_r,
                    filter_state=filter_state,
                    prefix_record=prefix_record,
                    prefix_raw=prefix_raw,
                    send_now=pd.Timestamp(send_now()) if callable(send_now) else send_now,
                )
            except (ApprovalError, Super1RuntimeError, xm.CandidateRetryableError, xm.CandidateNotExecutableError) as exc:
                return {
                    "state": "PROPOSAL_INVALID_NO_SEND",
                    "persistent_state": "PROPOSAL_INVALID",
                    "order_id": order_id,
                    "reason": str(exc),
                    "filter_state": filter_state,
                    "cancelled_pending": [],
                }
            return {
                **result,
                "filter_state": filter_state,
                "filter_features": filter_state["features"],
            }
        finally:
            self._active_approval = None
            self._active_proposal = None

    def reconcile_orders(
        self,
        output_root: Path,
        prefix: dict[str, Any],
        now: pd.Timestamp,
        lock: dict[str, Any],
    ) -> dict[str, object]:
        self._super1_record = None
        self._prefix_raw_path = None
        self._prefix_raw_bytes = None
        if prefix.get("path") and Path(str(prefix["path"])).exists():
            prefix_path = Path(str(prefix["path"]))
            prefix_raw = prefix_path.read_bytes()
            parsed = json.loads(prefix_raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise Super1FeatureError("Super1 prefix JSON root must be an object.")
            self._super1_record = parsed
            self._prefix_raw_path = prefix_path
            self._prefix_raw_bytes = prefix_raw
        try:
            result = super().reconcile_orders(output_root, prefix, now, lock)
            result["strategy"] = "Super1"
            result["deployment_mode"] = str(self.config["deployment_mode"])
            return result
        finally:
            self._super1_record = None
            self._prefix_raw_path = None
            self._prefix_raw_bytes = None


def configure_core() -> None:
    runtime = core.read_json(RUNTIME_CONFIG)
    validate_super1_candidate(runtime)
    if runtime.get("deployment_binding_required") is not True:
        raise Super1FeatureError("Super1 V5 requires a private signed deployment binding.")
    def load_bound_runtime() -> dict[str, Any]:
        try:
            binding = load_verified_deployment_binding(
                runtime["deployment_binding_path"],
                runtime["deployment_binding_signature_path"],
                ROOT / runtime["deployment_binding_public_key_path"],
            )
        except (KeyError, DeploymentBindingError) as exc:
            raise Super1FeatureError("Private signed deployment binding is unavailable; no-send.") from exc
        return {
            **runtime,
            "account_login": int(binding.binding["account_login"]),
            "expected_server": str(binding.binding["server"]),
            "expected_company": str(binding.binding["company"]),
            "account_key": binding.account_key,
            "_verified_deployment_binding": binding,
        }
    core.runtime_config = load_bound_runtime
    xm.RUNTIME_CONFIG = RUNTIME_CONFIG
    core.RUNTIME_CONFIG = RUNTIME_CONFIG
    core.SCRIPT_PATH = Path(__file__).resolve()
    core.HARNESS_PATHS = (
        Path(__file__).resolve(),
        Path(xm.__file__).resolve(),
        Path(core.__file__).resolve(),
        FORWARD_SHADOW_ADAPTER.resolve(),
        (ROOT / str(runtime["candidate_path"])).resolve(),
        (ROOT / str(runtime["signal_contract_path"])).resolve(),
        SUPER1_MANIFEST.resolve(),
        (ROOT / "scripts" / "super1_runtime_guard.py").resolve(),
    )
    core.REQUIRED_ENV = REQUIRED_ENV
    if "--credential-stdin" in sys.argv:
        password_holder: dict[str, str | None] = {"value": None}

        def provide_credentials() -> dict[str, str]:
            bound_runtime = load_bound_runtime()
            if password_holder["value"] is None:
                password_holder["value"] = sys.stdin.readline().rstrip("\r\n")
            if not password_holder["value"]:
                raise Super1FeatureError("Transient broker credential was not provided on stdin.")
            return {
                "XM_MT5_SERVER": str(bound_runtime["expected_server"]),
                "XM_MT5_TERMINAL_PATH": str(runtime["terminal_path"]),
                "XM_MT5_READ_ONLY_PASSWORD": password_holder["value"],
            }

        core.CREDENTIAL_PROVIDER = provide_credentials
    else:
        core.CREDENTIAL_PROVIDER = lambda: None
    core.CapitalDemoClient = Super1XmMt5DemoOrderClient
    core.install_xm_scheduled_gap_integrity()


def main() -> None:
    configure_core()
    core.main()


if __name__ == "__main__":
    main()
