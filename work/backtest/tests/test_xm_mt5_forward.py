from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("run_xm_mt5_forward", ROOT / "scripts" / "run_xm_mt5_forward.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
MODULE.configure_core()


def test_configure_core_locks_forward_shadow_adapter() -> None:
    assert MODULE.FORWARD_SHADOW_ADAPTER.resolve() in MODULE.core.HARNESS_PATHS


def test_optional_mt5_password_reaches_client_from_environment(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "MetaTrader5", SimpleNamespace())
    monkeypatch.setenv("XM_MT5_READ_ONLY_PASSWORD", "secret-value")
    config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))

    client = MODULE.XmMt5ReadOnlyClient(config, {"XM_MT5_SERVER": "XMGlobal-MT5 7"})

    assert client.password == "secret-value"


def _login_client(mt5, *, password="", terminal_path="", portable=False):
    client = object.__new__(MODULE.XmMt5ReadOnlyClient)
    client.mt5 = mt5
    client.login_id = 318413815
    client.server = "XMGlobal-MT5 7"
    client.password = password
    client.terminal_path = terminal_path
    client.portable = portable
    client.connected = False
    return client


def test_mt5_login_primary_success_does_not_use_fallback() -> None:
    class PrimarySuccessMt5:
        def __init__(self):
            self.initialize_calls = []
            self.login_calls = []

        def initialize(self, *args, **kwargs):
            self.initialize_calls.append((args, kwargs))
            return True

        def login(self, *args, **kwargs):
            self.login_calls.append((args, kwargs))
            return True

        def account_info(self):
            return SimpleNamespace(login=318413815, server="XMGlobal-MT5 7", trade_mode=0)

        def shutdown(self):
            raise AssertionError("a valid primary session must remain connected")

    mt5 = PrimarySuccessMt5()
    client = _login_client(mt5, password="secret", terminal_path="C:/MT5/terminal64.exe", portable=True)

    result = client.login()

    assert result["login"] == 318413815
    assert len(mt5.initialize_calls) == 1
    assert mt5.initialize_calls[0][0] == ("C:/MT5/terminal64.exe",)
    assert mt5.initialize_calls[0][1]["portable"] is True
    assert mt5.login_calls == []
    assert client.connected is True


def test_mt5_login_primary_failure_uses_saved_session_fallback() -> None:
    class SavedSessionMt5:
        def __init__(self):
            self.initialize_calls = []
            self.login_calls = []
            self.shutdown_count = 0

        def initialize(self, *args, **kwargs):
            self.initialize_calls.append((args, kwargs))
            return len(self.initialize_calls) == 2

        def login(self, *args, **kwargs):
            self.login_calls.append((args, kwargs))
            return True

        def account_info(self):
            return SimpleNamespace(login=318413815, server="XMGlobal-MT5 7", trade_mode=0)

        def shutdown(self):
            self.shutdown_count += 1

        def last_error(self):
            return (-6, "Terminal: Authorization failed")

    mt5 = SavedSessionMt5()
    client = _login_client(mt5, terminal_path="C:/MT5/terminal64.exe", portable=True)

    result = client.login()

    assert result["server"] == "XMGlobal-MT5 7"
    assert mt5.shutdown_count == 1
    assert len(mt5.initialize_calls) == 2
    assert "login" in mt5.initialize_calls[0][1]
    assert "login" not in mt5.initialize_calls[1][1]
    assert mt5.initialize_calls[1][1]["portable"] is True
    assert mt5.login_calls == [
        ((318413815,), {"server": "XMGlobal-MT5 7", "timeout": 60_000})
    ]


def test_mt5_login_fallback_wrong_identity_fails_closed_and_shuts_down() -> None:
    class WrongIdentityMt5:
        def __init__(self):
            self.initialize_count = 0
            self.shutdown_count = 0

        def initialize(self, *args, **kwargs):
            self.initialize_count += 1
            return self.initialize_count == 2

        def login(self, *args, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=999, server="XMGlobal-MT5 7", trade_mode=0)

        def shutdown(self):
            self.shutdown_count += 1

        def last_error(self):
            return (-6, "authorization failed")

    mt5 = WrongIdentityMt5()
    client = _login_client(mt5)

    with pytest.raises(MODULE.XmMt5Error, match="unexpected account"):
        client.login()

    assert mt5.shutdown_count == 2
    assert client.connected is False


def test_mt5_login_total_failure_is_transient_and_redacts_password() -> None:
    class TotalFailureMt5:
        def __init__(self):
            self.shutdown_count = 0

        def initialize(self, *args, **kwargs):
            return False

        def shutdown(self):
            self.shutdown_count += 1

        def last_error(self):
            return (-6, "authorization failed for top-secret")

    mt5 = TotalFailureMt5()
    client = _login_client(mt5, password="top-secret")

    with pytest.raises(MODULE.core.TransientLiveError) as exc_info:
        client.login()

    assert isinstance(exc_info.value, MODULE.XmMt5Error)
    assert "top-secret" not in str(exc_info.value)
    assert "***" in str(exc_info.value)
    assert mt5.shutdown_count == 2
    assert client.connected is False


def test_weekend_never_creates_an_executable_prefix(tmp_path) -> None:
    now = pd.Timestamp("2026-08-09T14:00:00Z")

    assert MODULE.core.run_prefix(tmp_path, None, now) == {
        "state": "OUTSIDE_TRADE_WINDOW",
        "reason": "WEEKEND",
    }
    assert MODULE.core.finalize_session(tmp_path, None, now) == {
        "state": "NON_TRADING_DAY",
        "reason": "WEEKEND",
    }


def test_campaign_does_not_backfill_session_already_in_progress() -> None:
    lock = {"created_at": "2026-08-12T21:22:00Z"}

    assert not MODULE.core.campaign_session_eligible(pd.Timestamp("2026-08-12T21:30:00Z"), lock)
    assert MODULE.core.campaign_session_eligible(pd.Timestamp("2026-08-13T13:30:00Z"), lock)


def test_xm_scheduled_break_is_not_treated_as_missing_market_data() -> None:
    profile_start = pd.Timestamp("2026-08-05T18:00:00", tz="America/New_York")
    trade_end = pd.Timestamp("2026-08-06T11:00:00", tz="America/New_York")
    times = pd.date_range(profile_start, trade_end, freq="5min")
    times = times[~((times.strftime("%H:%M") >= "20:00") & (times.strftime("%H:%M") < "21:00"))]
    frame = pd.DataFrame(
        {
            "time": times,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
        }
    )

    assert not MODULE.core.baseline_assess_manual_state_day(
        frame, trade_end.date(), "5m", profile_start, trade_end
    ).valid
    assert MODULE.core.xm_assess_manual_state_day(
        frame, trade_end.date(), "5m", profile_start, trade_end
    ).valid

    real_gap = frame[frame["time"] != pd.Timestamp("2026-08-06T05:00:00", tz="America/New_York")]
    assert not MODULE.core.xm_assess_manual_state_day(
        real_gap, trade_end.date(), "5m", profile_start, trade_end
    ).valid


