from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("run_capital_forward", ROOT / "scripts" / "run_capital_forward.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_normalize_price_uses_bid_and_volume() -> None:
    row = MODULE.normalize_price(
        {
            "snapshotTimeUTC": "2026-07-24T13:30:00",
            "openPrice": {"bid": 100.0, "ask": 101.0},
            "highPrice": {"bid": 102.0, "ask": 103.0},
            "lowPrice": {"bid": 99.0, "ask": 100.0},
            "closePrice": {"bid": 101.5, "ask": 102.5},
            "lastTradedVolume": 7,
        }
    )
    assert row == {
        "bar_time_utc": "2026-07-24T13:30:00+00:00",
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.5,
        "volume": 7.0,
    }


def test_aggregate_records_source_minutes_and_requires_closed_bucket() -> None:
    times = pd.date_range("2026-07-24 09:30", periods=5, freq="1min", tz="America/New_York")
    frame = pd.DataFrame(
        {
            "time": times,
            "open": [1, 2, 3, 4, 5],
            "high": [2, 3, 4, 5, 6],
            "low": [0, 1, 2, 3, 4],
            "close": [1.5, 2.5, 3.5, 4.5, 5.5],
            "volume": [1, 1, 1, 1, 1],
            "known_time": pd.date_range("2026-07-24 13:31", periods=5, freq="1min", tz="UTC"),
        }
    )
    result = MODULE.aggregate_minutes(
        frame,
        3,
        pd.Timestamp("2026-07-24 09:35:08", tz="America/New_York"),
    )
    assert len(result) == 1
    assert result.iloc[0]["time"] == times[0]
    assert result.iloc[0]["source_minute_count"] == 3


def test_aggregate_keeps_partial_bucket_when_one_source_minute_has_no_ticks() -> None:
    times = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-08-13 04:10", tz="America/New_York"),
            pd.Timestamp("2026-08-13 04:12", tz="America/New_York"),
            pd.Timestamp("2026-08-13 04:13", tz="America/New_York"),
            pd.Timestamp("2026-08-13 04:14", tz="America/New_York"),
        ]
    )
    frame = pd.DataFrame(
        {
            "time": times,
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [101.0, 102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0, 102.0],
            "close": [100.5, 101.5, 102.5, 103.5],
            "volume": [1.0, 1.0, 1.0, 1.0],
            "known_time": times.tz_convert("UTC") + pd.Timedelta(minutes=1),
        }
    )

    result = MODULE.aggregate_minutes(
        frame,
        5,
        pd.Timestamp("2026-08-13 04:16", tz="America/New_York"),
    )

    assert len(result) == 1
    assert result.iloc[0]["time"] == times[0]
    assert result.iloc[0]["source_minute_count"] == 4
    assert result.iloc[0]["open"] == 100.0
    assert result.iloc[0]["close"] == 103.5
    assert result.iloc[0]["volume"] == 4


def test_conflicting_revision_is_recorded_without_overwrite() -> None:
    first = {
        "snapshotTimeUTC": "2026-07-24T13:30:00",
        "openPrice": {"bid": 100},
        "highPrice": {"bid": 102},
        "lowPrice": {"bid": 99},
        "closePrice": {"bid": 101},
        "lastTradedVolume": 7,
    }
    revised = {
        **first,
        "closePrice": {"bid": 101.5},
    }
    with tempfile.TemporaryDirectory() as directory:
        store = MODULE.BarStore(Path(directory))
        known = pd.Timestamp("2026-07-24T13:31:05Z")
        assert store.ingest("US100", known, [first])["inserted"] == 1
        assert store.ingest("US100", known + pd.Timedelta(minutes=1), [revised])["conflicts"] == 1
        frame = store.minute_frame(
            "US100",
            pd.Timestamp("2026-07-24T13:00:00Z"),
            pd.Timestamp("2026-07-24T14:00:00Z"),
        )
        assert frame.iloc[0]["close"] == 101
        assert store.conflict_count(
            "US100",
            pd.Timestamp("2026-07-24T13:00:00Z"),
            pd.Timestamp("2026-07-24T14:00:00Z"),
        ) == 1
        store.close()


def test_output_root_process_lock_rejects_a_second_owner(tmp_path) -> None:
    first = MODULE.OutputRootProcessLock(tmp_path).acquire()
    try:
        try:
            MODULE.OutputRootProcessLock(tmp_path).acquire()
        except MODULE.CriticalLiveError as exc:
            assert "already owns output root" in str(exc)
        else:
            raise AssertionError("A second daemon acquired the same output-root lock.")
    finally:
        first.release()

    MODULE.OutputRootProcessLock(tmp_path).acquire().release()


