"""Validation of unsigned candidate metadata without executing candidate code."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .engine_pipeline import source_code_hash


class CandidateValidationError(ValueError):
    pass


SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
GIT_OID_RE = re.compile(r"^[0-9a-fA-F]{40}$")
INDEPENDENT_BASE_CODE_HASH = "bb333e7a5790b2b9d18e933707cc8f310d1ba55bff91ff611778c63da2bab42b"
V4_REQUIRED_BINDING_PATHS = frozenset({
    "config_path", "calendar_path", "data_manifest_path", "signal_contract_path",
    "instrument_registry_path", "locked_oos_baseline_path", "account_binding_schema_path",
    "sandbox_protocol_path", "deal_schema_path", "candidate_path",
})
V4_REQUIRED_BINDING_HASHES = frozenset({
    "candidate_file_sha256", "config_sha256", "calendar_sha256", "data_manifest_sha256", "signal_contract_sha256",
    "instrument_registry_sha256", "locked_oos_baseline_sha256", "account_binding_schema_sha256",
    "sandbox_protocol_sha256", "deal_schema_sha256", "engine_source_sha256", "risk_policy_sha256",
})


def _file(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise CandidateValidationError(f"{label} must be repository-relative")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise CandidateValidationError(f"{label} escapes repository") from exc
    if not path.is_file():
        raise CandidateValidationError(f"{label} is missing")
    return path


def _require_hash(value: Any, label: str) -> str:
    result = str(value or "")
    if not SHA256_RE.fullmatch(result):
        raise CandidateValidationError(f"{label} is not a SHA-256 digest")
    return result.lower()


def candidate_artifact_hash(payload: Mapping[str, Any]) -> str:
    body = dict(payload)
    body.pop("candidate_artifact_sha256", None)
    return sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def validate_unsigned_candidate(
    path: str | Path,
    *,
    root: str | Path,
    expected_code_hash: str | None = None,
    expected_result_hash: str | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    candidate_path = Path(path).resolve()
    try:
        candidate_path.relative_to(root_path)
    except ValueError as exc:
        raise CandidateValidationError("candidate path escapes repository") from exc
    try:
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateValidationError("unsigned candidate is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("unsigned") is not True:
        raise CandidateValidationError("candidate must be explicitly unsigned")
    if payload.get("status") != "UNSIGNED_VALIDATION_ONLY":
        raise CandidateValidationError("candidate status is not unsigned validation-only")
    if payload.get("base_independent_code_hash") != INDEPENDENT_BASE_CODE_HASH:
        raise CandidateValidationError("candidate independent base code hash is not the required semantic baseline")
    observed_artifact = _require_hash(payload.get("candidate_artifact_sha256"), "candidate_artifact_sha256")
    if observed_artifact != candidate_artifact_hash(payload):
        raise CandidateValidationError("candidate artifact hash mismatch")
    observed_code = _require_hash(payload.get("code_hash"), "code_hash")
    if expected_code_hash is not None and observed_code != _require_hash(expected_code_hash, "expected_code_hash"):
        raise CandidateValidationError("candidate code hash differs from expected current source")
    if observed_code != source_code_hash():
        raise CandidateValidationError("candidate code hash differs from current source")
    if payload.get("health_baseline_candidate_hash") != observed_code:
        raise CandidateValidationError("candidate health baseline is not bound to current code")
    config_path = _file(root_path, str(payload.get("config_path") or ""), "candidate config")
    if _require_hash(payload.get("config_sha256"), "config_sha256") != sha256(config_path.read_bytes()).hexdigest():
        raise CandidateValidationError("candidate config hash mismatch")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateValidationError("candidate config is unreadable") from exc
    if not isinstance(config, dict):
        raise CandidateValidationError("candidate config is not an object")
    candidate_relative = candidate_path.relative_to(root_path).as_posix()
    if config.get("campaign_id") != payload.get("candidate_id") or config.get("candidate_path") != candidate_relative:
        raise CandidateValidationError("candidate config is not bound to this candidate")
    baseline = config.get("strategy_health_baseline")
    if not isinstance(baseline, dict) or baseline.get("candidate_hash") != observed_code:
        raise CandidateValidationError("candidate config health baseline is not bound to current code")
    calendar_path = _file(root_path, str(payload.get("calendar_path") or ""), "candidate calendar")
    if _require_hash(payload.get("calendar_sha256"), "calendar_sha256") != sha256(calendar_path.read_bytes()).hexdigest():
        raise CandidateValidationError("candidate calendar hash mismatch")
    calendar_ref = config.get("rth_session_calendar")
    if not isinstance(calendar_ref, dict) or calendar_ref.get("path") != payload.get("calendar_path") or calendar_ref.get("sha256") != str(payload.get("calendar_sha256") or "").lower():
        raise CandidateValidationError("candidate config calendar is not bound to this artifact")
    data_manifest_path = _file(root_path, str(payload.get("data_manifest_path") or ""), "candidate data manifest")
    if _require_hash(payload.get("data_manifest_sha256"), "data_manifest_sha256") != sha256(data_manifest_path.read_bytes()).hexdigest():
        raise CandidateValidationError("candidate data manifest hash mismatch")
    try:
        data_manifest = json.loads(data_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateValidationError("candidate data manifest is unreadable") from exc
    expected_data = payload.get("data_hashes")
    if not isinstance(expected_data, dict) or data_manifest.get("data_hashes") != expected_data:
        raise CandidateValidationError("candidate data hashes differ from the fresh audit")
    result_hash = _require_hash(payload.get("result_hash"), "result_hash")
    if expected_result_hash is not None and result_hash != _require_hash(expected_result_hash, "expected_result_hash"):
        raise CandidateValidationError("candidate result hash differs from the canonical result")
    if data_manifest.get("code_hash") != observed_code or data_manifest.get("result_hash") != result_hash:
        raise CandidateValidationError("candidate is not bound to the exact fresh audit code/result")
    return {
        "candidate_path": str(candidate_path),
        "candidate_artifact_sha256": observed_artifact,
        "code_hash": observed_code,
        "config_sha256": str(payload["config_sha256"]).lower(),
        "calendar_sha256": str(payload["calendar_sha256"]).lower(),
        "data_manifest_sha256": str(payload["data_manifest_sha256"]).lower(),
        "result_hash": result_hash,
        "data_hashes": dict(expected_data),
        "unsigned": True,
    }


def _v4_payload_hash(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    return sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateValidationError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise CandidateValidationError(f"{label} is not an object")
    return value


def _evidence_root(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise CandidateValidationError("V4 promotion evidence private root is missing")
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not resolved.is_dir():
        raise CandidateValidationError("V4 promotion evidence private root is missing")
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved
    raise CandidateValidationError("V4 promotion evidence root must be external to the repository")


def _evidence_file(evidence_root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CandidateValidationError(f"{label} path is missing")
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else evidence_root / candidate).resolve()
    try:
        resolved.relative_to(evidence_root)
    except ValueError as exc:
        raise CandidateValidationError(f"{label} escapes the private evidence root") from exc
    if not resolved.is_file():
        raise CandidateValidationError(f"{label} is missing")
    return resolved


def validate_super1_v5_candidate(
    candidate: Mapping[str, Any],
    *,
    root: str | Path,
    runtime: Mapping[str, Any],
    candidate_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    """Validate the active V5 chain after the generic artifact loader ran.

    V5 is deliberately structural and unsigned: it can authorize only the
    fresh-forward validation path, never a historical result or promotion.
    """
    root_path = Path(root).resolve()
    candidate_file = _file(root_path, Path(candidate_path).resolve().relative_to(root_path).as_posix(), "V5 candidate")
    if sha256(candidate_file.read_bytes()).hexdigest() != str(runtime.get("candidate_file_sha256") or "").lower():
        raise CandidateValidationError("V5 candidate file hash mismatch")
    if candidate.get("schema_version") != 2 or candidate.get("artifact_type") != "strategy_candidate":
        raise CandidateValidationError("V5 candidate must use the generic schema-2 strategy artifact")
    if candidate.get("status") != "UNSIGNED_VALIDATION_ONLY" or candidate.get("unsigned") is not True:
        raise CandidateValidationError("V5 candidate unsigned status is invalid")
    if candidate.get("live_enabled") is not False or candidate.get("proven") is not False or candidate.get("fresh_forward_required") is not True:
        raise CandidateValidationError("V5 candidate safety flags are invalid")
    if candidate.get("artifact_sha256") != sha256(
        json.dumps({key: value for key, value in candidate.items() if key != "artifact_sha256"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest():
        raise CandidateValidationError("V5 candidate artifact hash mismatch")
    forbidden = ("promotion", "development_result", "full_evaluation", "locked_oos", "result_sha256")

    def assert_no_legacy_keys(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if any(token in str(key).lower() for token in forbidden):
                    raise CandidateValidationError("V5 chain contains legacy OOS or promotion fields")
                assert_no_legacy_keys(item)
        elif isinstance(value, list):
            for item in value:
                assert_no_legacy_keys(item)

    assert_no_legacy_keys(candidate)
    if candidate.get("artifact_sha256") != runtime.get("candidate_artifact_sha256") or candidate.get("setup_rules") != runtime.get("setup_rules"):
        raise CandidateValidationError("V5 runtime is not bound to the candidate")
    manifest_file = _file(root_path, Path(manifest_path).resolve().relative_to(root_path).as_posix(), "V5 manifest")
    manifest = _read_object(manifest_file, "V5 manifest")
    signal_relative = str(runtime.get("signal_contract_path") or "")
    config_relative = str(runtime.get("config_path") or "")
    signal_file = _file(root_path, signal_relative, "V5 signal contract")
    config_file = _file(root_path, config_relative, "V5 config")
    if sha256(signal_file.read_bytes()).hexdigest() != str(runtime.get("signal_contract_sha256") or "").lower():
        raise CandidateValidationError("V5 signal contract hash mismatch")
    config = _read_object(config_file, "V5 config")
    signal = _read_object(signal_file, "V5 signal contract")
    assert_no_legacy_keys(manifest)
    assert_no_legacy_keys(signal)
    assert_no_legacy_keys(config)
    if (
        manifest.get("schema_version") != 5
        or manifest.get("name") != "Super1 V5"
        or manifest.get("status") != "UNSIGNED_VALIDATION_ONLY"
        or manifest.get("unsigned") is not True
        or manifest.get("live_enabled") is not False
        or manifest.get("proven") is not False
        or manifest.get("fresh_forward_required") is not True
        or manifest.get("candidate_path") != candidate_file.relative_to(root_path).as_posix()
        or manifest.get("candidate_file_sha256") != sha256(candidate_file.read_bytes()).hexdigest()
        or manifest.get("candidate_artifact_sha256") != candidate.get("artifact_sha256")
        or manifest.get("signal_contract_path") != signal_relative
        or manifest.get("signal_contract_sha256") != sha256(signal_file.read_bytes()).hexdigest()
        or manifest.get("config_path") != config_relative
        or manifest.get("config_sha256") != sha256(config_file.read_bytes()).hexdigest()
        or manifest.get("engine_source_sha256") != source_code_hash()
    ):
        raise CandidateValidationError("V5 manifest bindings are invalid")
    source_commit = str(manifest.get("source_commit") or "")
    source_tree = str(manifest.get("source_tree_sha256") or "")
    if not GIT_OID_RE.fullmatch(source_commit) or not GIT_OID_RE.fullmatch(source_tree):
        raise CandidateValidationError("V5 source commit/tree must be Git object IDs")
    if (root_path / ".git").exists():
        try:
            from scripts.git_provenance import validate_commit_tree
            validate_commit_tree(root_path, source_commit, source_tree)
        except (OSError, ValueError) as exc:
            raise CandidateValidationError("V5 source commit/tree is not a real Git binding") from exc
    signal_source = signal.get("signal_source")
    overlay = signal.get("overlay_candidate")
    transport = signal.get("demo_order_transport")
    calendar = signal.get("rth_session_calendar")
    if not isinstance(signal_source, Mapping) or not isinstance(overlay, Mapping) or not isinstance(transport, Mapping) or not isinstance(calendar, Mapping):
        raise CandidateValidationError("V5 signal contract sections are incomplete")
    source_config = _file(root_path, str(signal_source.get("config_path") or ""), "V5 source config")
    source_generator = _file(root_path, str(signal_source.get("generator_path") or ""), "V5 source generator")
    source_adapter = _file(root_path, str(signal_source.get("payload_adapter_path") or ""), "V5 source adapter")
    transport_file = _file(root_path, str(transport.get("path") or ""), "V5 transport")
    calendar_file = _file(root_path, str(calendar.get("path") or ""), "V5 calendar")
    source_hashes = (
        (source_config, signal_source.get("config_sha256")),
        (source_generator, signal_source.get("generator_sha256")),
        (source_adapter, signal_source.get("payload_adapter_sha256")),
        (transport_file, transport.get("sha256")),
        (calendar_file, calendar.get("sha256")),
    )
    if any(sha256(path.read_bytes()).hexdigest() != str(expected).lower() for path, expected in source_hashes):
        raise CandidateValidationError("V5 signal source hash binding is invalid")
    if signal_source.get("engine_source_sha256") != source_code_hash() or overlay.get("path") != candidate_file.relative_to(root_path).as_posix() or overlay.get("file_sha256") != sha256(candidate_file.read_bytes()).hexdigest() or overlay.get("artifact_sha256") != candidate.get("artifact_sha256"):
        raise CandidateValidationError("V5 overlay binding is invalid")
    if config.get("schema_version") != 5 or config.get("status") != "UNSIGNED_VALIDATION_ONLY" or config.get("unsigned") is not True or config.get("live_enabled") is not False or config.get("proven") is not False or config.get("fresh_forward_required") is not True:
        raise CandidateValidationError("V5 runtime config safety flags are invalid")
    baseline = config.get("strategy_health_baseline")
    if (
        not isinstance(baseline, Mapping)
        or baseline.get("candidate_hash") != candidate.get("artifact_sha256")
        or baseline.get("closed_trades") != 0
        or baseline.get("valid_sessions") != 0
        or baseline.get("years") != []
        or baseline.get("rolling_net_r_p05") != 0.0
        or baseline.get("drawdown_p95") != 0.0
        or baseline.get("drawdown_p99") != 0.0
        or baseline.get("locked") is not True
    ):
        raise CandidateValidationError("V5 strategy-health baseline must be an empty fresh-forward baseline")
    if config.get("candidate_path") != candidate_file.relative_to(root_path).as_posix() or config.get("candidate_file_sha256") != sha256(candidate_file.read_bytes()).hexdigest() or config.get("candidate_artifact_sha256") != candidate.get("artifact_sha256") or config.get("signal_contract_path") != signal_relative or config.get("signal_contract_sha256") != sha256(signal_file.read_bytes()).hexdigest():
        raise CandidateValidationError("V5 runtime config bindings are invalid")
    if config.get("rth_session_calendar") != dict(calendar) or config.get("setup_rules") != runtime.get("setup_rules"):
        raise CandidateValidationError("V5 calendar or setup binding is invalid")
    safety = signal.get("safety")
    deployment = manifest.get("deployment")
    if not isinstance(safety, Mapping) or not isinstance(deployment, Mapping) or safety.get("real_money_live_enabled") is not False or safety.get("real_money_execution_allowed") is not False or deployment.get("real_money_live_enabled") is not False or deployment.get("real_money_execution_allowed") is not False:
        raise CandidateValidationError("V5 live-money safety binding is invalid")
    return dict(candidate)


def validate_super1_v4_candidate(
    path: str | Path,
    root: str | Path,
    require_promotable: bool,
    *,
    trusted_root_public_key_path: str | Path | None = None,
    trusted_root_public_key_sha256: str | None = None,
    repository_identity: str | None = None,
    branch: str | None = None,
    owner_trust_policy_path: str | Path | None = None,
    owner_replay_ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate the V4-only candidate/manifest/config hash chain.

    This intentionally does not call the V2/V3 validator: old candidate schemas
    must never inherit V4 promotion semantics.
    """
    root_path = Path(root).resolve()
    candidate_path = Path(path).resolve()
    try:
        candidate_path.relative_to(root_path)
    except ValueError as exc:
        raise CandidateValidationError("V4 candidate path escapes repository") from exc
    candidate = _read_object(candidate_path, "V4 candidate")
    if int(candidate.get("schema_version", 0)) != 4:
        raise CandidateValidationError("V4 candidate schema_version is required")
    if candidate.get("unsigned") is not True or candidate.get("live_enabled") is not False or candidate.get("proven") is not False:
        raise CandidateValidationError("V4 candidate safety flags are invalid")
    artifact = _require_hash(candidate.get("artifact_sha256"), "candidate artifact_sha256")
    if artifact != _v4_payload_hash(candidate):
        raise CandidateValidationError("V4 candidate artifact hash mismatch")

    manifest_relative = str(candidate.get("manifest_path") or "")
    manifest_path = _file(root_path, manifest_relative, "V4 manifest") if manifest_relative else candidate_path.with_name("super1_manifest_v4.json")
    try:
        manifest_path.relative_to(root_path)
    except ValueError as exc:
        raise CandidateValidationError("V4 manifest path escapes repository") from exc
    if not manifest_path.is_file():
        raise CandidateValidationError("V4 manifest is missing")
    manifest = _read_object(manifest_path, "V4 manifest")
    if int(manifest.get("schema_version", 0)) != 4:
        raise CandidateValidationError("V4 manifest schema_version is required")

    binding = manifest.get("promotion_bindings", manifest.get("bindings"))
    if not isinstance(binding, Mapping):
        raise CandidateValidationError("V4 manifest promotion bindings are missing")
    candidate_binding = candidate.get("promotion_bindings")
    if not isinstance(candidate_binding, Mapping):
        raise CandidateValidationError("candidate promotion_bindings are missing")
    if set(binding) != V4_REQUIRED_BINDING_PATHS | V4_REQUIRED_BINDING_HASHES | {"candidate_artifact_sha256"}:
        raise CandidateValidationError("V4 promotion binding key set is incomplete or contains unexpected fields")
    if set(candidate_binding) != V4_REQUIRED_BINDING_PATHS:
        raise CandidateValidationError("V4 candidate static binding key set is incomplete")
    for required in V4_REQUIRED_BINDING_PATHS:
        if required not in candidate_binding or not candidate_binding.get(required):
            raise CandidateValidationError(f"V4 required promotion binding is missing: {required}")
        if candidate_binding[required] != binding.get(required):
            raise CandidateValidationError(f"V4 candidate static binding differs from manifest: {required}")

    checked: dict[str, str] = {}
    for key, value in binding.items():
        if not key.endswith("_path"):
            continue
        if key == "candidate_path":
            continue
        digest_key = key[:-5] + "_sha256"
        relative = str(value or "")
        file_path = _file(root_path, relative, f"V4 binding {key}")
        observed = sha256(file_path.read_bytes()).hexdigest()
        expected = _require_hash(
            binding.get(digest_key, manifest.get(digest_key)),
            f"V4 binding {digest_key}",
        )
        if observed != expected:
            raise CandidateValidationError(f"V4 binding hash mismatch: {key}")
        checked[key] = observed

    config_relative = str(binding.get("config_path") or manifest.get("config_path") or "")
    config_path = _file(root_path, config_relative, "V4 config")
    config = _read_object(config_path, "V4 config")
    if int(config.get("schema_version", 0)) != 4 or config.get("status") != "UNSIGNED_VALIDATION_ONLY":
        raise CandidateValidationError("V4 config schema/status is invalid")
    if config.get("candidate_path") != binding.get("candidate_path") or config.get("candidate_artifact_sha256") != artifact:
        raise CandidateValidationError("V4 config candidate binding is invalid")
    if config.get("candidate_file_sha256") != binding.get("candidate_file_sha256", manifest.get("candidate_file_sha256")):
        raise CandidateValidationError("V4 config candidate file binding is invalid")
    if config.get("rth_session_calendar", {}).get("path") != binding.get("calendar_path"):
        raise CandidateValidationError("V4 config calendar path binding is invalid")
    if config.get("rth_session_calendar", {}).get("sha256") != binding.get("calendar_sha256"):
        raise CandidateValidationError("V4 config calendar hash binding is invalid")
    config_hash = _require_hash(binding.get("config_sha256", manifest.get("config_sha256")), "V4 config_sha256")
    if sha256(config_path.read_bytes()).hexdigest() != config_hash:
        raise CandidateValidationError("V4 config hash mismatch")

    candidate_relative = candidate_path.relative_to(root_path).as_posix()
    if binding.get("candidate_path") != candidate_relative:
        raise CandidateValidationError("V4 manifest candidate path is invalid")
    candidate_file_hash = _require_hash(manifest.get("candidate_file_sha256"), "V4 candidate_file_sha256")
    if candidate_file_hash != sha256(candidate_path.read_bytes()).hexdigest():
        raise CandidateValidationError("V4 candidate file hash mismatch")
    candidate_artifact_hash = _require_hash(
        binding.get("candidate_artifact_sha256", manifest.get("candidate_artifact_sha256")),
        "V4 candidate_artifact_sha256",
    )
    if candidate_artifact_hash != artifact:
        raise CandidateValidationError("V4 candidate artifact binding is invalid")
    data_path = _file(root_path, str(binding.get("data_manifest_path") or ""), "V4 data manifest")
    data_manifest = _read_object(data_path, "V4 data manifest")
    if data_manifest.get("schema_version") != 4:
        raise CandidateValidationError("V4 data manifest schema_version is invalid")
    data_hash = _require_hash(binding.get("data_manifest_sha256"), "V4 data_manifest_sha256")
    if sha256(data_path.read_bytes()).hexdigest() != data_hash:
        raise CandidateValidationError("V4 data manifest hash mismatch")
    if binding.get("data_manifest_sha256") != data_hash:
        raise CandidateValidationError("V4 manifest data binding is invalid")
    risk_payload = {
        "risk_limits": config.get("risk_limits"),
        "live_risk_policy": config.get("live_risk_policy"),
        "risk_rule": config.get("risk_rule"),
    }
    risk_hash = sha256(json.dumps(risk_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    if binding.get("risk_policy_sha256") != risk_hash:
        raise CandidateValidationError("V4 risk policy hash mismatch")

    if require_promotable:
        coverage = data_manifest.get("coverage")
        blockers = manifest.get("promotion_blockers")
        if not isinstance(blockers, list) or blockers:
            raise CandidateValidationError("V4 promotion blocker is active")
        if data_manifest.get("promotion_status") not in {"VERIFIED_COMPLETE", "COMPLETE"}:
            raise CandidateValidationError("V4 data manifest is not complete")
        if not isinstance(coverage, Mapping) or int(coverage.get("invalid_sessions", -1)) != 0 or int(coverage.get("missing_sessions", -1)) != 0:
            raise CandidateValidationError("V4 promotion requires zero invalid/missing coverage")
        evidence = manifest.get("promotion_evidence")
        if not isinstance(evidence, Mapping):
            raise CandidateValidationError("V4 fresh promotion evidence is missing")
        evidence_root = _evidence_root(root_path, evidence.get("private_evidence_root"))
        hash_fields = ("reacquisition_manifest_sha256", "reacquisition_semantic_root_sha256", "full_history_determinism_a_sha256", "full_history_determinism_b_sha256", "reliability_determinism_a_sha256", "reliability_determinism_b_sha256", "risk_xray_sha256", "security_history_attestation_sha256", "account_rotation_attestation_sha256")
        for field in hash_fields:
            _require_hash(evidence.get(field), f"V4 promotion evidence {field}")
        for field in ("full_history_determinism_a_sha256", "full_history_determinism_b_sha256", "reliability_determinism_a_sha256", "reliability_determinism_b_sha256", "risk_xray_sha256", "security_history_attestation_sha256", "account_rotation_attestation_sha256"):
            path_field = field.removesuffix("_sha256") + "_path"
            observed = sha256(_evidence_file(evidence_root, evidence.get(path_field), f"V4 promotion evidence {path_field}").read_bytes()).hexdigest()
            if observed != str(evidence[field]).lower():
                raise CandidateValidationError(f"V4 promotion evidence {field} is not bound to the attested file")
        _require_hash(evidence.get("final_manifest_source_head_sha256"), "V4 final manifest source head SHA-256")
        reacquisition_path = _file(root_path, str(evidence.get("reacquisition_manifest_path") or ""), "V4 reacquisition manifest evidence")
        expected_reacquisition = root_path / "data/provenance/dukascopy_v4/acquisition_v5/reacquisition_manifest_v5.json"
        if reacquisition_path != expected_reacquisition:
            raise CandidateValidationError("V4 reacquisition evidence path is not the canonical final manifest")
        if not reacquisition_path.is_file() or sha256(reacquisition_path.read_bytes()).hexdigest() != str(evidence["reacquisition_manifest_sha256"]).lower():
            raise CandidateValidationError("V4 reacquisition promotion evidence is not bound to the final manifest bytes")
        try:
            from .reacquisition_contract import validate_final_manifest
            final_attestation = _evidence_file(evidence_root, evidence.get("final_manifest_attestation_path"), "V4 final manifest attestation")
            final_signature = _evidence_file(evidence_root, evidence.get("final_manifest_signature_path"), "V4 final manifest signature")
            final_public_key = _evidence_file(evidence_root, evidence.get("final_manifest_public_key_path"), "V4 final manifest public key")
            reacquisition = validate_final_manifest(
                reacquisition_path,
                provenance_root=root_path / "data/provenance/dukascopy_v4",
                inventory_path=root_path / "data/provenance/dukascopy_v4/frozen_invalid_leg_days_v4.csv",
                detached_attestation_path=final_attestation,
                detached_signature_path=final_signature,
                pinned_public_key_path=final_public_key,
                pinned_public_key_sha256=_require_hash(evidence.get("final_manifest_public_key_sha256"), "V4 final manifest public key SHA-256"),
                expected_source_head_sha256=_require_hash(evidence.get("final_manifest_source_head_sha256"), "V4 final manifest source head SHA-256"),
                trusted_root_public_key_path=Path(trusted_root_public_key_path).resolve() if trusted_root_public_key_path is not None else None,
                trusted_root_public_key_sha256=trusted_root_public_key_sha256,
                repository_identity=repository_identity,
                branch=branch,
                owner_trust_policy_path=Path(owner_trust_policy_path).resolve() if owner_trust_policy_path is not None else None,
                owner_replay_ledger_path=Path(owner_replay_ledger_path).resolve() if owner_replay_ledger_path is not None else None,
            )
        except Exception as exc:
            raise CandidateValidationError("V4 reacquisition promotion evidence is not a strict verified manifest") from exc
        if reacquisition["semantic_root_sha256"] != evidence["reacquisition_semantic_root_sha256"]:
            raise CandidateValidationError("V4 reacquisition semantic root evidence differs")
    return {
        "candidate_path": candidate_relative,
        "manifest_path": manifest_path.relative_to(root_path).as_posix(),
        "candidate_artifact_sha256": artifact,
        "config_sha256": config_hash,
        "data_manifest_sha256": data_hash,
        "promotion_blockers": list(manifest.get("promotion_blockers", [])),
        "promotable": bool(require_promotable),
        "checked_paths": checked,
    }


def validate_promotable_candidate(path: str | Path, *, root: str | Path) -> dict[str, Any]:
    """Validate promotion-only evidence in addition to the structural hash chain."""
    result = validate_unsigned_candidate(path, root=root)
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    manifest_path = _file(Path(root).resolve(), str(payload.get("data_manifest_path") or ""), "candidate data manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("forward_shadow_ready") is not True:
        raise CandidateValidationError("forward_shadow_ready must be true for promotion")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, Mapping):
        raise CandidateValidationError("promotable candidate coverage is missing")
    invalid = int(coverage.get("invalid_sessions", coverage.get("invalid_data_days", -1)))
    missing = int(coverage.get("missing_sessions", coverage.get("missing_data_days", 0)))
    if invalid != 0 or missing != 0:
        raise CandidateValidationError("promotable candidate requires zero invalid/missing coverage")
    return {**result, "promotable": True}