def test_daily_order_preflight_checks_both_symbols_without_sending(tmp_path) -> None:
    class PreflightMt5:
        def __init__(self) -> None:
            self.checked = 0

        def symbol_select(self, symbol, selected):
            return selected

        def symbol_info(self, symbol):
            return SimpleNamespace(point=0.1, digits=1, trade_tick_size=0.1, trade_stops_level=10)

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(ask=20000.2, bid=20000.0)

        def order_check(self, request):
            self.checked += 1
            return SimpleNamespace(retcode=0, comment="Done")

        def order_send(self, request):
            raise AssertionError("preflight must never send an order")

    class PreflightClient(MODULE.XmMt5DemoOrderClient):
        def _pending_request(self, symbol, direction, entry, stop, target, comment):
            return {"symbol": symbol, "type": direction, "volume": 0.1}

    client = object.__new__(PreflightClient)
    client.mt5 = PreflightMt5()
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))
    now = pd.Timestamp("2026-08-05T14:00:00Z")

    result = client.preflight_order_transport(tmp_path, now)

    assert result["state"] == "PASS"
    assert result["order_send_called"] is False
    assert len(result["checks"]) == 4
    assert client.mt5.checked == 4
    assert client.preflight_order_transport(tmp_path, now)["state"] == "ALREADY_PASSED"


def test_daily_order_preflight_waits_for_market_open_and_transient_quote(tmp_path) -> None:
    class PreflightMt5:
        def symbol_select(self, symbol, selected):
            return selected

        def symbol_info(self, symbol):
            return SimpleNamespace(point=0.1, digits=1, trade_tick_size=0.1, trade_stops_level=10)

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(ask=20000.2, bid=20000.0, time_msc=1786367700000)

        def order_check(self, request):
            return SimpleNamespace(retcode=10021, comment="No quotes")

    class PreflightClient(MODULE.XmMt5DemoOrderClient):
        def _pending_request(self, symbol, direction, entry, stop, target, comment):
            return {"symbol": symbol, "type": direction, "volume": 0.1}

    client = object.__new__(PreflightClient)
    client.mt5 = PreflightMt5()
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))

    before_open = pd.Timestamp("2026-08-10T13:20:00Z")
    assert client.preflight_order_transport(tmp_path, before_open) == {
        "state": "WAITING_PREFLIGHT_WINDOW",
        "opens_at": "09:30",
    }

    after_open = pd.Timestamp("2026-08-10T13:31:00Z")
    result = client.preflight_order_transport(tmp_path, after_open)
    assert result["state"] == "WAITING_ORDER_CHECK"
    assert result["symbol"] == "US100Cash"


def test_daily_order_preflight_retries_temporary_entry_distance(tmp_path) -> None:
    class PreflightMt5:
        def symbol_select(self, symbol, selected):
            return selected

        def symbol_info(self, symbol):
            return SimpleNamespace(point=0.1, digits=1, trade_tick_size=0.1, trade_stops_level=10)

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(ask=20000.2, bid=20000.0)

    class PreflightClient(MODULE.XmMt5DemoOrderClient):
        def _pending_request(self, symbol, direction, entry, stop, target, comment):
            raise MODULE.CandidateRetryableError(
                "ENTRY_DISTANCE_WAIT",
                "quote moved while preflight request was being built",
            )

    client = object.__new__(PreflightClient)
    client.mt5 = PreflightMt5()
    client.config = json.loads(MODULE.RUNTIME_CONFIG.read_text(encoding="utf-8"))

    result = client.preflight_order_transport(
        tmp_path, pd.Timestamp("2026-08-10T13:31:00Z")
    )

    assert result == {
        "state": "WAITING_ORDER_CHECK",
        "symbol": "US100Cash",
        "direction": "long",
        "reason_code": "ENTRY_DISTANCE_WAIT",
    }




class FakeMt5:
    TIMEFRAME_M1 = 1
    COPY_TICKS_ALL = 0

    def __init__(self) -> None:
        self.shutdown_called = False

    def initialize(self, **kwargs):
        return kwargs["login"] == 318413815 and kwargs["server"] == "XM-DEMO"

    def account_info(self):
        return SimpleNamespace(login=318413815, server="XM-DEMO", trade_mode=0)

    def copy_rates_range(self, symbol, timeframe, start, end):
        assert symbol == "US100Cash"
        assert timeframe == 1
        return [
            {
                "time": 1785072600,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "tick_volume": 11,
                "real_volume": 0,
            }
        ]

    def shutdown(self):
        self.shutdown_called = True

    def last_error(self):
        return (0, "ok")


def test_mt5_adapter_synthesizes_only_broker_verified_tickless_minutes(monkeypatch) -> None:
    class TicklessGapMt5(FakeMt5):
        def copy_rates_range(self, symbol, timeframe, start, end):
            base = pd.Timestamp("2026-08-14T07:55:00Z")
            return np.array(
                [
                    (
                        int((base + pd.Timedelta(minutes=offset)).timestamp()),
                        100.0 + offset,
                        100.5 + offset,
                        99.5 + offset,
                        100.25 + offset,
                        1,
                        0,
                    )
                    for offset in (0, 2, 3, 5)
                ],
                dtype=[
                    ("time", "i8"),
                    ("open", "f8"),
                    ("high", "f8"),
                    ("low", "f8"),
                    ("close", "f8"),
                    ("tick_volume", "i8"),
                    ("real_volume", "i8"),
                ],
            )

        def copy_ticks_range(self, symbol, start, end, flags):
            return []

    client = object.__new__(MODULE.XmMt5ReadOnlyClient)
    client.mt5 = TicklessGapMt5()
    client.connected = True
    client.closed_bar_delay_seconds = 0
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: pd.Timestamp("2026-08-14T08:01:00Z"))

    _, rows = client.prices(
        "US500Cash",
        pd.Timestamp("2026-08-14T07:55:00Z"),
        pd.Timestamp("2026-08-14T08:00:00Z"),
    )

    assert [row["snapshotTimeUTC"] for row in rows] == [
        f"2026-08-14T{minute}:00+00:00" for minute in ("07:55", "07:56", "07:57", "07:58", "07:59", "08:00")
    ]
    synthetic = [row for row in rows if row.get("verifiedNoTick")]
    assert [row["snapshotTimeUTC"] for row in synthetic] == [
        "2026-08-14T07:56:00+00:00",
        "2026-08-14T07:59:00+00:00",
    ]
    assert all(row["lastTradedVolume"] == 0 for row in synthetic)


