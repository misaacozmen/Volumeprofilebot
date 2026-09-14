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
INDEPENDENT_BASE_CODE_HASH = "bb333e7a5790b2b9d18e933707cc8f310d1ba55bff91ff611778c63da2bab42b"


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


def validate_super1_v4_candidate(
    path: str | Path,
    root: str | Path,
    require_promotable: bool,
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
    if not isinstance(candidate_binding, Mapping) or dict(candidate_binding) != dict(binding):
        raise CandidateValidationError("candidate promotion_bindings differ from the V4 manifest")

    checked: dict[str, str] = {}
    for key, value in binding.items():
        if not key.endswith("_path"):
            continue
        digest_key = "candidate_file_sha256" if key == "candidate_path" else key[:-5] + "_sha256"
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
    candidate_file_hash = _require_hash(
        binding.get("candidate_file_sha256", manifest.get("candidate_file_sha256")),
        "V4 candidate_file_sha256",
    )
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
    if candidate.get("promotion_bindings", {}).get("data_manifest_sha256") != data_hash:
        raise CandidateValidationError("V4 candidate data binding is invalid")
    risk_payload = {
        "risk_limits": config.get("risk_limits"),
        "live_risk_policy": config.get("live_risk_policy"),
        "risk_rule": config.get("risk_rule"),
    }
    risk_hash = sha256(json.dumps(risk_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    if binding.get("risk_policy_sha256", manifest.get("risk_policy_sha256")) != risk_hash:
        raise CandidateValidationError("V4 risk policy hash mismatch")

    if require_promotable:
        coverage = data_manifest.get("coverage")
        if manifest.get("promotion_blocker") not in (None, ""):
            raise CandidateValidationError("V4 promotion blocker is active")
        if data_manifest.get("promotion_status") not in {"VERIFIED_COMPLETE", "COMPLETE"}:
            raise CandidateValidationError("V4 data manifest is not complete")
        if not isinstance(coverage, Mapping) or int(coverage.get("invalid_sessions", -1)) != 0 or int(coverage.get("missing_sessions", -1)) != 0:
            raise CandidateValidationError("V4 promotion requires zero invalid/missing coverage")
    return {
        "candidate_path": candidate_relative,
        "manifest_path": manifest_path.relative_to(root_path).as_posix(),
        "candidate_artifact_sha256": artifact,
        "config_sha256": config_hash,
        "data_manifest_sha256": data_hash,
        "promotion_blocker": manifest.get("promotion_blocker"),
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
