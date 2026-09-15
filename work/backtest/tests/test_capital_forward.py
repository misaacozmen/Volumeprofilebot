from __future__ import annotations

import importlib.util
import copy
import json
from pathlib import Path
import tempfile

import pandas as pd
import pytest

from v08_helpers import checkpoint_if_enabled, record_if_enabled


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
        lambda output_root, store, market_data_asof, knowledge_asof: {"state": "VALID", "path": "prefix.json"},
    )
    monkeypatch.setattr(
        MODULE,
        "finalize_session",
        lambda output_root, store, **kwargs: (_ for _ in ()).throw(
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


def test_run_once_refreshes_time_after_fetch_before_reconcile(tmp_path, monkeypatch) -> None:
    timestamps = iter(
        [
            pd.Timestamp("2026-08-20T14:00:00Z"),
            pd.Timestamp("2026-08-20T14:01:00Z"),
            pd.Timestamp("2026-08-20T14:02:00Z"),
            pd.Timestamp("2026-08-20T14:03:00Z"),
        ]
    )
    seen = {}

    class Store:
        def latest_time(self, epic):
            del epic
            return None

    class Client:
        def preflight_order_transport(self, output_root, now):
            del output_root
            seen["preflight"] = now
            return {"state": "PASS"}

        def reconcile_orders(self, output_root, prefix, now, lock):
            del output_root, prefix, lock
            seen["reconcile"] = now
            return {"state": "NO_SIGNAL_NO_SEND"}

        def daily_health_report(self, output_root, now, prefix, final, lock):
            del output_root, prefix, final, lock
            seen["report"] = now
            return {"state": "READY"}

    runtime = {
        "history_days": 1,
        "legs": {"nq": {"epic": "NQ"}, "spx": {"epic": "SPX"}},
    }
    monkeypatch.setattr(MODULE, "runtime_config", lambda: runtime)
    monkeypatch.setattr(MODULE, "campaign_lock", lambda output_root: {"created_at": "test"})
    monkeypatch.setattr(MODULE, "utc_now", lambda: next(timestamps))
    monkeypatch.setattr(
        MODULE,
        "fetch_window",
        lambda client, store, output_root, epic, start, end: seen.setdefault(
            f"fetch_{epic}", end
        ) or {"epic": epic},
    )
    monkeypatch.setattr(MODULE, "campaign_session_eligible", lambda now, lock: True)
    monkeypatch.setattr(MODULE, "run_prefix", lambda output_root, store, *args: {"state": "VALID", "path": "prefix.json"})
    monkeypatch.setattr(
        MODULE,
        "finalize_session",
        lambda output_root, store, **kwargs: {"state": "SESSION_OPEN"},
    )

    result = MODULE.run_once(tmp_path, Client(), Store())

    assert seen["fetch_NQ"] == pd.Timestamp("2026-08-20T14:00:00Z")
    assert seen["fetch_SPX"] == pd.Timestamp("2026-08-20T14:00:00Z")
    assert seen["preflight"] == pd.Timestamp("2026-08-20T14:01:00Z")
    assert seen["reconcile"] == pd.Timestamp("2026-08-20T14:02:00Z")
    assert seen["report"] == pd.Timestamp("2026-08-20T14:03:00Z")
    assert result["timing"]["send_guard_at"] == "2026-08-20T14:02:00+00:00"
    assert result["timing"]["market_data_asof"] == "2026-08-20T14:00:00+00:00"


def test_run_once_without_reconcile_reports_null_send_guard(tmp_path, monkeypatch) -> None:
    timestamps = iter(
        [
            pd.Timestamp("2026-08-20T14:00:00Z"),
            pd.Timestamp("2026-08-20T14:01:00Z"),
            pd.Timestamp("2026-08-20T14:02:00Z"),
        ]
    )

    class Store:
        def latest_time(self, epic):
            del epic
            return None

    class Client:
        pass

    runtime = {"history_days": 1, "legs": {"nq": {"epic": "NQ"}, "spx": {"epic": "SPX"}}}
    monkeypatch.setattr(MODULE, "runtime_config", lambda: runtime)
    monkeypatch.setattr(MODULE, "campaign_lock", lambda output_root: {"created_at": "test"})
    monkeypatch.setattr(MODULE, "utc_now", lambda: next(timestamps))
    monkeypatch.setattr(MODULE, "fetch_window", lambda *args: {"state": "FETCHED"})
    monkeypatch.setattr(MODULE, "campaign_session_eligible", lambda now, lock: True)
    monkeypatch.setattr(MODULE, "run_prefix", lambda output_root, store, *args: {"state": "VALID"})
    monkeypatch.setattr(
        MODULE,
        "finalize_session",
        lambda output_root, store, **kwargs: {"state": "SESSION_OPEN"},
    )

    result = MODULE.run_once(tmp_path, Client(), Store())

    assert result["execution"] == {"state": "NO_ORDER_TRANSPORT"}
    assert result["timing"]["send_guard_at"] is None


def test_fetch_window_preserves_provider_observation_after_requested_asof(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    class Client:
        def prices(self, epic, start, end):
            assert epic == "NQ"
            assert end == pd.Timestamp("2026-08-20T14:00:00Z")
            return pd.Timestamp("2026-08-20T14:00:10Z"), [
                {
                    "snapshotTimeUTC": "2026-08-20T13:59:00Z",
                    "openPrice": {"bid": 100.0},
                    "highPrice": {"bid": 101.0},
                    "lowPrice": {"bid": 99.0},
                    "closePrice": {"bid": 100.5},
                    "lastTradedVolume": 1,
                }
            ]

    store = MODULE.BarStore(tmp_path)
    result = MODULE.fetch_window(
        Client(),
        store,
        tmp_path,
        "NQ",
        pd.Timestamp("2026-08-20T13:59:00Z"),
        pd.Timestamp("2026-08-20T14:00:00Z"),
    )

    assert result["market_data_asof"] == "2026-08-20T14:00:00+00:00"
    assert result["fetch_scope_end"] == "2026-08-20T14:00:00+00:00"
    assert result["provider_observation_at"] == "2026-08-20T14:00:10+00:00"
    frame = store.minute_frame(
        "NQ",
        pd.Timestamp("2026-08-20T13:59:00Z"),
        pd.Timestamp("2026-08-20T14:00:00Z"),
        pd.Timestamp("2026-08-20T14:01:00Z"),
    )
    assert frame.iloc[0]["known_time"] == pd.Timestamp("2026-08-20T14:00:10Z")
    record_if_enabled(request, evidence_token)


def test_finalize_waits_for_complete_shared_fetch_scope_without_marker(tmp_path, monkeypatch, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    configs = {
        "nq": type("Config", (), {"trade_window_end": "10:30"})(),
        "spx": type("Config", (), {"trade_window_end": "11:00"})(),
    }
    runtime = {
        "closed_bar_delay_seconds": 8,
        "legs": {"nq": {"timeframe": "3m"}, "spx": {"timeframe": "5m"}},
    }
    monkeypatch.setattr(MODULE, "runtime_config", lambda: runtime)
    monkeypatch.setattr(MODULE, "live_strategy_objects", lambda: (configs, None, None))

    result = MODULE.finalize_session(
        tmp_path,
        object(),
        market_data_asof=pd.Timestamp("2026-07-29T14:58:00Z"),
        knowledge_asof=pd.Timestamp("2026-07-29T15:05:10Z"),
        finalization_asof=pd.Timestamp("2026-07-29T15:05:10Z"),
        fetches={
            "nq": {"fetch_scope_end": "2026-07-29T14:58:00Z"},
            "spx": {"fetch_scope_end": "2026-07-29T14:58:00Z"},
        },
    )

    assert result["state"] == "WAIT_FOR_FINAL_DATA"
    assert result["missing_scope"] == {"spx": "2026-07-29T14:58:00Z"}
    assert not (tmp_path / "finalized").exists()
    record_if_enabled(request, evidence_token)


def test_finalize_shared_asof_boundaries_use_real_two_leg_store_and_seal_once(
    tmp_path,
    monkeypatch,
    request,
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    runtime = json.loads((ROOT / "live_forward" / "super1_xm_mt5_demo_config_v4.json").read_text(encoding="utf-8"))
    runtime["manual_required"] = False
    monkeypatch.setattr(MODULE, "runtime_config", lambda: runtime)
    parent_lock = copy.deepcopy(MODULE.read_json(MODULE.PARENT_BASELINE))
    parent_lock["engine_manifest"]["code_hash"] = MODULE.source_code_hash()
    monkeypatch.setattr(MODULE, "validate_parent_baseline", lambda: parent_lock)

    trade_date = pd.Timestamp("2026-07-29", tz=MODULE.TZ).date()
    profile_start = pd.Timestamp("2026-07-28 18:00", tz=MODULE.TZ)
    full_end = pd.Timestamp("2026-07-29 11:00:08", tz=MODULE.TZ)
    source_times = pd.date_range(profile_start, full_end - pd.Timedelta(minutes=1), freq="1min")
    source_rows = {
        epic: [
            {
                "snapshotTimeUTC": item.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S"),
                "openPrice": {"bid": 100.0 + index * 0.001},
                "highPrice": {"bid": 100.5 + index * 0.001},
                "lowPrice": {"bid": 99.5 + index * 0.001},
                "closePrice": {"bid": 100.25 + index * 0.001},
                "lastTradedVolume": 1,
            }
            for index, item in enumerate(source_times)
        ]
        for epic in (runtime["legs"]["nq"]["epic"], runtime["legs"]["spx"]["epic"])
    }

    class TwoLegFetchClient:
        def __init__(self, rows=source_rows):
            self.rows = rows

        def prices(self, epic, start, end):
            rows = [
                row
                for row in self.rows[epic]
                if start <= pd.Timestamp(row["snapshotTimeUTC"], tz="UTC") < end
            ]
            return end + pd.Timedelta(seconds=10), rows

    root = tmp_path / "valid"
    store = MODULE.BarStore(root)
    try:
        fetches = {
            key: MODULE.fetch_window(
                TwoLegFetchClient(),
                store,
                root,
                runtime["legs"][key]["epic"],
                profile_start,
                full_end,
            )
            for key in MODULE.LEG_ORDER
        }
        for shared_asof in (
            pd.Timestamp("2026-07-29 10:58:00", tz=MODULE.TZ),
            pd.Timestamp("2026-07-29 11:00:00", tz=MODULE.TZ),
            pd.Timestamp("2026-07-29 11:00:07", tz=MODULE.TZ),
        ):
            result = MODULE.finalize_session(
                root,
                store,
                market_data_asof=shared_asof,
                knowledge_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
                finalization_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
                fetches=fetches,
            )
            assert result["state"] == "WAIT_FOR_FINAL_DATA"
            assert not (root / "finalized").exists()
            assert not (root / "sessions").exists()

        result = MODULE.finalize_session(
            root,
            store,
            market_data_asof=pd.Timestamp("2026-07-29 11:00:08", tz=MODULE.TZ),
            knowledge_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
            finalization_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
            fetches=fetches,
        )
        assert result["state"] == "VALID"
        marker = root / "finalized" / f"{trade_date}.json"
        assert marker.exists()
        session = MODULE.read_json(Path(result["path"]) / "session.json")
        assert session["data_valid"] is True
        marker_bytes = marker.read_bytes()
        marker_hash = MODULE.object_hash(MODULE.read_json(marker))
        repeated = MODULE.finalize_session(
            root,
            store,
            market_data_asof=pd.Timestamp("2026-07-29 11:00:08", tz=MODULE.TZ),
            knowledge_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
            finalization_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
            fetches=fetches,
        )
        assert repeated["state"] == "ALREADY_FINALIZED"
        assert marker.read_bytes() == marker_bytes
        assert MODULE.object_hash(MODULE.read_json(marker)) == marker_hash
    finally:
        store.close()

    incomplete_rows = {
        epic: [
            row for row in rows
            if not row["snapshotTimeUTC"].startswith("2026-07-29T13:00:00")
        ]
        for epic, rows in source_rows.items()
    }
    incomplete_root = tmp_path / "incomplete"
    incomplete_store = MODULE.BarStore(incomplete_root)
    try:
        incomplete_fetches = {
            key: MODULE.fetch_window(
                TwoLegFetchClient(incomplete_rows),
                incomplete_store,
                incomplete_root,
                runtime["legs"][key]["epic"],
                profile_start,
                full_end,
            )
            for key in MODULE.LEG_ORDER
        }
        incomplete = MODULE.finalize_session(
            incomplete_root,
            incomplete_store,
            market_data_asof=pd.Timestamp("2026-07-29 11:00:08", tz=MODULE.TZ),
            knowledge_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
            finalization_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
            fetches=incomplete_fetches,
        )
        assert incomplete["state"] == "DATA_INVALID"
        incomplete_session = MODULE.read_json(Path(incomplete["path"]) / "session.json")
        assert incomplete_session["data_valid"] is False
        assert any(
            issue["code"] in {"MISSING_BARS", "INCOMPLETE_SOURCE_MINUTES"}
            for gate in incomplete_session["data_gates"].values()
            for issue in gate["issues"]
        )
    finally:
        incomplete_store.close()

    conflict_root = tmp_path / "conflict"
    conflict_store = MODULE.BarStore(conflict_root)
    try:
        conflict_fetches = {
            key: MODULE.fetch_window(
                TwoLegFetchClient(),
                conflict_store,
                conflict_root,
                runtime["legs"][key]["epic"],
                profile_start,
                full_end,
            )
            for key in MODULE.LEG_ORDER
        }
        conflict_row = copy.deepcopy(source_rows["US100"][0])
        conflict_row["closePrice"] = {"bid": float(conflict_row["closePrice"]["bid"]) + 0.1}
        conflict_store.ingest(
            "US100",
            pd.Timestamp("2026-07-29 11:00:08", tz=MODULE.TZ),
            [conflict_row],
        )
        conflicting = MODULE.finalize_session(
            conflict_root,
            conflict_store,
            market_data_asof=pd.Timestamp("2026-07-29 11:00:08", tz=MODULE.TZ),
            knowledge_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
            finalization_asof=pd.Timestamp("2026-07-29 11:05:10", tz=MODULE.TZ),
            fetches=conflict_fetches,
        )
        assert conflicting["state"] == "DATA_INVALID"
        conflicting_session = MODULE.read_json(Path(conflicting["path"]) / "session.json")
        assert any(
            issue["code"] == "CONFLICTING_DUPLICATE_OHLC"
            for gate in conflicting_session["data_gates"].values()
            for issue in gate["issues"]
        )
    finally:
        conflict_store.close()
    record_if_enabled(request, evidence_token)


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

        def stop_reconciliation(self, output_root, reason):
            del output_root, reason
            return {
                "safe_stop": "PASS",
                "owned_pending": 0,
                "open_positions": 0,
                "unknown_exposure": 0,
            }

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
    assert latch["state"] == "UNSAFE_STOP_NO_SEND"
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


def test_finalize_future_known_required_bar_waits_for_knowledge_without_artifacts(monkeypatch, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    configs = {
        "nq": type("Config", (), {"trade_window_end": "10:30"})(),
        "spx": type("Config", (), {"trade_window_end": "11:00"})(),
    }
    runtime = {
        "closed_bar_delay_seconds": 8,
        "legs": {"nq": {"timeframe": "3m"}, "spx": {"timeframe": "5m"}},
    }
    monkeypatch.setattr(MODULE, "runtime_config", lambda: runtime)
    monkeypatch.setattr(MODULE, "live_strategy_objects", lambda: (configs, None, None))
    result = MODULE.finalize_session(
        request.config._tmp_path_factory.mktemp("future-known-output"),
        object(),
        market_data_asof=pd.Timestamp("2026-07-29T15:00:00Z"),
        knowledge_asof=pd.Timestamp("2026-07-29T15:05:00Z"),
        finalization_asof=pd.Timestamp("2026-07-29T16:00:00Z"),
        fetches={
            key: {
                "fetch_scope_end": "2026-07-29T15:00:00Z",
                "provider_observation_at": "2026-07-29T15:06:00Z",
                "data_known_at": "2026-07-29T15:06:00Z",
            }
            for key in configs
        },
    )
    assert result["state"] == "WAIT_FOR_KNOWLEDGE"
    assert result["excluded_future_known_count"] == 4
    record_if_enabled(request, evidence_token)


def test_finalize_missing_provider_metadata_waits_for_final_data_without_artifacts(monkeypatch, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    configs = {
        "nq": type("Config", (), {"trade_window_end": "10:30"})(),
        "spx": type("Config", (), {"trade_window_end": "11:00"})(),
    }
    runtime = {
        "closed_bar_delay_seconds": 8,
        "legs": {"nq": {"timeframe": "3m"}, "spx": {"timeframe": "5m"}},
    }
    monkeypatch.setattr(MODULE, "runtime_config", lambda: runtime)
    monkeypatch.setattr(MODULE, "live_strategy_objects", lambda: (configs, None, None))
    result = MODULE.finalize_session(
        request.config._tmp_path_factory.mktemp("missing-metadata-output"),
        object(),
        market_data_asof=pd.Timestamp("2026-07-29T15:00:00Z"),
        knowledge_asof=pd.Timestamp("2026-07-29T15:05:00Z"),
        finalization_asof=pd.Timestamp("2026-07-29T16:00:00Z"),
        fetches={key: {"fetch_scope_end": "2026-07-29T15:00:00Z"} for key in configs},
    )
    assert result["state"] == "WAIT_FOR_FINAL_DATA"
    assert result["missing_metadata"] == {
        "nq": ["provider_observation_at", "data_known_at"],
        "spx": ["provider_observation_at", "data_known_at"],
    }
    record_if_enabled(request, evidence_token)