def test_mt5_adapter_does_not_fill_a_missing_minute_when_ticks_exist(monkeypatch) -> None:
    class UnexplainedGapMt5(FakeMt5):
        def copy_rates_range(self, symbol, timeframe, start, end):
            base = pd.Timestamp("2026-08-14T07:55:00Z")
            return [
                {
                    "time": int((base + pd.Timedelta(minutes=offset)).timestamp()),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "tick_volume": 1,
                    "real_volume": 0,
                }
                for offset in (0, 2)
            ]

        def copy_ticks_range(self, symbol, start, end, flags):
            return [object()]

    client = object.__new__(MODULE.XmMt5ReadOnlyClient)
    client.mt5 = UnexplainedGapMt5()
    client.connected = True
    client.closed_bar_delay_seconds = 0
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: pd.Timestamp("2026-08-14T08:00:00Z"))

    _, rows = client.prices(
        "US500Cash",
        pd.Timestamp("2026-08-14T07:55:00Z"),
        pd.Timestamp("2026-08-14T07:57:00Z"),
    )

    assert [row["snapshotTimeUTC"] for row in rows] == [
        "2026-08-14T07:55:00+00:00",
        "2026-08-14T07:57:00+00:00",
    ]


def test_mt5_tick_verification_excludes_the_next_minute_boundary(monkeypatch) -> None:
    boundary = pd.Timestamp("2026-08-14T07:57:00Z")

    class BoundaryTickMt5(FakeMt5):
        def copy_rates_range(self, symbol, timeframe, start, end):
            return [
                {
                    "time": int(pd.Timestamp(f"2026-08-14T07:{minute}:00Z").timestamp()),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "tick_volume": 1,
                    "real_volume": 0,
                }
                for minute in ("55", "57")
            ]

        def copy_ticks_range(self, symbol, start, end, flags):
            return [object()] if pd.Timestamp(end) >= boundary else []

    client = object.__new__(MODULE.XmMt5ReadOnlyClient)
    client.mt5 = BoundaryTickMt5()
    client.connected = True
    client.closed_bar_delay_seconds = 0
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: pd.Timestamp("2026-08-14T08:00:00Z"))

    _, rows = client.prices(
        "US500Cash",
        pd.Timestamp("2026-08-14T07:55:00Z"),
        boundary,
    )

    assert [row["snapshotTimeUTC"] for row in rows if row.get("verifiedNoTick")] == [
        "2026-08-14T07:56:00+00:00"
    ]


def test_mt5_adapter_verifies_and_fills_trailing_closed_tickless_minutes(monkeypatch) -> None:
    class TrailingGapMt5(FakeMt5):
        def copy_rates_range(self, symbol, timeframe, start, end):
            return [
                {
                    "time": int(pd.Timestamp("2026-08-14T07:55:00Z").timestamp()),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "tick_volume": 1,
                    "real_volume": 0,
                }
            ]

        def copy_ticks_range(self, symbol, start, end, flags):
            return []

    client = object.__new__(MODULE.XmMt5ReadOnlyClient)
    client.mt5 = TrailingGapMt5()
    client.connected = True
    client.closed_bar_delay_seconds = 0
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: pd.Timestamp("2026-08-14T07:58:30Z"))

    _, rows = client.prices(
        "US500Cash",
        pd.Timestamp("2026-08-14T07:55:00Z"),
        pd.Timestamp("2026-08-14T07:58:30Z"),
    )

    assert [row["snapshotTimeUTC"] for row in rows] == [
        "2026-08-14T07:55:00+00:00",
        "2026-08-14T07:56:00+00:00",
        "2026-08-14T07:57:00+00:00",
    ]
    assert sum(bool(row.get("verifiedNoTick")) for row in rows) == 2


def test_mt5_adapter_fills_verified_tickless_minute_immediately_after_scheduled_close(
    monkeypatch,
) -> None:
    class ReopenGapMt5(FakeMt5):
        def copy_rates_range(self, symbol, timeframe, start, end):
            return [
                {
                    "time": int(timestamp.timestamp()),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "tick_volume": 1,
                    "real_volume": 0,
                }
                for timestamp, price in (
                    (pd.Timestamp("2026-08-13T23:59:00Z"), 100.0),
                    (pd.Timestamp("2026-08-14T01:01:00Z"), 101.0),
                )
            ]

        def copy_ticks_range(self, symbol, start, end, flags):
            return []

    client = object.__new__(MODULE.XmMt5ReadOnlyClient)
    client.mt5 = ReopenGapMt5()
    client.connected = True
    client.closed_bar_delay_seconds = 0
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: pd.Timestamp("2026-08-14T01:03:00Z"))

    _, rows = client.prices(
        "US500Cash",
        pd.Timestamp("2026-08-13T23:59:00Z"),
        pd.Timestamp("2026-08-14T01:01:00Z"),
    )

    synthetic = [row for row in rows if row.get("verifiedNoTick")]
    assert synthetic == [
        {
            "snapshotTimeUTC": "2026-08-14T01:00:00+00:00",
            "openPrice": {"bid": 101.0},
            "highPrice": {"bid": 101.0},
            "lowPrice": {"bid": 101.0},
            "closePrice": {"bid": 101.0},
            "lastTradedVolume": 0.0,
            "verifiedNoTick": True,
        }
    ]


def test_mt5_adapter_fills_weekly_reopen_gap_when_request_starts_during_weekend(
    monkeypatch,
) -> None:
    class WeekendReopenMt5(FakeMt5):
        def copy_rates_range(self, symbol, timeframe, start, end):
            return [
                {
                    "time": int(pd.Timestamp("2026-08-17T01:01:00Z").timestamp()),
                    "open": 101.0,
                    "high": 101.0,
                    "low": 101.0,
                    "close": 101.0,
                    "tick_volume": 1,
                    "real_volume": 0,
                }
            ]

        def copy_ticks_range(self, symbol, start, end, flags):
            return []

    client = object.__new__(MODULE.XmMt5ReadOnlyClient)
    client.mt5 = WeekendReopenMt5()
    client.connected = True
    client.closed_bar_delay_seconds = 0
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: pd.Timestamp("2026-08-17T01:03:00Z"))

    _, rows = client.prices(
        "US500Cash",
        pd.Timestamp("2026-08-16T23:49:00Z"),
        pd.Timestamp("2026-08-17T01:01:00Z"),
    )

    assert [row["snapshotTimeUTC"] for row in rows] == [
        "2026-08-17T01:00:00+00:00",
        "2026-08-17T01:01:00+00:00",
    ]
    assert rows[0]["verifiedNoTick"] is True
    assert rows[0]["openPrice"]["bid"] == 101.0


class FakeTradeMt5(FakeMt5):
    ACCOUNT_TRADE_MODE_DEMO = 0

    def __init__(self, trade_mode=0, terminal_trade_allowed=True) -> None:
        super().__init__()
        self.trade_mode = trade_mode
        self.terminal_trade_allowed = terminal_trade_allowed

    def initialize(self, **kwargs):
        return kwargs["login"] == 318413815 and kwargs["server"] == "XMGlobal-MT5 7"

    def account_info(self):
        return SimpleNamespace(
            login=318413815,
            server="XMGlobal-MT5 7",
            company="XM Global Limited",
            trade_mode=self.trade_mode,
            trade_allowed=True,
            trade_expert=True,
        )

    def terminal_info(self):
        return SimpleNamespace(
            connected=True,
            trade_allowed=self.terminal_trade_allowed,
            tradeapi_disabled=False,
        )


