from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from backtest.dukascopy_acquisition import (
    AcquisitionDeferred,
    HttpCas,
    ProviderProcessLock,
    RateLimitController,
    parse_retry_after,
    rate_limit_delay,
)
from scripts.reacquire_invalid_sessions_v5 import load_authoritative_targets


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def test_retry_after_seconds_and_http_date_are_parsed_without_sleep() -> None:
    assert parse_retry_after("12", now=NOW) == 12.0
    assert parse_retry_after("Wed, 14 Sep 2026 12:01:00 GMT", now=NOW) == 60.0
    assert parse_retry_after("not-a-date", now=NOW) is None


def test_429_delay_is_header_max_base_plus_deterministic_positive_jitter() -> None:
    delay, parsed = rate_limit_delay(1, "10", now=NOW, rng=lambda: 1)
    assert parsed == 10.0
    assert delay > 60.0


def test_persisted_circuit_defers_and_half_open_success_resets(tmp_path) -> None:
    controller = RateLimitController(tmp_path / "state.json", rng=lambda: 1)
    item = controller.record_429("datafeed.dukascopy.com", None, now=NOW)
    with pytest.raises(AcquisitionDeferred):
        controller.before_request("datafeed.dukascopy.com", now=NOW)
    later = datetime.fromisoformat(item["next_retry_at_utc"].replace("Z", "+00:00"))
    controller.before_request("datafeed.dukascopy.com", now=later)
    controller.record_success("datafeed.dukascopy.com", now=later)
    assert controller.store.read()["hosts"]["datafeed.dukascopy.com"]["circuit"] == "CLOSED"


def test_stale_lock_requires_pid_and_start_token_match(tmp_path) -> None:
    path = tmp_path / "provider.lock"
    path.write_text(json.dumps({"pid": 44, "process_start_token": "old"}), encoding="utf-8")
    lock = ProviderProcessLock(path, pid=55, token="new")
    lock.acquire(process_alive=lambda pid: True, token_for_pid=lambda pid: "current")
    assert lock.held
    lock.release()


def test_cas_is_content_addressed_and_reuses_verified_body(tmp_path) -> None:
    cas = HttpCas(tmp_path / "cas")
    digest, path = cas.put(b"locked-body")
    assert path.name == f"{digest}.bi5"
    assert cas.put(b"locked-body") == (digest, path)


def test_authoritative_inventory_has_113_targets_and_missing_nq_record() -> None:
    targets = load_authoritative_targets(
        __import__("pathlib").Path("data/provenance/dukascopy_v4/frozen_invalid_leg_days_v4.csv")
    )
    assert len(targets) == 113
    assert any(str(row["date"]) == "2025-04-16" and row["leg"] == "nq" for row in targets)