def test_fatal_latch_is_persistent_until_explicitly_cleared(tmp_path) -> None:
    path = MODULE.write_fatal_latch(tmp_path, "UNSAFE_OPEN_ORDERS", "readback failed")
    assert path.exists()
    try:
        MODULE.assert_no_fatal_latch(tmp_path)
    except MODULE.CriticalLiveError as exc:
        assert "Persistent fatal latch" in str(exc)
    else:
        raise AssertionError("Fatal latch did not block restart.")


def test_fetch_heartbeat_refresh_preserves_health_payload(tmp_path, monkeypatch) -> None:
    MODULE.write_health(
        tmp_path,
        "RUNNING",
        markets={"nq": {"streaming": True}},
        last_cycle={"execution": {"state": "FETCHING_MARKET_DATA"}},
    )
    monkeypatch.setattr(MODULE, "utc_now", lambda: pd.Timestamp("2026-08-14T14:00:00Z"))

    MODULE.touch_health(tmp_path, "FETCHING_MARKET_DATA")

    health = MODULE.read_json(MODULE.health_path(tmp_path))
    assert health["state"] == "RUNNING"
    assert health["markets"]["nq"]["streaming"] is True
    assert health["last_cycle"]["execution"]["state"] == "FETCHING_MARKET_DATA"
    assert health["heartbeat_phase"] == "FETCHING_MARKET_DATA"
    assert health["updated_at"] == "2026-08-14T14:00:00+00:00"


def test_run_once_promotes_unknown_broker_state_to_transient_recovery(
    tmp_path,
    monkeypatch,
) -> None:
    class Store:
        def latest_time(self, epic):
            del epic
            return None

    class UnknownBrokerClient:
        send_count = 0

        def preflight_order_transport(self, output_root, now):
            del output_root, now
            return {"state": "PASS"}

        def reconcile_orders(self, output_root, prefix, now, lock):
            del output_root, prefix, now, lock
            return {"state": "UNKNOWN_NO_SEND", "reason": "orders_get unavailable"}

        def order_send(self, request):
            del request
            type(self).send_count += 1

    runtime = {
        "history_days": 1,
        "legs": {"nq": {"epic": "NQ"}, "spx": {"epic": "SPX"}},
    }
    monkeypatch.setattr(MODULE, "runtime_config", lambda: runtime)
    monkeypatch.setattr(MODULE, "campaign_lock", lambda output_root: {"created_at": "test"})
    monkeypatch.setattr(MODULE, "utc_now", lambda: pd.Timestamp("2026-08-20T14:00:00Z"))
    monkeypatch.setattr(
        MODULE,
        "fetch_window",
        lambda client, store, output_root, epic, start, end: {"epic": epic},
    )
    monkeypatch.setattr(MODULE, "campaign_session_eligible", lambda now, lock: True)
    monkeypatch.setattr(
        MODULE,
        "run_prefix",
        lambda output_root, store, now: {"state": "VALID", "path": "prefix.json"},
    )
    monkeypatch.setattr(
        MODULE,
        "finalize_session",
        lambda output_root, store, now: (_ for _ in ()).throw(
            AssertionError("UNKNOWN broker state must stop the cycle before finalization")
        ),
    )
    client = UnknownBrokerClient()

    try:
        MODULE.run_once(tmp_path, client, Store())
    except MODULE.TransientLiveError as exc:
        assert "UNKNOWN_NO_SEND" in str(exc)
        assert "orders_get unavailable" in str(exc)
    else:
        raise AssertionError("UNKNOWN_NO_SEND was accepted as a completed daemon cycle.")

    assert client.send_count == 0