class FakeOrderMt5(FakeTradeMt5):
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_TIME_SPECIFIED = 2
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009

    def __init__(self) -> None:
        super().__init__()
        self.pending = []
        self.pending_send_count = 0

    def symbol_select(self, symbol, visible):
        return symbol == "US100Cash" and visible

    def symbol_info(self, symbol):
        return SimpleNamespace(
            digits=2,
            volume_min=0.1,
            volume_step=0.1,
            volume_max=10.0,
        )

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=100.0, ask=101.0)

    def order_check(self, request):
        return SimpleNamespace(retcode=0, comment="Done")

    def order_send(self, request):
        if request["action"] == self.TRADE_ACTION_PENDING:
            self.pending_send_count += 1
            order = SimpleNamespace(
                ticket=12345,
                magic=request["magic"],
                comment=request["comment"],
            )
            self.pending = [order]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=12345, deal=0)
        if request["action"] == self.TRADE_ACTION_REMOVE:
            self.pending = []
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=request["order"], deal=0)
        raise AssertionError("Unexpected fake order action.")

    def orders_get(self, ticket=None):
        if ticket is None:
            return tuple(self.pending)
        return tuple(item for item in self.pending if item.ticket == ticket)

    def positions_get(self):
        return ()

    def history_orders_get(self, start, end):
        return ()

    def history_deals_get(self, start, end):
        return ()


class NoConflictStore:
    def conflict_count(self, epic, start, end):
        return 0


def test_mt5_adapter_emits_canonical_minute_bar_without_order_api() -> None:
    client = object.__new__(MODULE.XmMt5ReadOnlyClient)
    client.mt5 = FakeMt5()
    client.login_id = 318413815
    client.server = "XM-DEMO"
    client.password = "secret"
    client.terminal_path = ""
    client.connected = False
    client.login()
    known, rows = client.prices(
        "US100Cash",
        pd.Timestamp("2026-07-26T09:00:00Z"),
        pd.Timestamp("2026-07-26T10:00:00Z"),
    )
    assert known.tz is not None
    assert rows[0]["openPrice"]["bid"] == 100.0
    assert rows[0]["lastTradedVolume"] == 11.0
    assert not hasattr(client, "order_send")
    client.close()
    assert client.mt5.shutdown_called


def test_mt5_adapter_excludes_the_still_open_minute(monkeypatch) -> None:
    known_time = pd.Timestamp("2026-07-29T13:31:08Z")

    class PartialMinuteMt5(FakeMt5):
        def copy_rates_range(self, symbol, timeframe, start, end):
            return [
                {
                    "time": int(pd.Timestamp("2026-07-29T13:30:00Z").timestamp()),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "tick_volume": 10,
                    "real_volume": 0,
                },
                {
                    "time": int(pd.Timestamp("2026-07-29T13:31:00Z").timestamp()),
                    "open": 100.5,
                    "high": 102.0,
                    "low": 100.0,
                    "close": 101.5,
                    "tick_volume": 3,
                    "real_volume": 0,
                },
            ]

    client = object.__new__(MODULE.XmMt5ReadOnlyClient)
    client.mt5 = PartialMinuteMt5()
    client.login_id = 318413815
    client.server = "XM-DEMO"
    client.password = "secret"
    client.terminal_path = ""
    client.closed_bar_delay_seconds = 8
    client.connected = False
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: known_time)

    observed_at, rows = client.prices(
        "US100Cash",
        pd.Timestamp("2026-07-29T13:30:00Z"),
        known_time,
    )

    assert observed_at == known_time
    assert [row["snapshotTimeUTC"] for row in rows] == ["2026-07-29T13:30:00+00:00"]


def session_frame(trade_date: str, minutes: int) -> pd.DataFrame:
    runtime = MODULE.core.runtime_config()
    start = pd.Timestamp(trade_date, tz=MODULE.core.TZ) - pd.Timedelta(hours=6)
    cutoff = pd.Timestamp(f"{trade_date} 11:00", tz=MODULE.core.TZ)
    times = [
        value
        for value in pd.date_range(start, cutoff, freq=f"{minutes}min", inclusive="left")
        if not MODULE.core.scheduled_closed_bucket(value, minutes, runtime)
    ]
    return pd.DataFrame(
        {
            "time": times,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
            "known_time": [value.tz_convert("UTC") for value in times],
        }
    )


def test_xm_planned_daily_break_is_not_counted_as_missing() -> None:
    frame = session_frame("2026-07-28", 3)
    gate = MODULE.core.frame_gate(
        NoConflictStore(),
        "US100Cash",
        frame,
        pd.Timestamp("2026-07-28").date(),
        pd.Timestamp("2026-07-28 11:00", tz=MODULE.core.TZ),
        3,
    )
    assert gate["state"] == "VALID"
    assert gate["scheduled_closed_bar_count"] == 20
    assert gate["issues"] == []


def test_xm_open_session_gap_remains_data_invalid() -> None:
    frame = session_frame("2026-07-28", 5)
    frame = frame[frame["time"] != pd.Timestamp("2026-07-27 21:00", tz=MODULE.core.TZ)]
    gate = MODULE.core.frame_gate(
        NoConflictStore(),
        "US500Cash",
        frame,
        pd.Timestamp("2026-07-28").date(),
        pd.Timestamp("2026-07-28 11:00", tz=MODULE.core.TZ),
        5,
    )
    assert gate["state"] == "DATA_INVALID"
    assert gate["issues"] == [
        {
            "code": "MISSING_BARS",
            "count": 1,
            "first": "2026-07-27T21:00:00-04:00",
            "last": "2026-07-27T21:00:00-04:00",
        }
    ]


def test_xm_unverified_isolated_overnight_gap_is_blocked() -> None:
    frame = session_frame("2026-07-28", 5)
    missing = pd.Timestamp("2026-07-28 05:50", tz=MODULE.core.TZ)
    frame = frame[frame["time"] != missing]
    gate = MODULE.core.frame_gate(
        NoConflictStore(),
        "US500Cash",
        frame,
        pd.Timestamp("2026-07-28").date(),
        pd.Timestamp("2026-07-28 11:00", tz=MODULE.core.TZ),
        5,
    )
    assert gate["state"] == "DATA_INVALID"
    assert gate["issues"][0]["code"] == "MISSING_BARS"
    assert gate["tolerated_no_tick_bar_count"] == 0


def test_xm_unverified_partial_overnight_bucket_is_blocked() -> None:
    frame = session_frame("2026-07-28", 5)
    frame["source_minute_count"] = 5
    partial = pd.Timestamp("2026-07-28 04:10", tz=MODULE.core.TZ)
    frame.loc[frame["time"] == partial, "source_minute_count"] = 4

    gate = MODULE.core.frame_gate(
        NoConflictStore(),
        "US500Cash",
        frame,
        pd.Timestamp("2026-07-28").date(),
        pd.Timestamp("2026-07-28 11:00", tz=MODULE.core.TZ),
        5,
    )

    assert gate["state"] == "DATA_INVALID"
    assert gate["issues"][0]["code"] == "INCOMPLETE_SOURCE_MINUTES"
    assert gate["tolerated_incomplete_source_bar_count"] == 0


