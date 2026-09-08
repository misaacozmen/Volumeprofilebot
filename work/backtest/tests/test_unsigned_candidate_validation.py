from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import shutil
import sys

import pytest

from backtest.candidate_validation import CandidateValidationError, candidate_artifact_hash, validate_promotable_candidate, validate_unsigned_candidate
from backtest.engine_pipeline import source_code_hash


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_super1_xm_mt5_forward as super1


CANDIDATE = ROOT / "research_candidates/super1/super1_unsigned_candidate_v2.json"
CONFIG = ROOT / "live_forward/super1_xm_mt5_demo_config_v2.json"
CALENDAR = ROOT / "live_forward/calendars/us_equity_rth_2022_2026_v2.json"
MANIFEST = ROOT / "outputs/reports/engine_reliability_audit_fresh_20260908_final5/run_manifest.json"


def _copy_candidate_inputs(root: Path) -> Path:
    candidate = root / "research_candidates/super1/super1_unsigned_candidate_v2.json"
    for source, relative in (
        (CANDIDATE, candidate.relative_to(root)),
        (CONFIG, Path("live_forward/super1_xm_mt5_demo_config_v2.json")),
        (CALENDAR, Path("live_forward/calendars/us_equity_rth_2022_2026_v2.json")),
        (MANIFEST, Path("outputs/reports/engine_reliability_audit_fresh_20260908_final5/run_manifest.json")),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return candidate


def _rebind_fixture_to_current_source(root: Path, candidate: Path) -> None:
    code_hash = source_code_hash()
    config_path = root / "live_forward/super1_xm_mt5_demo_config_v2.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["strategy_health_baseline"]["candidate_hash"] = code_hash
    config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    manifest_path = root / "outputs/reports/engine_reliability_audit_fresh_20260908_final5/run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["code_hash"] = code_hash
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["code_hash"] = code_hash
    payload["health_baseline_candidate_hash"] = code_hash
    payload["config_sha256"] = sha256(config_path.read_bytes()).hexdigest()
    payload["data_manifest_sha256"] = sha256(manifest_path.read_bytes()).hexdigest()
    payload["candidate_artifact_sha256"] = candidate_artifact_hash(payload)
    candidate.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def test_historical_unsigned_candidate_is_rejected_after_source_change(tmp_path: Path) -> None:
    candidate = _copy_candidate_inputs(tmp_path)
    with pytest.raises(CandidateValidationError, match="current source"):
        validate_unsigned_candidate(candidate, root=tmp_path)


def test_candidate_code_or_artifact_mutation_fails_closed(tmp_path: Path) -> None:
    candidate = _copy_candidate_inputs(tmp_path)
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["code_hash"] = "0" * 64
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CandidateValidationError, match="artifact hash mismatch"):
        validate_unsigned_candidate(candidate, root=tmp_path)


def test_candidate_calendar_mutation_fails_closed(tmp_path: Path) -> None:
    candidate = _copy_candidate_inputs(tmp_path)
    _rebind_fixture_to_current_source(tmp_path, candidate)
    calendar = tmp_path / "live_forward/calendars/us_equity_rth_2022_2026_v2.json"
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    payload["early_close_dates"] = [*payload["early_close_dates"], "2026-07-02"]
    calendar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CandidateValidationError, match="calendar hash mismatch"):
        validate_unsigned_candidate(candidate, root=tmp_path)


def test_canonical_v2_calendar_excludes_july_second_but_blocks_without_raw_sources() -> None:
    runtime = json.loads(CONFIG.read_text(encoding="utf-8"))
    calendar = json.loads(CALENDAR.read_text(encoding="utf-8"))
    assert "2026-07-02" not in calendar["early_close_dates"]
    with pytest.raises(super1.Super1FeatureError, match="raw source bytes are not sealed"):
        super1.load_verified_rth_calendar(runtime)


def test_promotable_validator_rejects_forward_shadow_not_ready(tmp_path: Path) -> None:
    candidate = _copy_candidate_inputs(tmp_path)
    _rebind_fixture_to_current_source(tmp_path, candidate)
    with pytest.raises(CandidateValidationError, match="forward_shadow_ready"):
        validate_promotable_candidate(candidate, root=tmp_path)