def test_unknown_broker_state_recovers_before_next_cycle_without_fatal_latch(
    tmp_path,
    monkeypatch,
) -> None:
    class FastStopEvent:
        def __init__(self) -> None:
            self.stopped = False

        def clear(self) -> None:
            self.stopped = False

        def set(self) -> None:
            self.stopped = True

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, timeout: float) -> bool:
            del timeout
            return self.stopped

    class FakeClient:
        instances = []
        send_count = 0

        def __init__(self, runtime, secrets) -> None:
            del runtime, secrets
            self.index = len(self.instances) + 1
            self.cancel_reasons = []
            self.instances.append(self)
            if self.index == 2:
                health = MODULE.read_json(MODULE.health_path(tmp_path))
                assert health["state"] == "RETRYING"
                assert health["broker_state"] == "UNKNOWN_NO_SEND"
                assert health["recovery_required"] is True

        def cancel_all_pending(self, output_root, reason):
            self.cancel_reasons.append(reason)
            if self.index == 1:
                raise MODULE.TransientLiveError("temporary broker auth failure")
            if self.index == 2 and reason == "TECHNICAL_RECOVERY":
                assert MODULE.broker_recovery_path(output_root).exists()
            return []

        def order_send(self, request) -> None:
            del request
            type(self).send_count += 1

        def close(self) -> None:
            pass

    stop_event = FastStopEvent()
    run_cycles = []

    def fake_verify(client, runtime):
        del client, runtime
        return {"nq": {"streaming": True}, "spx": {"streaming": True}}

    def fake_run_once(output_root, client, store):
        del output_root, store
        if client.index == 1:
            raise MODULE.TransientLiveError(
                "Broker state is UNKNOWN_NO_SEND; verified pending-order recovery is required"
            )
        assert client.index == 2
        run_cycles.append(client.index)
        stop_event.set()
        return {"execution": {"state": "NO_SIGNAL_NO_SEND"}}

    monkeypatch.setattr(MODULE, "_STOP_EVENT", stop_event)
    monkeypatch.setattr(MODULE, "_install_shutdown_handlers", lambda: {})
    monkeypatch.setattr(MODULE, "_restore_shutdown_handlers", lambda previous: None)
    monkeypatch.setattr(MODULE, "campaign_lock", lambda output_root: {"created_at": "test"})
    monkeypatch.setattr(
        MODULE,
        "runtime_config",
        lambda: {"execution": "MT5_DEMO_ORDERS", "poll_seconds": 0},
    )
    monkeypatch.setattr(MODULE, "credentials", lambda: {"XM_MT5_SERVER": "demo"})
    monkeypatch.setattr(MODULE, "CapitalDemoClient", FakeClient)
    monkeypatch.setattr(MODULE, "verify_markets", fake_verify)
    monkeypatch.setattr(MODULE, "run_once", fake_run_once)

    args = MODULE.argparse.Namespace(output_root=str(tmp_path))
    MODULE._daemon_locked(args, tmp_path)

    assert run_cycles == [2]
    assert FakeClient.send_count == 0
    assert FakeClient.instances[0].cancel_reasons == ["TECHNICAL_FAILURE"]
    assert FakeClient.instances[1].cancel_reasons[0] == "TECHNICAL_RECOVERY"
    assert not MODULE.broker_recovery_path(tmp_path).exists()
    assert not MODULE.fatal_latch_path(tmp_path).exists()
    health = MODULE.read_json(MODULE.health_path(tmp_path))
    assert health["state"] == "STOPPED"


def test_shutdown_cancel_readback_failure_remains_fatal(tmp_path) -> None:
    class UnreachableBroker:
        def cancel_all_pending(self, output_root, reason):
            del output_root, reason
            raise MODULE.TransientLiveError("broker readback unavailable")

    try:
        MODULE._cancel_for_stop(
            tmp_path,
            UnreachableBroker(),
            "SIGNAL_SHUTDOWN_VERIFY",
            require_readback=True,
        )
    except MODULE.CriticalLiveError as exc:
        assert "fatal latch" in str(exc)
    else:
        raise AssertionError("Controlled shutdown accepted an unverified broker readback.")

    latch = MODULE.read_json(MODULE.fatal_latch_path(tmp_path))
    assert latch["state"] == "UNSAFE_OPEN_ORDERS"
    assert latch["shutdown_reason"] == "SIGNAL_SHUTDOWN_VERIFY"


def test_recovery_cancel_rejection_remains_fatal(tmp_path) -> None:
    class RejectedCancellation:
        def cancel_all_pending(self, output_root, reason):
            del output_root, reason
            raise MODULE.CriticalLiveError("pending order remained after cancellation")

    try:
        MODULE._cancel_for_recovery(
            tmp_path,
            RejectedCancellation(),
            "TECHNICAL_RECOVERY",
            transport=True,
        )
    except MODULE.CriticalLiveError as exc:
        assert "fatal latch" in str(exc)
    else:
        raise AssertionError("A verified cancellation rejection entered retry mode.")

    latch = MODULE.read_json(MODULE.fatal_latch_path(tmp_path))
    assert latch["state"] == "UNSAFE_OPEN_ORDERS"