def test_xm_partial_bucket_during_critical_window_remains_invalid() -> None:
    frame = session_frame("2026-07-28", 5)
    frame["source_minute_count"] = 5
    partial = pd.Timestamp("2026-07-28 09:15", tz=MODULE.core.TZ)
    frame.loc[frame["time"] == partial, "source_minute_count"] = 4

    gate = MODULE.core.frame_gate(
        NoConflictStore(),
        "US500Cash",
        frame,
        pd.Timestamp("2026-07-28").date(),
        pd.Timestamp("2026-07-28 11:00", tz=MODULE.core.TZ),
        5,
    )

    assert gate["state"] == "DATA_INVALID"
    assert gate["issues"][0]["code"] == "INCOMPLETE_SOURCE_MINUTES"


def test_xm_unverified_evening_gap_before_midnight_is_blocked() -> None:
    frame = session_frame("2026-07-28", 5)
    missing = pd.Timestamp("2026-07-27 21:25", tz=MODULE.core.TZ)
    frame = frame[frame["time"] != missing]
    gate = MODULE.core.frame_gate(
        NoConflictStore(),
        "US500Cash",
        frame,
        pd.Timestamp("2026-07-28").date(),
        pd.Timestamp("2026-07-28 11:00", tz=MODULE.core.TZ),
        5,
    )
    assert gate["state"] == "DATA_INVALID"
    assert gate["issues"][0]["code"] == "MISSING_BARS"
    assert gate["tolerated_no_tick_ranges"] == []


def test_xm_weekend_reopen_window_uses_verified_sunday_session() -> None:
    frame = session_frame("2026-07-27", 5)
    gate = MODULE.core.frame_gate(
        NoConflictStore(),
        "US500Cash",
        frame,
        pd.Timestamp("2026-07-27").date(),
        pd.Timestamp("2026-07-27 11:00", tz=MODULE.core.TZ),
        5,
    )
    assert gate["state"] == "VALID"
    assert gate["scheduled_closed_bar_count"] == 36


def test_broker_bar_during_configured_close_stops_for_schedule_drift() -> None:
    frame = session_frame("2026-07-28", 5)
    extra = frame.iloc[[0]].copy()
    extra["time"] = pd.Timestamp("2026-07-27 20:00", tz=MODULE.core.TZ)
    frame = pd.concat([frame, extra], ignore_index=True).sort_values("time")
    gate = MODULE.core.frame_gate(
        NoConflictStore(),
        "US500Cash",
        frame,
        pd.Timestamp("2026-07-28").date(),
        pd.Timestamp("2026-07-28 11:00", tz=MODULE.core.TZ),
        5,
    )
    assert gate["state"] == "DATA_INVALID"
    assert gate["issues"][0]["code"] == "SESSION_SCHEDULE_DRIFT"


def test_prefix_snapshot_comparison_handles_stable_nan_values() -> None:
    assert MODULE.core.snapshots_equal(
        {"vah": float("nan"), "nested": [{"value": float("nan")}]},
        {"vah": float("nan"), "nested": [{"value": float("nan")}]},
    )
    assert not MODULE.core.snapshots_equal(
        {"vah": float("nan"), "value": 1.0},
        {"vah": float("nan"), "value": 2.0},
    )


def test_daily_event_counts_are_scoped_to_new_york_trade_date() -> None:
    events = [
        {"event": "SUBMITTED", "recorded_at": "2026-07-29T03:30:00Z"},
        {"event": "SUBMITTED", "recorded_at": "2026-07-29T14:00:00Z"},
        {"event": "CANCELLED", "recorded_at": "2026-07-30T02:00:00Z"},
    ]
    selected = MODULE.XmMt5DemoOrderClient._events_for_trade_date(events, "2026-07-29")
    assert [event["event"] for event in selected] == ["SUBMITTED", "CANCELLED"]


def test_live_pair_cap_stays_latched_after_intraday_breach() -> None:
    class PairCapMt5(FakeTradeMt5):
        DEAL_ENTRY_OUT = 1
        DEAL_REASON_SL = 4
        DEAL_REASON_TP = 5

        def history_deals_get(self, start, end):
            return (
                SimpleNamespace(
                        magic=self.test_magic,
                    entry=self.DEAL_ENTRY_OUT,
                    reason=self.DEAL_REASON_SL,
                    symbol="US100Cash",
                    time_msc=1,
                    ticket=1,
                ),
                SimpleNamespace(
                        magic=self.test_magic,
                    entry=self.DEAL_ENTRY_OUT,
                    reason=self.DEAL_REASON_TP,
                    symbol="US500Cash",
                    time_msc=2,
                    ticket=2,
                ),
            )

        def positions_get(self):
            return ()

    client = demo_client(PairCapMt5())
    client.mt5.test_magic = client.magic
    configs, _, _ = MODULE.core.live_strategy_objects()
    runtime = MODULE.core.runtime_config()

    result = client._pair_cap_state(pd.Timestamp("2026-07-29T16:00:00Z"), configs, runtime)

    assert result["realized_r"] > -1.0
    assert result["cap_breached"] is True
    assert result["state"] == "SUPPRESSED_DAILY_CAP"


def demo_client(fake_mt5: FakeTradeMt5) -> object:
    client = object.__new__(MODULE.XmMt5DemoOrderClient)
    client.mt5 = fake_mt5
    client.login_id = 318413815
    client.server = "XMGlobal-MT5 7"
    client.password = ""
    client.terminal_path = ""
    client.connected = False
    client.config = MODULE.core.runtime_config()
    client.magic = int(client.config["magic_number"])
    client.demo_verified = False
    client.account = None
    client.terminal = None
    return client


def test_smoke_exposure_counts_foreign_magic_orders_and_positions() -> None:
    mt5 = FakeOrderMt5()
    mt5.pending = [SimpleNamespace(ticket=1, magic=111), SimpleNamespace(ticket=2, magic=222)]
    mt5.positions_get = lambda: (SimpleNamespace(ticket=3, magic=333), SimpleNamespace(ticket=4, magic=444))
    exposure = demo_client(mt5)._smoke_exposure()
    assert exposure == {"open_orders": 2, "open_positions": 2, "unknown_exposure": 0}


def test_smoke_rejects_foreign_exposure_before_order_check() -> None:
    mt5 = FakeOrderMt5()
    mt5.pending = [SimpleNamespace(ticket=1, magic=999)]
    mt5.order_check = lambda request: (_ for _ in ()).throw(AssertionError("order_check must not run"))
    with pytest.raises(MODULE.core.CriticalLiveError, match="flat dedicated demo account"):
        demo_client(mt5).smoke_order(Path("."), {"runtime_config_hash": "test"})


