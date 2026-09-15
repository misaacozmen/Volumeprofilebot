from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from backtest.dukascopy_acquisition import AcquisitionDeferred, RateLimitController
from backtest.reacquisition_contract import _validate_detached_attestation, apply_verified_reacquisitions


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
HOST = "datafeed.dukascopy.com"
ROOT = Path(__file__).resolve().parents[1]


def test_record_success_preserves_spacing_and_cooldown_state(tmp_path: Path) -> None:
    controller = RateLimitController(tmp_path / "state.json", rng=lambda: 1)
    limited = controller.record_429(HOST, None, now=NOW, transport_fixture=True)
    later = datetime.fromisoformat(limited["next_retry_at_utc"].replace("Z", "+00:00"))
    request = controller.before_request(HOST, now=later, transport_fixture=True)
    controller.finish_request(request, status=200, body_sha256="a" * 64, body_byte_count=1, finished_at=later, transport_fixture=True)
    controller.record_success(HOST, now=later + timedelta(seconds=1), transport_fixture=True)
    state = controller.store.read()["hosts"][HOST]
    assert state["last_provider_start_at_utc"] == later.isoformat().replace("+00:00", "Z")
    assert state["next_retry_at_utc"] == limited["next_retry_at_utc"]


def test_http_status_updates_are_serialized_and_persisted(tmp_path: Path) -> None:
    controller = RateLimitController(tmp_path / "state.json")
    controller.record_http_status(503)
    controller.record_http_status(503)
    assert controller.store.read()["http_status_counts"] == {"503": 2}


def test_apply_verified_reacquisition_accepts_nested_target_schema(tmp_path: Path) -> None:
    derived = tmp_path / "derived.csv"
    pd.DataFrame({"time": ["2025-01-01T00:00:00Z"], "close": [2.0]}).to_csv(derived, index=False)
    loaded = {("DUKASCOPY_USATECHIDXUSD", "3m"): pd.DataFrame({"time": ["2025-01-01T00:00:00Z"], "close": [1.0]})}
    result = apply_verified_reacquisitions(
        loaded,
        {"targets": [{"target": {"date": "2025-01-01", "leg": "nq", "timeframe": "3m"}, "derived_path": "derived.csv"}]},
        provenance_root=tmp_path,
        frame_loader=pd.read_csv,
    )
    assert result[("DUKASCOPY_USATECHIDXUSD", "3m")]["close"].tolist() == [2.0]


def test_apply_verified_reacquisition_rejects_unknown_loaded_dataset(tmp_path: Path) -> None:
    derived = tmp_path / "derived.csv"
    pd.DataFrame({"time": ["2025-01-01T00:00:00Z"], "close": [2.0]}).to_csv(derived, index=False)
    with pytest.raises(ValueError, match="not loaded"):
        apply_verified_reacquisitions(
            {},
            {"targets": [{"target": {"date": "2025-01-01", "leg": "nq", "timeframe": "3m"}, "derived_path": "derived.csv"}]},
            provenance_root=tmp_path,
            frame_loader=pd.read_csv,
        )


def test_final_manifest_requires_external_detached_attestation_and_pinned_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="detached final attestation"):
        _validate_detached_attestation(
            tmp_path / "manifest.json",
            {},
            attestation_path=None,
            signature_path=None,
            public_key_path=None,
            pinned_public_key_sha256=None,
            expected_source_head_sha256=None,
        )


def test_reacquisition_and_node_body_timeout_contracts_are_present() -> None:
    reacquire = (ROOT / "scripts/reacquire_invalid_sessions_v5.py").read_text(encoding="utf-8")
    node = (ROOT / "tools/dukascopy-downloader/acquire_v5.mjs").read_text(encoding="utf-8")
    assert "while True:" in reacquire and "controller.record_success(HOST)" in reacquire and "break" in reacquire
    assert "reader = response.body.getReader()" in node
    assert "clearTimeout(timer)" in node
    assert "reader.cancel()" in node