def test_smoke_rejects_foreign_position_exposure_before_order_check() -> None:
    mt5 = FakeOrderMt5()
    mt5.positions_get = lambda: (SimpleNamespace(ticket=1, magic=999),)
    mt5.order_check = lambda request: (_ for _ in ()).throw(AssertionError("order_check must not run"))
    with pytest.raises(MODULE.core.CriticalLiveError, match="flat dedicated demo account"):
        demo_client(mt5).smoke_order(Path("."), {"runtime_config_hash": "test"})


def test_smoke_does_not_pass_when_foreign_exposure_remains_after_cancellation(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    foreign = SimpleNamespace(ticket=999, magic=999, comment="foreign")
    original_remove = mt5.order_send
    def remove_with_foreign(request):
        result = original_remove(request)
        if request["action"] == mt5.TRADE_ACTION_REMOVE:
            mt5.pending.append(foreign)
        return result
    mt5.order_send = remove_with_foreign
    with pytest.raises(MODULE.core.CriticalLiveError, match="return the dedicated demo account to flat"):
        demo_client(mt5).smoke_order(tmp_path, {"runtime_config_hash": "test"})


def test_smoke_exposure_keeps_unknown_state_fail_closed() -> None:
    mt5 = FakeOrderMt5()
    mt5.orders_get = lambda ticket=None: None
    with pytest.raises(MODULE.BrokerStateUnknownError):
        demo_client(mt5)._smoke_exposure()


def test_smoke_exposure_counts_empty_dedicated_account_as_known_flat() -> None:
    exposure = demo_client(FakeOrderMt5())._smoke_exposure()
    assert exposure["open_orders"] == 0
    assert exposure["open_positions"] == 0
    assert exposure["unknown_exposure"] == 0


def test_smoke_foreign_order_is_not_hidden_by_magic_filter() -> None:
    mt5 = FakeOrderMt5()
    mt5.pending = [SimpleNamespace(ticket=77, magic=0, comment="manual")]
    client = demo_client(mt5)
    assert client._smoke_exposure()["open_orders"] == len(mt5.pending)


def test_demo_identity_and_terminal_permission_are_both_required() -> None:
    client = demo_client(FakeTradeMt5(terminal_trade_allowed=False))
    status = client.order_permission_status()
    assert status["state"] == "ORDER_PERMISSION_DISABLED"
    assert status["checks"]["demo_verified"] is True
    assert status["checks"]["terminal_trade_allowed"] is False


def test_non_demo_account_is_rejected_before_order_permission() -> None:
    client = demo_client(FakeTradeMt5(trade_mode=1))
    try:
        client.order_permission_status()
    except MODULE.core.CriticalLiveError as exc:
        assert "demo identity gate" in str(exc)
    else:
        raise AssertionError("Non-demo account was not rejected.")


def test_demo_identity_drift_is_rejected_before_order_permission() -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    assert client.order_permission_status()["state"] == "READY"
    mt5.account_info = lambda: SimpleNamespace(
        login=999999,
        server="REAL-SERVER",
        company="Other Broker",
        trade_mode=1,
        trade_allowed=True,
        trade_expert=True,
    )

    with pytest.raises(MODULE.core.CriticalLiveError, match="demo identity gate"):
        client._require_order_permission()

    assert client.connected is False
    assert client.demo_verified is False
    assert mt5.shutdown_called is True
    assert mt5.pending_send_count == 0


def test_smoke_order_uses_minimum_demo_volume_and_is_immediately_cancelled(tmp_path) -> None:
    client = demo_client(FakeOrderMt5())
    result = client.smoke_order(
        tmp_path,
        {"runtime_config_hash": "runtime-hash"},
    )
    assert result["state"] == "PASS"
    assert result["minimum_volume"] == 0.1
    assert result["cancelled"]["state"] == "CANCELLED"
    assert client.mt5.pending == []
    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "orders" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["SMOKE_SUBMITTED", "CANCELLED"]


def test_pending_request_aligns_all_prices_to_broker_tick_size() -> None:
    class TickSizedMt5(FakeOrderMt5):
        def symbol_info(self, symbol):
            return SimpleNamespace(
                digits=2,
                point=0.01,
                trade_tick_size=0.25,
                trade_stops_level=0,
                trade_freeze_level=0,
                volume_min=0.1,
                volume_step=0.1,
                volume_max=10.0,
            )

    request = demo_client(TickSizedMt5())._pending_request(
        "US100Cash", "long", 97.62, 90.13, 120.12, "FSP:test"
    )
    assert request["price"] == 97.5
    assert request["sl"] == 90.25
    assert request["tp"] == 120.0


def test_pending_request_uses_broker_quote_without_comparing_broker_clock_to_host() -> None:
    class BrokerClockMt5(FakeOrderMt5):
        def symbol_info_tick(self, symbol):
            return SimpleNamespace(
                bid=100.0,
                ask=101.0,
                time_msc=int(pd.Timestamp("2026-07-29T16:00:00Z").timestamp() * 1000),
            )

    request = demo_client(BrokerClockMt5())._pending_request(
        "US100Cash", "long", 97.5, 90.0, 120.0, "FSP:test"
    )
    assert request["price"] == 97.5


def test_pending_request_treats_live_entry_distance_as_retryable() -> None:
    class DistanceRuleMt5(FakeOrderMt5):
        def symbol_info(self, symbol):
            return SimpleNamespace(
                digits=2,
                point=0.01,
                trade_tick_size=0.01,
                trade_stops_level=100,
                trade_freeze_level=0,
                volume_min=0.1,
                volume_step=0.1,
                volume_max=10.0,
            )

    try:
        demo_client(DistanceRuleMt5())._pending_request(
            "US100Cash", "long", 100.5, 90.0, 120.0, "FSP:test"
        )
    except MODULE.CandidateRetryableError as exc:
        assert exc.code == "ENTRY_DISTANCE_WAIT"
        assert "temporarily inside the broker distance" in str(exc)
    else:
        raise AssertionError("Broker stop-distance violation was accepted.")


def test_illegal_pending_price_geometry_remains_critical() -> None:
    try:
        demo_client(FakeOrderMt5())._pending_request(
            "US100Cash", "long", 100.0, 105.0, 120.0, "FSP:test"
        )
    except MODULE.core.CriticalLiveError as exc:
        assert "price geometry" in str(exc)
    else:
        raise AssertionError("Illegal pending-order geometry was not critical.")


def order_candidate() -> dict[str, object]:
    return {
        "order_id": "order-1",
        "thesis_id": "thesis-1",
        "leg_key": "nq",
        "direction": "long",
        "stop_price": 90.0,
        "target_price": 120.0,
        "fvg_time": "2026-07-29T13:00:00Z",
        "fvg_known_time": "2026-07-29T13:03:00Z",
    }


def claim_intent_in_process(database_path: str, start_event: object, results: object) -> None:
    start_event.wait()
    connection = sqlite3.connect(database_path, timeout=10.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("BEGIN IMMEDIATE")
        inserted = connection.execute(
            "INSERT OR IGNORE INTO order_intents "
            "(order_id, status, comment, request_json, broker_ticket, created_at, updated_at) "
            "VALUES ('process-order', 'INTENT', 'comment', '{}', NULL, 'now', 'now')"
        ).rowcount
        connection.commit()
        results.put(int(inserted))
    finally:
        connection.close()


def test_duplicate_candidate_is_submitted_only_once(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    first = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)
    second = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)
    assert first["state"] == "SUBMITTED"
    assert second["state"] == "IDEMPOTENT_ALREADY_SUBMITTED"
    assert mt5.pending_send_count == 1


def test_stale_candidate_is_nonfatal_no_send_and_idempotent(tmp_path) -> None:
    class CrossedQuoteMt5(FakeOrderMt5):
        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=98.0, ask=99.0)

    mt5 = CrossedQuoteMt5()
    client = demo_client(mt5)

    first = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)
    second = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)

    assert first == {
        "state": "CANDIDATE_NOT_EXECUTABLE_NO_SEND",
        "order_id": "order-1",
        "reason_code": "STALE_ENTRY_CROSSED",
    }
    assert second["state"] == "IDEMPOTENT_NOT_EXECUTABLE_NO_SEND"
    assert mt5.pending_send_count == 0
    assert [item["event"] for item in client._events(tmp_path)] == [
        "CANDIDATE_NOT_EXECUTABLE"
    ]


def test_entry_distance_wait_retries_then_submits_once(tmp_path) -> None:
    class MovingQuoteMt5(FakeOrderMt5):
        def __init__(self):
            super().__init__()
            self.quote_count = 0

        def symbol_info(self, symbol):
            return SimpleNamespace(
                digits=2,
                point=0.01,
                trade_tick_size=0.01,
                trade_stops_level=100,
                volume_min=0.1,
                volume_step=0.1,
                volume_max=10.0,
            )

        def symbol_info_tick(self, symbol):
            self.quote_count += 1
            ask = 100.5 if self.quote_count == 1 else 102.0
            return SimpleNamespace(bid=ask - 1.0, ask=ask)

    mt5 = MovingQuoteMt5()
    client = demo_client(mt5)

    first = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)
    second = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)

    assert first == {
        "state": "ENTRY_DISTANCE_WAIT_NO_SEND",
        "order_id": "order-1",
        "reason_code": "ENTRY_DISTANCE_WAIT",
    }
    assert second["state"] == "SUBMITTED"
    assert mt5.pending_send_count == 1
    assert [item["event"] for item in client._events(tmp_path)] == [
        "INTENT",
        "CHECK_PASSED",
        "SUBMITTED",
    ]


def test_stop_target_distance_is_terminal_and_idempotent(tmp_path) -> None:
    class PermanentDistanceMt5(FakeOrderMt5):
        def symbol_info(self, symbol):
            return SimpleNamespace(
                digits=2,
                point=0.01,
                trade_tick_size=0.01,
                trade_stops_level=100,
                volume_min=0.1,
                volume_step=0.1,
                volume_max=10.0,
            )

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=101.0, ask=102.0)

    decision = {
        **order_candidate(),
        "stop_price": 99.5,
        "target_price": 101.0,
    }
    mt5 = PermanentDistanceMt5()
    client = demo_client(mt5)

    first = client._place_candidate(tmp_path, decision, "US100Cash", 2.0)
    second = client._place_candidate(tmp_path, decision, "US100Cash", 2.0)

    assert first == {
        "state": "CANDIDATE_NOT_EXECUTABLE_NO_SEND",
        "order_id": "order-1",
        "reason_code": "BROKER_SL_TP_DISTANCE",
    }
    assert second["state"] == "IDEMPOTENT_NOT_EXECUTABLE_NO_SEND"
    assert mt5.pending_send_count == 0
    assert [item["event"] for item in client._events(tmp_path)] == [
        "CANDIDATE_NOT_EXECUTABLE"
    ]


def test_transient_order_check_retries_without_duplicate_send(tmp_path) -> None:
    class RetryCheckMt5(FakeOrderMt5):
        def __init__(self):
            super().__init__()
            self.check_count = 0

        def order_check(self, request):
            self.check_count += 1
            if self.check_count == 1:
                return SimpleNamespace(retcode=10021, comment="No quotes")
            return SimpleNamespace(retcode=0, comment="Done")

    mt5 = RetryCheckMt5()
    client = demo_client(mt5)

    first = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)
    second = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)

    assert first == {
        "state": "CHECK_RETRYABLE_NO_SEND",
        "order_id": "order-1",
        "retcode": 10021,
    }
    assert second["state"] == "SUBMITTED"
    assert mt5.check_count == 2
    assert mt5.pending_send_count == 1
    assert [item["event"] for item in client._events(tmp_path)] == [
        "INTENT",
        "CHECK_RETRYABLE",
        "CHECK_PASSED",
        "SUBMITTED",
    ]


def test_uncertain_prior_intent_refuses_duplicate_send(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    client._append_order_event(
        tmp_path,
        {"event": "INTENT", "order_id": "order-1", "comment": "FSP-nq-order-1"},
    )
    try:
        client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)
    except MODULE.core.CriticalLiveError as exc:
        assert "uncertain prior order intent" in str(exc)
    else:
        raise AssertionError("Uncertain prior intent did not stop duplicate submission.")
    assert mt5.pending_send_count == 0


def test_restart_links_existing_broker_order_after_recorded_intent(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    comment = client._comment("nq", "order-1")
    client._append_order_event(
        tmp_path,
        {"event": "INTENT", "order_id": "order-1", "comment": comment},
    )
    mt5.pending = [SimpleNamespace(ticket=12345, magic=client.magic, comment=comment)]

    result = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)

    assert result["state"] == "IDEMPOTENT_LINKED_EXISTING"
    assert mt5.pending_send_count == 0
    events = client._events(tmp_path)
    assert [event["event"] for event in events] == ["INTENT", "LINKED_EXISTING"]


def test_data_invalid_cancels_own_pending_order_without_resubmitting(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)
    result = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert result["state"] == "DATA_INVALID_NO_SEND"
    assert mt5.pending == []
    assert mt5.pending_send_count == 1


def test_cancel_readback_remains_available_when_open_permission_is_disabled(tmp_path) -> None:
    class CancelOnlyMt5(FakeOrderMt5):
        def account_info(self):
            account = super().account_info()
            account.trade_allowed = False
            account.trade_expert = False
            return account

        def terminal_info(self):
            return SimpleNamespace(
                connected=True,
                trade_allowed=False,
                tradeapi_disabled=True,
            )

    mt5 = CancelOnlyMt5()
    client = demo_client(mt5)
    mt5.pending = [
        SimpleNamespace(ticket=12345, magic=client.magic, comment="FSP:test")
    ]

    cancelled = client.cancel_all_pending(tmp_path, "TECHNICAL_RECOVERY")

    assert cancelled == [
        {"ticket": 12345, "state": "CANCELLED", "reason": "TECHNICAL_RECOVERY"}
    ]
    assert mt5.pending == []


def test_already_recorded_data_invalid_remains_no_send(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    client.connected = True
    client.demo_verified = True
    client.account = mt5.account_info()
    client.terminal = mt5.terminal_info()
    mt5.pending = [
        SimpleNamespace(ticket=12345, magic=client.magic, comment="FSP-invalid")
    ]
    prefix_path = tmp_path / "invalid-prefix.json"
    prefix_path.write_text('{"state":"DATA_INVALID"}', encoding="utf-8")

    result = client.reconcile_orders(
        tmp_path,
        {"state": "ALREADY_RECORDED", "path": str(prefix_path)},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["state"] == "DATA_INVALID_NO_SEND"
    assert mt5.pending == []
    assert len(result["cancelled"]) == 1
    assert mt5.pending_send_count == 0


def test_outside_trade_window_cancels_own_pending_order(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    client._place_candidate(tmp_path, order_candidate(), "US100Cash", 3.0)

    result = client.reconcile_orders(
        tmp_path,
        {"state": "OUTSIDE_TRADE_WINDOW"},
        pd.Timestamp("2026-07-29T13:00:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["state"] == "NO_EXECUTABLE_PREFIX"
    assert len(result["cancelled"]) == 1
    assert mt5.pending == []


def test_final_trade_window_prefix_cancels_instead_of_submitting(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    client._place_candidate(tmp_path, order_candidate(), "US100Cash", 3.0)
    decision = {
        **order_candidate(),
        "setup_state": "VALID",
        "order_state": "CANCELLED",
        "terminal_reason": "CANCELLED_TRADE_WINDOW_END",
        "pair_cap_state": "ALLOWED",
    }
    prefix_path = tmp_path / "final-prefix.json"
    prefix_path.write_text(
        __import__("json").dumps(
            {
                "date": "2026-07-29",
                "state": "VALID",
                "deterministic_rerun": True,
                "invariant_errors": [],
                "cutoffs": {
                    "nq": "2026-07-29T10:30:00-04:00",
                    "spx": "2026-07-29T10:30:00-04:00",
                },
                "hashes": {
                    "config": "config-hash",
                    "code": MODULE.core.source_code_hash(),
                },
                "payload": {"decisions": [decision]},
            }
        ),
        encoding="utf-8",
    )

    result = client.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        pd.Timestamp("2026-07-29T14:31:00Z"),
        {"live_config_hash": "config-hash"},
    )

    assert result["state"] == "RECONCILED"
    assert result["candidate_count"] == 0
    assert len(result["cancelled_stale"]) == 1
    assert mt5.pending == []
    assert mt5.pending_send_count == 1


def test_executable_intrawindow_prefix_reaches_broker_submission(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    decision = {
        **order_candidate(),
        "setup_state": "VALID",
        "order_state": "CANCELLED",
        "terminal_reason": "CANCELLED_TRADE_WINDOW_END",
        "pair_cap_state": "ALLOWED",
        "intrabar_ambiguity": "",
    }
    prefix_path = tmp_path / "executable-prefix.json"
    prefix_path.write_text(
        json.dumps(
            {
                "date": "2026-07-29",
                "state": "VALID",
                "deterministic_rerun": True,
                "invariant_errors": [],
                "cutoffs": {
                    "nq": "2026-07-29T10:27:00-04:00",
                    "spx": "2026-07-29T10:25:00-04:00",
                },
                "hashes": {
                    "config": "config-hash",
                    "code": MODULE.core.source_code_hash(),
                },
                "payload": {"decisions": [decision]},
            }
        ),
        encoding="utf-8",
    )

    result = client.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        pd.Timestamp("2026-07-29T14:28:00Z"),
        {"live_config_hash": "config-hash"},
    )

    assert result["state"] == "RECONCILED"
    assert result["candidate_count"] == 1
    assert result["results"][0]["state"] == "SUBMITTED"
    assert mt5.pending_send_count == 1


def test_pending_order_expires_at_the_leg_trade_window_end(monkeypatch) -> None:
    now = pd.Timestamp("2026-07-29T14:00:00Z")
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: now)
    request = demo_client(FakeOrderMt5())._pending_request(
        "US100Cash", "long", 97.5, 90.0, 120.0, "FSP:test"
    )
    configs, _, _ = MODULE.core.live_strategy_objects()
    expected = pd.Timestamp(
        f"2026-07-29 {configs['nq'].trade_window_end}", tz=MODULE.core.TZ
    ).tz_convert("UTC")

    assert request["type_time"] == FakeOrderMt5.ORDER_TIME_SPECIFIED
    assert isinstance(request["expiration"], int)
    assert pd.Timestamp(request["expiration"], unit="s", tz="UTC") == expected


def test_none_broker_collection_returns_unknown_no_send(tmp_path) -> None:
    class UnknownOrdersMt5(FakeOrderMt5):
        def orders_get(self, ticket=None):
            return None

        def last_error(self):
            return (1, "transport unavailable")

    mt5 = UnknownOrdersMt5()
    result = demo_client(mt5).reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["state"] == "UNKNOWN_NO_SEND"
    assert mt5.pending_send_count == 0


def test_sqlite_intent_allows_only_one_parallel_send(tmp_path, monkeypatch) -> None:
    mt5 = FakeOrderMt5()
    clients = [demo_client(mt5), demo_client(mt5)]
    barrier = threading.Barrier(2)
    original_state = MODULE.XmMt5DemoOrderClient._intent_state

    def synchronized_initial_state(self, output_root, order_id):
        state = original_state(self, output_root, order_id)
        barrier.wait(timeout=5)
        return state

    monkeypatch.setattr(
        MODULE.XmMt5DemoOrderClient,
        "_intent_state",
        synchronized_initial_state,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda client: client._place_candidate(
                    tmp_path, order_candidate(), "US100Cash", 2.0
                ),
                clients,
            )
        )

    assert mt5.pending_send_count == 1
    assert sum(result["state"] == "SUBMITTED" for result in results) == 1
    assert all(
        result["state"] == "SUBMITTED" or result["state"].startswith("IDEMPOTENT_")
        for result in results
    )


def test_cancel_acknowledgement_requires_empty_broker_readback(tmp_path) -> None:
    class StuckOrderMt5(FakeOrderMt5):
        def order_send(self, request):
            if request["action"] == self.TRADE_ACTION_REMOVE:
                return SimpleNamespace(
                    retcode=self.TRADE_RETCODE_DONE,
                    order=request["order"],
                    deal=0,
                )
            return super().order_send(request)

    mt5 = StuckOrderMt5()
    client = demo_client(mt5)
    client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)

    try:
        client.cancel_all_pending(tmp_path, "TEST")
    except MODULE.UnsafeOpenOrdersError as exc:
        assert "remained open" in str(exc)
    else:
        raise AssertionError("Unverified cancellation was treated as safe.")


def test_sqlite_unique_intent_is_process_safe(tmp_path) -> None:
    client = demo_client(FakeOrderMt5())
    connection = client._order_connection(tmp_path)
    connection.close()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=claim_intent_in_process,
            args=(str(client._order_db(tmp_path)), start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted(results.get(timeout=2) for _ in processes) == [0, 1]
