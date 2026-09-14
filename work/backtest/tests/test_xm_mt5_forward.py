from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from v08_helpers import checkpoint_if_enabled, record_if_enabled
from backtest.live.approval import ApprovalStore
from backtest.live.execution import Mt5WritePort


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
    assert mt5.shutdown_count == 10  # two fail-closed shutdowns per bounded connection attempt
    assert client.connected is False


def test_weekend_never_creates_an_executable_prefix(tmp_path) -> None:
    now = pd.Timestamp("2026-08-09T14:00:00Z")

    assert MODULE.core.run_prefix(tmp_path, None, now, now) == {
        "state": "OUTSIDE_TRADE_WINDOW",
        "reason": "WEEKEND",
    }
    assert MODULE.core.finalize_session(
        tmp_path,
        None,
        market_data_asof=now,
        knowledge_asof=now,
        finalization_asof=now,
        fetches={},
    ) == {
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
            company="Fixture Broker Ltd",
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
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_REASON_SL = 4
    DEAL_REASON_TP = 5
    ORDER_TIME_GTC = 0
    ORDER_TIME_SPECIFIED = 2
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009
    ORDER_STATE_CANCELED = 2

    def __init__(self) -> None:
        super().__init__()
        self.pending = []
        self.history_order = None
        self.pending_send_count = 0
        self.history_deal_calls = []

    def symbol_select(self, symbol, visible):
        return symbol in {"US100Cash", "US500Cash"} and visible

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
                symbol=request["symbol"],
                type=request["type"],
                volume_initial=request["volume"],
                volume_current=request["volume"],
                price_open=request["price"],
                sl=request["sl"],
                tp=request["tp"],
                time_expiration=request["expiration"],
            )
            self.pending = [order]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=12345, deal=0)
        if request["action"] == self.TRADE_ACTION_REMOVE:
            if self.pending:
                history_values = vars(self.pending[0]).copy()
                history_values["state"] = self.ORDER_STATE_CANCELED
                self.history_order = SimpleNamespace(**history_values)
            self.pending = []
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=request["order"], deal=0)
        raise AssertionError("Unexpected fake order action.")

    def orders_get(self, ticket=None):
        if ticket is None:
            return tuple(self.pending)
        return tuple(item for item in self.pending if item.ticket == ticket)

    def positions_get(self, ticket=None):
        return ()

    def history_orders_get(self, start=None, end=None, *, ticket=None, position=None):
        if self.history_order is None:
            return ()
        if ticket is not None and int(ticket) != int(self.history_order.ticket):
            return ()
        return (self.history_order,)

    def history_deals_get(self, start=None, end=None, *, ticket=None, position=None):
        self.history_deal_calls.append(
            {"start": start, "end": end, "ticket": ticket, "position": position}
        )
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


class _CanonicalTestWriteAdapter:
    """Test-only adapter that exercises the same durable SQLite write port."""

    def __init__(self, mt5: object) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="otobt-test-write-")
        self.db_path = Path(self._temp.name) / "orders.sqlite3"
        store = ApprovalStore(self.db_path)
        store.close()
        self.port = Mt5WritePort(mt5, self.db_path)

    def send(self, request: dict[str, object]) -> object:
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        operation_id = "test-" + request_hash
        approval_id = "approval-" + request_hash
        action = int(request.get("action", -1))
        operation_type = "CANCEL" if action == 8 else ("CLOSE" if "position" in request else "ENTRY")
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                """INSERT OR IGNORE INTO approvals
                   (approval_id,state,lease_id,lease_nonce,operator_sid,campaign_id,account_key,
                    proposal_id,proposal_hash,approval_type,issued_at_utc,expires_at_utc,
                    release_id,candidate_hash,reason,wire_request_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (approval_id, "CONSUMED", "test-lease", "test-nonce", "S-1-5-21-2",
                 "test-campaign", "318413815", operation_id, request_hash, operation_type,
                 "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00",
                 "test-release", "f" * 64, "", request_hash),
            )
            connection.commit()
        finally:
            connection.close()
        self.port.arm_operator_operation(
            operation_id,
            operation_type=operation_type,
            request=request,
            approval_id=approval_id,
            campaign_id="test-campaign",
            account_key="318413815",
            final_snapshot_hash="1" * 64,
            final_policy_hash="2" * 64,
            final_risk_expires_at="2099-01-01T00:00:00+00:00",
        )
        response = self.port.send(operation_id)
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE order_state_records SET state='WRITE_ACKNOWLEDGED',updated_at_utc=datetime('now') "
                "WHERE order_id=? AND state='WRITE_ATTEMPTED'",
                (operation_id,),
            )
            connection.commit()
        finally:
            connection.close()
        return response


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
    client._write_adapter = _CanonicalTestWriteAdapter(fake_mt5)
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
    with pytest.raises(MODULE.core.CriticalLiveError, match="Super1 production order coordinator"):
        demo_client(mt5).smoke_order(Path("."), {"runtime_config_hash": "test"})


def test_smoke_rejects_foreign_position_exposure_before_order_check() -> None:
    mt5 = FakeOrderMt5()
    mt5.positions_get = lambda: (SimpleNamespace(ticket=1, magic=999),)
    mt5.order_check = lambda request: (_ for _ in ()).throw(AssertionError("order_check must not run"))
    with pytest.raises(MODULE.core.CriticalLiveError, match="Super1 production order coordinator"):
        demo_client(mt5).smoke_order(Path("."), {"runtime_config_hash": "test"})


def test_smoke_does_not_pass_when_foreign_exposure_remains_after_cancellation(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    with pytest.raises(MODULE.core.CriticalLiveError, match="Super1 production order coordinator"):
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


def test_base_client_smoke_is_not_an_execution_path(tmp_path) -> None:
    with pytest.raises(MODULE.core.CriticalLiveError, match="Super1 production order coordinator"):
        demo_client(FakeOrderMt5()).smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash"})


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


def place_candidate(client, output_root, decision=None, reward=2.0):
    now = pd.Timestamp("2026-07-29T14:28:00Z")
    prefix = {
        "date": "2026-07-29",
        "recorded_at": now.isoformat(),
        "cutoffs": {
            "nq": "2026-07-29T10:27:00-04:00",
            "spx": "2026-07-29T10:25:00-04:00",
        },
    }
    return client._place_candidate(
        output_root,
        order_candidate() if decision is None else decision,
        "US100Cash",
        reward,
        prefix_record=prefix,
        send_now=now,
    )


def claim_intent_in_process(output_root_str: str, start_event: object, results: object) -> None:
    start_event.wait()
    output_root = Path(output_root_str)
    client = object.__new__(MODULE.XmMt5DemoOrderClient)
    client._initialize_order_db(output_root)
    connection = sqlite3.connect(client._order_db(output_root), timeout=30.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
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


def resume_pre_send_in_process(
    output_root_str: str,
    start_event: object,
    ready_barrier: object,
    send_counter_path: str,
    results: object,
) -> None:
    try:
        class IndependentProcessMt5(FakeOrderMt5):
            def order_send(self, request):
                if request["action"] == self.TRADE_ACTION_PENDING:
                    connection = sqlite3.connect(
                        send_counter_path, timeout=30.0, isolation_level=None
                    )
                    try:
                        connection.execute("PRAGMA busy_timeout = 30000")
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute("UPDATE counters SET sends = sends + 1")
                        connection.commit()
                    finally:
                        connection.close()
                return super().order_send(request)

        original_state = MODULE.XmMt5DemoOrderClient._intent_state

        def synchronized_state(self, output_root, order_id):
            state = original_state(self, output_root, order_id)
            if order_id == "order-1" and state and state["status"] == "PRE_SEND_DEFERRED":
                ready_barrier.wait(timeout=15)
            return state

        MODULE.XmMt5DemoOrderClient._intent_state = synchronized_state
        start_event.wait(timeout=15)
        result = place_candidate(demo_client(IndependentProcessMt5()), Path(output_root_str))
        results.put({"result": result})
    except Exception as exc:
        results.put({"exception": f"{type(exc).__name__}: {exc}"})


def test_duplicate_candidate_is_submitted_only_once(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    first = place_candidate(client, tmp_path)
    second = place_candidate(client, tmp_path)
    assert first["state"] == "SUBMITTED"
    assert second["state"] == "IDEMPOTENT_ALREADY_SUBMITTED"
    assert mt5.pending_send_count == 1


def test_stale_candidate_is_nonfatal_no_send_and_idempotent(tmp_path) -> None:
    class CrossedQuoteMt5(FakeOrderMt5):
        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=98.0, ask=99.0)

    mt5 = CrossedQuoteMt5()
    client = demo_client(mt5)

    first = place_candidate(client, tmp_path)
    second = place_candidate(client, tmp_path)

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

    first = place_candidate(client, tmp_path)
    second = place_candidate(client, tmp_path)

    assert first == {
        "state": "ENTRY_DISTANCE_WAIT_NO_SEND",
        "order_id": "order-1",
        "reason_code": "ENTRY_DISTANCE_WAIT",
    }
    assert second["state"] == "SUBMITTED"
    assert mt5.pending_send_count == 1


def test_order_check_exception_is_persistent_retryable_without_send(tmp_path) -> None:
    class BrokenCheckMt5(FakeOrderMt5):
        def order_check(self, request):
            raise RuntimeError("check transport failed")

    mt5 = BrokenCheckMt5()
    client = demo_client(mt5)

    result = place_candidate(client, tmp_path)

    assert result["state"] == "CHECK_RETRYABLE_NO_SEND"
    assert result["retcode"] is None
    assert mt5.pending_send_count == 0
    assert client._intent_state(tmp_path, "order-1")["status"] == "CHECK_RETRYABLE"


def test_missing_prefix_is_fail_closed_before_sdk_send(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)

    result = client._place_candidate(tmp_path, order_candidate(), "US100Cash", 2.0)

    assert result["state"] == "PREFIX_REQUIRED_NO_SEND"
    assert mt5.pending_send_count == 0
    assert client._intent_state(tmp_path, "order-1")["status"] == "PREFIX_REQUIRED"


def test_final_guard_expiry_after_send_arm_is_terminal_without_sdk_call(tmp_path, monkeypatch) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    opened = pd.Timestamp("2026-07-29T14:28:00Z")
    expired = pd.Timestamp("2026-07-29T14:31:00Z")
    guard_times = iter([opened, expired])
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: opened)

    result = client._place_candidate(
        tmp_path,
        order_candidate(),
        "US100Cash",
        2.0,
        prefix_record={
            "date": "2026-07-29",
            "recorded_at": opened.isoformat(),
            "cutoffs": {
                "nq": "2026-07-29T10:27:00-04:00",
                "spx": "2026-07-29T10:25:00-04:00",
            },
        },
        send_now=lambda: next(guard_times),
    )

    assert result["state"] == "WINDOW_EXPIRED_NO_SEND"
    assert mt5.pending_send_count == 0
    assert client._intent_state(tmp_path, "order-1")["status"] == "WINDOW_EXPIRED"


def test_check_passed_crash_is_retried_on_restart(tmp_path, monkeypatch) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    original_arm = MODULE.XmMt5DemoOrderClient._arm_send

    def crash_after_check(*args, **kwargs):
        raise RuntimeError("crash after CHECK_PASSED")

    monkeypatch.setattr(MODULE.XmMt5DemoOrderClient, "_arm_send", crash_after_check)
    with pytest.raises(RuntimeError, match="crash after CHECK_PASSED"):
        place_candidate(client, tmp_path)

    assert mt5.pending_send_count == 0
    assert client._intent_state(tmp_path, "order-1")["status"] == "INTENT"
    assert [event["event"] for event in client._events(tmp_path)] == [
        "INTENT", "CHECK_PASSED"
    ]

    monkeypatch.setattr(MODULE.XmMt5DemoOrderClient, "_arm_send", original_arm)
    result = place_candidate(demo_client(mt5), tmp_path)
    assert result["state"] == "SUBMITTED"
    assert mt5.pending_send_count == 1


def test_permission_drop_cannot_arm_and_recovers_without_duplicate_send(tmp_path) -> None:
    class DropPermissionMt5(FakeOrderMt5):
        def __init__(self):
            super().__init__()
            self.drop_next = True

        def order_check(self, request):
            if self.drop_next:
                self.terminal_trade_allowed = False
                self.drop_next = False
            return super().order_check(request)

    mt5 = DropPermissionMt5()
    client = demo_client(mt5)

    blocked = place_candidate(client, tmp_path)

    assert blocked["state"] == "ORDER_PERMISSION_DISABLED_NO_SEND"
    assert mt5.pending_send_count == 0
    assert client._intent_state(tmp_path, "order-1")["status"] == "INTENT"
    assert not any(event["event"] == "SEND_ARMED" for event in client._events(tmp_path))

    mt5.terminal_trade_allowed = True
    result = place_candidate(client, tmp_path)
    assert result["state"] == "SUBMITTED"
    assert mt5.pending_send_count == 1


def test_send_armed_crash_is_reconciled_without_resend(tmp_path, monkeypatch) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    original_send = mt5.order_send
    monkeypatch.setattr(mt5, "order_send", lambda request: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        place_candidate(client, tmp_path)

    assert mt5.pending_send_count == 0
    assert client._intent_state(tmp_path, "order-1")["status"] == "SEND_ARMED"

    monkeypatch.setattr(mt5, "order_send", original_send)
    result = place_candidate(demo_client(mt5), tmp_path)
    assert result["state"] == "IDEMPOTENT_SEND_ARMED_RECONCILE_REQUIRED"
    assert mt5.pending_send_count == 0


def test_sdk_unknown_result_is_persistent_and_never_replayed(tmp_path) -> None:
    class UnknownSendMt5(FakeOrderMt5):
        def order_send(self, request):
            if request["action"] == self.TRADE_ACTION_PENDING:
                self.pending_send_count += 1
                return None
            return super().order_send(request)

    mt5 = UnknownSendMt5()
    client = demo_client(mt5)

    first = place_candidate(client, tmp_path)
    second = place_candidate(client, tmp_path)

    assert first["state"] == "SEND_UNKNOWN_NO_SEND"
    assert second["state"] == "IDEMPOTENT_SEND_UNKNOWN_NO_SEND"
    assert mt5.pending_send_count == 1
    assert [item["event"] for item in client._events(tmp_path)] == [
        "INTENT",
        "CHECK_PASSED",
        "SEND_ARMED",
        "SEND_UNKNOWN",
    ]


@pytest.mark.parametrize("mode", ["none", "timeout", "connection", "partial"])
def test_unknown_or_partial_candidate_blocks_later_candidate_in_same_cycle(
    tmp_path,
    mode,
    monkeypatch,
    request,
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    class UncertainSendMt5(FakeOrderMt5):
        TRADE_RETCODE_DONE_PARTIAL = 10010

        def __init__(self):
            super().__init__()
            self.hidden = True
            self.deals = []
            self.history_order = None

        def orders_get(self, ticket=None):
            if self.hidden:
                return ()
            return super().orders_get(ticket=ticket)

        def history_orders_get(self, start=None, end=None, *, ticket=None, position=None):
            if self.hidden or self.history_order is None:
                return ()
            order = self.history_order
            if ticket is not None and int(ticket) != int(order.ticket):
                return ()
            return (order,)

        def history_deals_get(self, start=None, end=None, *, ticket=None, position=None):
            if self.hidden:
                return ()
            result = list(self.deals)
            if ticket is not None:
                result = [item for item in result if int(item.order) == int(ticket)]
            if position is not None:
                result = [item for item in result if int(item.position_id) == int(position)]
            return tuple(result)

        def order_send(self, request):
            if request["action"] == self.TRADE_ACTION_PENDING:
                self.pending_send_count += 1
                self.pending = [SimpleNamespace(
                    ticket=12345,
                    magic=request["magic"],
                    comment=request["comment"],
                    symbol=request["symbol"],
                    type=request["type"],
                    volume_initial=request["volume"],
                    volume_current=request["volume"] if mode != "partial" else request["volume"] - 0.04,
                    price_open=request["price"],
                    sl=request["sl"],
                    tp=request["tp"],
                    time_expiration=request["expiration"],
                )]
                self.history_order = SimpleNamespace(**vars(self.pending[0]), state=4)
                if mode == "partial":
                    self.deals = [SimpleNamespace(
                        ticket=500,
                        order=12345,
                        position_id=700,
                        symbol=request["symbol"],
                        entry=self.DEAL_ENTRY_IN,
                        reason=0,
                        price=request["price"],
                        volume=0.04,
                        comment=request["comment"],
                        magic=request["magic"],
                        time_msc=1785330300000,
                        type=self.ORDER_TYPE_BUY,
                    )]
                if mode == "none":
                    return None
                if mode == "timeout":
                    raise TimeoutError("SDK send timed out")
                if mode == "connection":
                    raise ConnectionError("SDK connection dropped")
                return SimpleNamespace(
                    retcode=self.TRADE_RETCODE_DONE_PARTIAL,
                    order=12345,
                    deal=0,
                    comment="partial",
                )
            return super().order_send(request)

    first = {
        **order_candidate(),
        "order_id": "order-1",
        "setup_state": "VALID",
        "order_state": "CANCELLED",
        "terminal_reason": "CANCELLED_TRADE_WINDOW_END",
        "pair_cap_state": "ALLOWED",
        "intrabar_ambiguity": "",
    }
    second = {**first, "order_id": "order-2", "fvg_time": "2026-07-29T13:01:00Z"}
    prefix_path = tmp_path / "prefix.json"
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
                "hashes": {"config": "config-hash", "code": MODULE.core.source_code_hash()},
                "payload": {"decisions": [first, second]},
            }
        ),
        encoding="utf-8",
    )
    mt5 = UncertainSendMt5()
    client = demo_client(mt5)
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: pd.Timestamp("2026-07-29T14:28:00Z"))
    result = client.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        pd.Timestamp("2026-07-29T14:28:00Z"),
        {"live_config_hash": "config-hash"},
    )

    assert mt5.pending_send_count == 1
    assert result["results"][0]["state"] in {
        "SEND_UNKNOWN_NO_SEND",
        "SEND_PARTIAL_NO_SEND",
    }
    assert result["results"][1] == {
        "state": "BLOCKED_BY_PRIOR_BROKER_UNCERTAINTY_NO_SEND",
        "order_id": "order-2",
        "reason": "A prior candidate in this cycle returned UNKNOWN/PARTIAL.",
    }

    restarted = demo_client(mt5)
    mt5.hidden = False
    next_cycle = restarted.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        pd.Timestamp("2026-07-29T14:29:00Z"),
        {"live_config_hash": "config-hash"},
    )
    assert mt5.pending_send_count == (1 if mode == "partial" else 2)
    assert next_cycle["state"] in {
        "RECONCILED",
        "UNKNOWN_NO_SEND",
        "BROKER_UNSAFE_NO_SEND",
        "CANCEL_UNKNOWN_NO_SEND",
    }
    assert any(event.get("event") == "LINKED_EXISTING" for event in restarted._events(tmp_path))
    record_if_enabled(request, evidence_token)


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

    first = place_candidate(client, tmp_path, decision)
    second = place_candidate(client, tmp_path, decision)

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

    first = place_candidate(client, tmp_path)
    second = place_candidate(client, tmp_path)

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
            "SEND_ARMED",
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
        place_candidate(client, tmp_path)
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

    result = place_candidate(client, tmp_path)

    assert result["state"] == "IDEMPOTENT_LINKED_EXISTING"
    assert mt5.pending_send_count == 0
    events = client._events(tmp_path)
    assert [event["event"] for event in events] == ["INTENT", "LINKED_EXISTING"]


def test_data_invalid_cancels_own_pending_order_without_resubmitting(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
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
    place_candidate(client, tmp_path, reward=3.0)

    result = client.reconcile_orders(
        tmp_path,
        {"state": "OUTSIDE_TRADE_WINDOW"},
        pd.Timestamp("2026-07-29T13:00:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["state"] == "NO_EXECUTABLE_PREFIX"
    assert len(result["cancelled"]) == 1
    assert mt5.pending == []


def test_final_trade_window_prefix_cancels_instead_of_submitting(tmp_path, monkeypatch) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    place_candidate(client, tmp_path, reward=3.0)
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
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: pd.Timestamp("2026-07-29T14:31:00Z"))

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


def test_executable_intrawindow_prefix_reaches_broker_submission(tmp_path, monkeypatch) -> None:
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
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: pd.Timestamp("2026-07-29T14:28:00Z"))

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


def test_stale_prefix_defers_without_check_passed_then_retries_once(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    stale_prefix = {
        "date": "2026-07-29",
        "recorded_at": "2026-07-29T14:25:00Z",
        "cutoffs": {"nq": "2026-07-29T10:24:00-04:00", "spx": "2026-07-29T10:25:00-04:00"},
    }
    fresh_prefix = {**stale_prefix, "cutoffs": {"nq": "2026-07-29T10:27:00-04:00", "spx": "2026-07-29T10:25:00-04:00"}}
    now = pd.Timestamp("2026-07-29T14:28:00Z")

    first = client._place_candidate(
        tmp_path, order_candidate(), "US100Cash", 2.0,
        prefix_record=stale_prefix, send_now=now,
    )
    assert first["state"] == "STALE_PREFIX_NO_SEND"
    assert mt5.pending_send_count == 0
    assert client._intent_state(tmp_path, "order-1")["status"] == "PRE_SEND_DEFERRED"
    assert [item["event"] for item in client._events(tmp_path)] == ["PRE_SEND_DEFERRED"]

    second = client._place_candidate(
        tmp_path, order_candidate(), "US100Cash", 2.0,
        prefix_record=fresh_prefix, send_now=now,
    )
    assert second["state"] == "SUBMITTED"
    assert mt5.pending_send_count == 1
    assert [item["event"] for item in client._events(tmp_path)] == [
            "PRE_SEND_DEFERRED", "INTENT_REEVALUATED", "CHECK_PASSED", "SEND_ARMED", "SUBMITTED"
    ]

    third = client._place_candidate(
        tmp_path, order_candidate(), "US100Cash", 2.0,
        prefix_record=fresh_prefix, send_now=now,
    )
    assert third["state"] == "IDEMPOTENT_ALREADY_SUBMITTED"
    assert mt5.pending_send_count == 1


def test_reconcile_common_cutoff_vector_rejects_stale_then_sends_once_when_fresh(
    tmp_path, monkeypatch, request
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    decision = {
        **order_candidate(),
        "leg_key": "spx",
        "setup_state": "VALID",
        "order_state": "CANCELLED",
        "terminal_reason": "CANCELLED_TRADE_WINDOW_END",
        "pair_cap_state": "ALLOWED",
        "intrabar_ambiguity": "",
    }
    prefix_path = tmp_path / "prefix.json"

    def write_prefix(nq_cutoff: str, spx_cutoff: str) -> None:
        prefix_path.write_text(
            json.dumps(
                {
                    "date": "2026-07-29",
                    "state": "VALID",
                    "deterministic_rerun": True,
                    "invariant_errors": [],
                    "cutoffs": {"nq": nq_cutoff, "spx": spx_cutoff},
                    "hashes": {
                        "config": "config-hash",
                        "code": MODULE.core.source_code_hash(),
                    },
                    "payload": {"decisions": [decision]},
                }
            ),
            encoding="utf-8",
        )

    now = pd.Timestamp("2026-07-29T14:24:10Z")
    monkeypatch.setattr(MODULE.core, "utc_now", lambda: now)
    write_prefix("2026-07-29T10:21:00-04:00", "2026-07-29T10:20:00-04:00")
    stale = client.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        now,
        {"live_config_hash": "config-hash"},
    )
    assert stale["results"][0]["state"] == "STALE_PREFIX_NO_SEND"
    assert mt5.pending_send_count == 0

    write_prefix("2026-07-29T10:24:00-04:00", "2026-07-29T10:20:00-04:00")
    fresh = client.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        now,
        {"live_config_hash": "config-hash"},
    )
    assert fresh["results"][0]["state"] == "SUBMITTED"
    assert mt5.pending_send_count == 1
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize("mode", ["none", "exception", "timeout-retcode", "connection-retcode"])
def test_remove_unknown_is_persistent_single_attempt_and_later_broker_resolved(
    tmp_path, mode
) -> None:
    class CancelUnknownMt5(FakeOrderMt5):
        ORDER_STATE_CANCELED = 2

        def __init__(self):
            super().__init__()
            self.remove_count = 0
            self.readback_ready = False
            self.history_order = None

        def order_send(self, request):
            if request["action"] != self.TRADE_ACTION_REMOVE:
                return super().order_send(request)
            self.remove_count += 1
            self.history_order = SimpleNamespace(
                **vars(self.pending[0]), state=self.ORDER_STATE_CANCELED
            )
            self.pending = []
            if mode == "none":
                return None
            if mode == "exception":
                raise TimeoutError("REMOVE response lost after broker apply")
            if mode == "timeout-retcode":
                return SimpleNamespace(retcode=10012, order=request["order"], deal=0)
            return SimpleNamespace(retcode=10031, order=request["order"], deal=0)

        def history_orders_get(self, start=None, end=None, *, ticket=None, position=None):
            if not self.readback_ready or self.history_order is None:
                return ()
            if ticket is not None and int(ticket) != int(self.history_order.ticket):
                return ()
            return (self.history_order,)

    mt5 = CancelUnknownMt5()
    client = demo_client(mt5)
    place_candidate(client, tmp_path)

    first = client.cancel_all_pending(tmp_path, "TECHNICAL_RECOVERY")
    assert first[0]["state"] in {"CANCEL_UNKNOWN", "CANCEL_REJECTED"}
    assert mt5.remove_count == 1
    assert client._intent_state(tmp_path, "order-1")["status"] in {"CANCEL_UNKNOWN", "CANCEL_REJECTED"}
    assert not (tmp_path / "fatal_latch.json").exists()

    restarted = demo_client(mt5)
    mt5.readback_ready = True
    resolved = restarted._reconcile_persistent_intents(
        tmp_path, pd.Timestamp("2026-07-29T14:30:00Z")
    )
    assert resolved[0]["state"] == "CANCELLED"
    assert mt5.remove_count == 1
    assert restarted._intent_state(tmp_path, "order-1")["status"] == "CANCELLED"
    assert not mt5.pending


class ReconcileCancelMt5(FakeOrderMt5):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.remove_count = 0
        self.cancelled_order = None
        self.history_order = None
        self.pending_requests = []

    def order_send(self, request):
        if request["action"] == self.TRADE_ACTION_PENDING:
            self.pending_requests.append(dict(request))
            return super().order_send(request)
        if request["action"] != self.TRADE_ACTION_REMOVE:
            return super().order_send(request)
        self.remove_count += 1
        if self.pending:
            self.cancelled_order = SimpleNamespace(**vars(self.pending[0]))
        if self.mode != "pending-visible":
            self.pending = []
        if self.mode == "reject":
            return SimpleNamespace(retcode=10006, order=request["order"], deal=0)
        if self.mode in {"none", "pending-visible"}:
            return None
        if self.mode == "exception":
            raise TimeoutError("REMOVE response lost")
        if self.mode == "timeout-retcode":
            return SimpleNamespace(retcode=10012, order=request["order"], deal=0)
        if self.mode == "connection-retcode":
            return SimpleNamespace(retcode=10031, order=request["order"], deal=0)
        raise AssertionError(f"unknown cancellation fixture mode {self.mode}")

    def confirm_cancel(self) -> None:
        if self.cancelled_order is None:
            raise AssertionError("fixture has no pending order to confirm")
        values = vars(self.cancelled_order).copy()
        values["state"] = self.ORDER_STATE_CANCELED
        self.pending = []
        self.history_order = SimpleNamespace(**values)

    def history_orders_get(self, start=None, end=None, *, ticket=None, position=None):
        del start, end, position
        if self.history_order is None:
            return ()
        if ticket is not None and int(ticket) != int(self.history_order.ticket):
            return ()
        return (self.history_order,)


def reconcile_candidate(order_id: str) -> dict[str, object]:
    return {
        **order_candidate(),
        "order_id": order_id,
        "setup_state": "VALID",
        "order_state": "CANCELLED",
        "terminal_reason": "CANCELLED_TRADE_WINDOW_END",
        "pair_cap_state": "ALLOWED",
        "intrabar_ambiguity": "",
    }


def reconcile_valid_candidate(client, output_root, order_id: str):
    now = pd.Timestamp("2026-07-29T14:28:00Z")
    prefix_path = output_root / "valid-prefix.json"
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
                "payload": {"decisions": [reconcile_candidate(order_id)]},
            }
        ),
        encoding="utf-8",
    )
    original_utc_now = MODULE.core.utc_now
    MODULE.core.utc_now = lambda: now
    try:
        return client.reconcile_orders(
            output_root,
            {"state": "VALID", "path": str(prefix_path)},
            now,
            {"live_config_hash": "config-hash"},
        )
    finally:
        MODULE.core.utc_now = original_utc_now


@pytest.mark.parametrize(
    "mode",
    ["none", "exception", "timeout-retcode", "connection-retcode"],
    ids=["none", "exception", "timeout", "connection"],
)
def test_r02_a_reconcile_restart_lost_remove_blocks_entry_and_resolves(tmp_path, mode, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = ReconcileCancelMt5(mode)
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    first_comment = mt5.pending_requests[0]["comment"]

    first = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T14:28:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert first["state"] == "CANCEL_UNKNOWN_NO_SEND"
    assert mt5.remove_count == 1
    assert client._intent_state(tmp_path, "order-1")["status"] == "CANCEL_UNKNOWN"
    armed = [item for item in client._events(tmp_path) if item.get("event") == "CANCEL_ARMED"]
    assert len(armed) == 1
    assert armed[0]["broker_order_ticket"] == 12345

    restarted = demo_client(mt5)
    blocked = reconcile_valid_candidate(restarted, tmp_path, "order-2")
    assert blocked["state"] == "CANCEL_UNKNOWN_NO_SEND"
    assert mt5.remove_count == 1
    assert mt5.pending_send_count == 1

    mt5.confirm_cancel()
    resolved = reconcile_valid_candidate(restarted, tmp_path, "order-2")
    assert resolved["state"] == "RECONCILED"
    assert mt5.remove_count == 1
    assert mt5.pending_send_count == 2
    assert mt5.pending_requests[-1]["comment"] != first_comment
    record_if_enabled(request, evidence_token)


def test_r02_b_pending_visible_restart_does_not_repeat_remove_or_send(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = ReconcileCancelMt5("pending-visible")
    client = demo_client(mt5)
    place_candidate(client, tmp_path)

    first = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T14:28:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert first["state"] == "CANCEL_UNKNOWN_NO_SEND"
    restarted = demo_client(mt5)
    second = restarted.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T14:29:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert second["state"] == "CANCEL_UNKNOWN_NO_SEND"
    assert mt5.remove_count == 1
    assert mt5.pending_send_count == 1
    assert mt5.pending
    record_if_enabled(request, evidence_token)


def test_r02_c_cancel_armed_crash_before_sdk_is_not_retried(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = ReconcileCancelMt5("none")
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    client._order_send_checked = lambda *args, **kwargs: (_ for _ in ()).throw(
        KeyboardInterrupt("process interrupted before REMOVE")
    )

    with pytest.raises(KeyboardInterrupt):
        client.cancel_all_pending(tmp_path, "TEST_CRASH")

    assert mt5.remove_count == 0
    assert client._intent_state(tmp_path, "order-1")["status"] == "CANCEL_ARMED"
    restarted = demo_client(mt5)
    blocked = reconcile_valid_candidate(restarted, tmp_path, "order-2")
    assert blocked["state"] == "CANCEL_UNKNOWN_NO_SEND"
    assert mt5.remove_count == 0
    assert mt5.pending_send_count == 1
    record_if_enabled(request, evidence_token)


def test_r02_e_rejected_remove_stays_distinct_then_confirmed_cancel_allows_new_order(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = ReconcileCancelMt5("reject")
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    rejected = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T14:28:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert rejected["state"] == "CANCEL_UNKNOWN_NO_SEND"
    assert client._intent_state(tmp_path, "order-1")["status"] == "CANCEL_REJECTED"
    assert mt5.remove_count == 1

    restarted = demo_client(mt5)
    blocked = reconcile_valid_candidate(restarted, tmp_path, "order-2")
    assert blocked["state"] == "CANCEL_UNKNOWN_NO_SEND"
    assert mt5.remove_count == 1
    mt5.confirm_cancel()
    allowed = reconcile_valid_candidate(restarted, tmp_path, "order-2")
    assert allowed["state"] == "RECONCILED"
    assert mt5.remove_count == 1
    assert mt5.pending_send_count == 2
    assert mt5.pending_requests[-1]["symbol"] == "US100Cash"
    record_if_enabled(request, evidence_token)


class DetailedOrderMt5(FakeOrderMt5):
    def order_send(self, request):
        if request["action"] == self.TRADE_ACTION_PENDING:
            self.pending_send_count += 1
            self.pending = [
                SimpleNamespace(
                    ticket=12345,
                    magic=request["magic"],
                    comment=request["comment"],
                    symbol=request["symbol"],
                    type=request["type"],
                    volume_initial=request["volume"],
                    volume_current=request["volume"],
                    price_open=request["price"],
                    sl=request["sl"],
                    tp=request["tp"],
                    time_expiration=request["expiration"],
                )
            ]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=12345, deal=0)
        return super().order_send(request)


def test_pending_broker_request_mismatch_blocks_new_orders(tmp_path) -> None:
    mt5 = DetailedOrderMt5()
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    mt5.pending[0].sl = 89.0

    result = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["state"] == "BROKER_UNSAFE_NO_SEND"
    assert result["broker_states"][0]["state"] == "BROKER_REQUEST_MISMATCH_NO_SEND"
    assert mt5.pending_send_count == 1
    assert mt5.pending == []


class PartialFillMt5(DetailedOrderMt5):
    ORDER_STATE_FILLED = 4
    ORDER_TYPE_BUY = 0
    DEAL_ENTRY_IN = 0

    def __init__(self, *, protected: bool = False) -> None:
        super().__init__()
        self.protected = protected
        self.history_order = None
        self.deals = []
        self.position = None

    def order_send(self, request):
        result = super().order_send(request)
        if request["action"] == self.TRADE_ACTION_PENDING:
            self.history_order = SimpleNamespace(
                ticket=12345,
                magic=request["magic"],
                comment=request["comment"],
                symbol=request["symbol"],
                type=request["type"],
                volume_initial=request["volume"],
                volume_current=0.0 if self.protected else 0.06,
                price_open=request["price"],
                sl=request["sl"],
                tp=request["tp"],
                time_expiration=request["expiration"],
                state=self.ORDER_STATE_FILLED,
            )
            self.pending = []
            volume = 0.1 if self.protected else 0.04
            self.deals = [
                SimpleNamespace(
                    ticket=500,
                    order=12345,
                    position_id=700,
                    symbol=request["symbol"],
                    entry=self.DEAL_ENTRY_IN,
                    reason=0,
                    price=100.5,
                    volume=volume,
                    comment=request["comment"],
                    magic=request["magic"],
                    time_msc=1785330300000,
                    type=self.ORDER_TYPE_BUY,
                )
            ]
            if self.protected:
                self.position = SimpleNamespace(
                    ticket=700,
                    identifier=700,
                    magic=request["magic"],
                    comment=request["comment"],
                    symbol=request["symbol"],
                    type=0,
                    volume=volume,
                    sl=request["sl"],
                    tp=request["tp"],
                )
        return result

    def positions_get(self, ticket=None):
        if self.position is None:
            return ()
        if ticket is not None and int(self.position.ticket) != int(ticket):
            return ()
        return (self.position,)

    def history_orders_get(self, start=None, end=None, *, ticket=None, position=None):
        if ticket is not None and int(ticket) != 12345:
            return ()
        return () if self.history_order is None else (self.history_order,)

    def history_deals_get(self, start=None, end=None, *, ticket=None, position=None):
        self.history_deal_calls.append(
            {"start": start, "end": end, "ticket": ticket, "position": position}
        )
        result = tuple(self.deals)
        if ticket is not None:
            result = tuple(item for item in result if int(item.order) == int(ticket))
        if position is not None:
            result = tuple(item for item in result if int(item.position_id) == int(position))
        return result


class PartialCancelMt5(PartialFillMt5):
    def __init__(self) -> None:
        super().__init__(protected=False)
        self.remove_count = 0
        self.cancelled_order = None
        self.history_order = None
        self.position_calls = 0

    def order_send(self, request):
        if request["action"] != self.TRADE_ACTION_REMOVE:
            return super().order_send(request)
        self.remove_count += 1
        self.cancelled_order = SimpleNamespace(**vars(self.pending[0]))
        self.pending = []
        return None

    def positions_get(self, ticket=None):
        self.position_calls += 1
        return super().positions_get(ticket=ticket)

    def confirm_cancel(self) -> None:
        if self.cancelled_order is None:
            raise AssertionError("REMOVE did not create a broker history fixture")
        values = vars(self.cancelled_order).copy()
        values["state"] = self.ORDER_STATE_CANCELED
        values["volume_current"] = 0.06
        self.history_order = SimpleNamespace(**values)

    def confirm_exit(self, reason: int) -> None:
        self.position = None
        self.deals.append(
            SimpleNamespace(
                ticket=501,
                order=99999,
                position_id=700,
                symbol="US100Cash",
                entry=self.DEAL_ENTRY_OUT,
                reason=reason,
                price=self.history_order.tp if reason == self.DEAL_REASON_TP else self.history_order.sl,
                volume=0.04,
                comment="broker-exit",
                magic=260729315,
                time_msc=1785330360000,
                type=self.ORDER_TYPE_SELL,
            )
        )


@pytest.mark.parametrize(
    ("exit_reason", "expected_terminal"),
    [(FakeOrderMt5.DEAL_REASON_SL, "CLOSED_SL"), (FakeOrderMt5.DEAL_REASON_TP, "CLOSED_TP")],
)
def test_r02_d_cancel_control_resolves_remainder_before_economic_exit(
    tmp_path, exit_reason, expected_terminal, request
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = PartialCancelMt5()
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    mt5.deals[0].volume = 0.04
    mt5.history_order.volume_current = 0.06
    mt5.position = SimpleNamespace(
        ticket=700,
        identifier=700,
        magic=client.magic,
        comment=client._comment("nq", "order-1"),
        symbol="US100Cash",
        type=0,
        volume=0.04,
        sl=mt5.history_order.sl,
        tp=mt5.history_order.tp,
    )
    mt5.pending = [SimpleNamespace(**vars(mt5.history_order))]

    first = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T14:28:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert first["state"] == "CANCEL_UNKNOWN_NO_SEND"
    assert mt5.remove_count == 1
    mt5.confirm_cancel()
    restarted = demo_client(mt5)
    resolved = restarted.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T14:30:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert resolved["state"] == "BROKER_UNSAFE_NO_SEND"
    economic = next(item for item in resolved["broker_states"] if item["order_id"] == "order-1")
    assert economic["state"] == "PARTIAL_FILL"
    assert economic["protection_state"] == "PROTECTED"
    assert economic["open_position_volume"] == 0.04
    assert restarted._intent_state(tmp_path, "order-1")["status"] == "LINKED_EXISTING"
    cancel_events = [item for item in restarted._events(tmp_path) if item.get("event") == "CANCEL_RESOLVED"]
    assert len(cancel_events) == 1
    assert cancel_events[0]["cancelled_remainder_volume"] == 0.06
    assert cancel_events[0]["entry_filled_volume"] == 0.04
    assert cancel_events[0]["position_id"] == 700
    assert mt5.remove_count == 1
    assert mt5.position_calls > 0
    assert any(call["position"] == 700 for call in mt5.history_deal_calls)

    mt5.confirm_exit(exit_reason)
    terminal = restarted.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T14:31:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert terminal["state"] == "DATA_INVALID_NO_SEND"
    assert next(item for item in terminal["broker_states"] if item["order_id"] == "order-1")["state"] == expected_terminal
    assert restarted._stored_broker_state(tmp_path, "order-1")[0] == expected_terminal
    assert restarted._intent_state(tmp_path, "order-1")["status"] == expected_terminal
    assert mt5.remove_count == 1
    record_if_enabled(request, evidence_token)


@pytest.mark.parametrize("legacy_status", ["CANCEL_UNKNOWN", "CANCEL_REJECTED"])
def test_r02_p_legacy_cancel_without_arm_uses_outbox_ticket_without_fabricating_request(
    tmp_path, legacy_status, monkeypatch, request
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    monkeypatch.setattr(
        MODULE.core,
        "utc_now",
        lambda: pd.Timestamp("2026-07-29T14:28:00Z"),
    )
    mt5 = ReconcileCancelMt5("none")
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    mt5.cancelled_order = SimpleNamespace(**vars(mt5.pending[0]))
    mt5.pending = []
    connection = client._order_connection(tmp_path)
    try:
        connection.execute(
            "UPDATE order_intents SET status=?, request_json=NULL, broker_ticket=NULL WHERE order_id=?",
            (legacy_status, "order-1"),
        )
        connection.execute(
            "INSERT INTO order_event_outbox (order_id, event_json, delivered_at) VALUES (?, ?, ?)",
            (
                "order-1",
                json.dumps({
                    "event": legacy_status,
                    "order_id": "order-1",
                    "ticket": 12345,
                    "broker_order_ticket": 12345,
                }),
                "legacy-acked",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    prefix_path = tmp_path / "valid-prefix.json"
    prefix_path.write_text(
        json.dumps({
            "date": "2026-07-29",
            "state": "VALID",
            "deterministic_rerun": True,
            "invariant_errors": [],
            "cutoffs": {"nq": "2026-07-29T10:27:00-04:00", "spx": "2026-07-29T10:25:00-04:00"},
            "hashes": {"config": "config-hash", "code": MODULE.core.source_code_hash()},
            "payload": {"decisions": [reconcile_candidate("order-2")]},
        }),
        encoding="utf-8",
    )
    restarted = demo_client(mt5)
    first = restarted.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        pd.Timestamp("2026-07-29T14:30:00Z"),
        {"live_config_hash": "config-hash"},
    )
    assert first["state"] == "CANCEL_UNKNOWN_NO_SEND"
    assert mt5.remove_count == 0
    assert mt5.pending_send_count == 1
    assert restarted._intent_state(tmp_path, "order-1")["status"] == legacy_status

    mt5.confirm_cancel()
    second = restarted.reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        pd.Timestamp("2026-07-29T14:28:00Z"),
        {"live_config_hash": "config-hash"},
    )
    assert second["state"] == "RECONCILED"
    assert mt5.remove_count == 0
    assert mt5.pending_send_count == 2
    assert restarted._intent_state(tmp_path, "order-1")["status"] == "CANCELLED"
    record_if_enabled(request, evidence_token)


def test_r02_p_legacy_outbox_ticket_conflict_is_unsafe_without_remove_or_send(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = ReconcileCancelMt5("none")
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    mt5.pending = []
    connection = client._order_connection(tmp_path)
    try:
        connection.execute(
            "UPDATE order_intents SET status='CANCEL_UNKNOWN', request_json=NULL, broker_ticket=12345 WHERE order_id='order-1'"
        )
        connection.execute(
            "INSERT INTO order_event_outbox (order_id, event_json, delivered_at) VALUES (?, ?, ?)",
            ("order-1", json.dumps({"event": "CANCEL_UNKNOWN", "order_id": "order-1", "ticket": 99999}), "legacy-acked"),
        )
        connection.commit()
    finally:
        connection.close()
    prefix_path = tmp_path / "valid-prefix.json"
    prefix_path.write_text(
        json.dumps({
            "date": "2026-07-29",
            "state": "VALID",
            "deterministic_rerun": True,
            "invariant_errors": [],
            "cutoffs": {"nq": "2026-07-29T10:27:00-04:00", "spx": "2026-07-29T10:25:00-04:00"},
            "hashes": {"config": "config-hash", "code": MODULE.core.source_code_hash()},
            "payload": {"decisions": [reconcile_candidate("order-2")]},
        }),
        encoding="utf-8",
    )
    result = demo_client(mt5).reconcile_orders(
        tmp_path,
        {"state": "VALID", "path": str(prefix_path)},
        pd.Timestamp("2026-07-29T14:30:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )
    assert result["state"] == "BROKER_UNSAFE_NO_SEND"
    assert result["broker_states"][0]["state"] == "BROKER_REQUEST_MISMATCH_NO_SEND"
    record_if_enabled(request, evidence_token)
    assert mt5.remove_count == 0
    assert mt5.pending_send_count == 1


class ClosedWithDifferentExitOrderMt5(PartialFillMt5):
    def __init__(self) -> None:
        super().__init__(protected=False)

    def order_send(self, request):
        result = super().order_send(request)
        if request["action"] == self.TRADE_ACTION_PENDING:
            self.deals[0] = SimpleNamespace(**vars(self.deals[0]))
            self.deals[0].volume = 0.1
            self.deals.append(
                SimpleNamespace(
                    ticket=501,
                    order=99999,
                    position_id=700,
                    symbol=request["symbol"],
                    entry=self.DEAL_ENTRY_OUT,
                    reason=self.DEAL_REASON_SL,
                    price=90.0,
                    volume=0.1,
                    comment="broker-generated-exit",
                    magic=999,
                    time_msc=1785330360000,
                    type=self.ORDER_TYPE_SELL,
                )
            )
        return result


class DocumentedLifecycleMt5(FakeOrderMt5):
    ORDER_STATE_FILLED = 4

    def __init__(self, direction: str, exit_reason: int | None = None) -> None:
        super().__init__()
        self.direction = direction
        self.exit_reason = exit_reason
        self.history_order = None
        self.deals = []
        self.position = None

    def order_send(self, request):
        result = super().order_send(request)
        if request["action"] != self.TRADE_ACTION_PENDING:
            return result
        entry_type = self.ORDER_TYPE_BUY if self.direction == "long" else self.ORDER_TYPE_SELL
        exit_type = self.ORDER_TYPE_SELL if self.direction == "long" else self.ORDER_TYPE_BUY
        self.history_order = SimpleNamespace(
            ticket=1001,
            magic=request["magic"],
            comment=request["comment"],
            symbol=request["symbol"],
            type=request["type"],
            volume_initial=0.1,
            volume_current=0.0,
            price_open=request["price"],
            sl=request["sl"],
            tp=request["tp"],
            time_expiration=request["expiration"],
            state=self.ORDER_STATE_FILLED,
        )
        # The strategy intent must link to the actual entry order returned by
        # the broker, not to the generic fake ticket used by the base helper.
        self.pending[0].ticket = 1001
        self.pending = []
        self.deals = [
            SimpleNamespace(
                ticket=2001,
                order=1001,
                position_id=3001,
                symbol=request["symbol"],
                entry=self.DEAL_ENTRY_IN,
                reason=0,
                price=request["price"],
                volume=0.1,
                comment=request["comment"],
                magic=request["magic"],
                time_msc=1785330300000,
                type=entry_type,
            )
        ]
        if self.exit_reason is None:
            self.position = SimpleNamespace(
                ticket=3001,
                identifier=3001,
                magic=request["magic"],
                comment=request["comment"],
                symbol=request["symbol"],
                type=self.POSITION_TYPE_BUY if self.direction == "long" else self.POSITION_TYPE_SELL,
                volume=0.1,
                sl=request["sl"],
                tp=request["tp"],
            )
        else:
            self.deals.append(
                SimpleNamespace(
                    ticket=2002,
                    order=1002,
                    position_id=3001,
                    symbol=request["symbol"],
                    entry=self.DEAL_ENTRY_OUT,
                    reason=self.exit_reason,
                    price=request["sl"] if self.exit_reason == self.DEAL_REASON_SL else request["tp"],
                    volume=0.1,
                    comment="broker-exit",
                    magic=0,
                    time_msc=1785330360000,
                    type=exit_type,
                )
            )
        return SimpleNamespace(
            retcode=result.retcode,
            order=1001,
            deal=0,
        )

    def orders_get(self, ticket=None):
        if ticket is None:
            return tuple(self.pending)
        return tuple(item for item in self.pending if int(item.ticket) == int(ticket))

    def positions_get(self, ticket=None):
        if self.position is None:
            return ()
        if ticket is not None and int(self.position.ticket) != int(ticket):
            return ()
        return (self.position,)

    def history_orders_get(self, start=None, end=None, *, ticket=None, position=None):
        del start, end, position
        if ticket is not None and int(ticket) != 1001:
            return ()
        return () if self.history_order is None else (self.history_order,)

    def history_deals_get(self, start=None, end=None, *, ticket=None, position=None):
        self.history_deal_calls.append(
            {"start": start, "end": end, "ticket": ticket, "position": position}
        )
        result = tuple(self.deals)
        if ticket is not None:
            result = tuple(item for item in result if int(item.order) == int(ticket))
        if position is not None:
            result = tuple(item for item in result if int(item.position_id) == int(position))
        return result


@pytest.mark.parametrize(
    ("direction", "exit_reason", "expected"),
    [
        ("long", None, "OPEN_PROTECTED"),
        ("short", None, "OPEN_PROTECTED"),
        ("long", FakeOrderMt5.DEAL_REASON_SL, "CLOSED_SL"),
        ("short", FakeOrderMt5.DEAL_REASON_TP, "CLOSED_TP"),
    ],
)
def test_documented_mt5_order_deal_position_chain_for_long_short_and_exit(
    tmp_path,
    direction,
    exit_reason,
    expected,
    request,
) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = DocumentedLifecycleMt5(direction, exit_reason)
    client = demo_client(mt5)
    decision = {
        **order_candidate(),
        "direction": direction,
        "stop_price": 120.0 if direction == "short" else 90.0,
        "target_price": 90.0 if direction == "short" else 120.0,
    }
    assert place_candidate(client, tmp_path, decision)["state"] == "SUBMITTED"

    result = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["broker_states"][0]["state"] == expected
    assert any(call["ticket"] == 1001 for call in mt5.history_deal_calls)
    assert any(call["position"] == 3001 for call in mt5.history_deal_calls)
    record_if_enabled(request, evidence_token)


def test_closed_state_uses_position_chain_and_allows_different_exit_order_ticket(tmp_path) -> None:
    mt5 = ClosedWithDifferentExitOrderMt5()
    client = demo_client(mt5)
    place_candidate(client, tmp_path)

    result = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["state"] == "DATA_INVALID_NO_SEND"
    assert result["broker_states"][0]["state"] == "CLOSED_SL"
    assert result["broker_states"][0]["exit_deal_tickets"] == [501]
    assert mt5.pending_send_count == 1
    assert {call["ticket"] for call in mt5.history_deal_calls if call["ticket"]} == {12345}
    assert {call["position"] for call in mt5.history_deal_calls if call["position"]} == {700}


def test_history_deal_lookup_rejects_undocumented_order_keyword(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = PartialFillMt5(protected=True)
    client = demo_client(mt5)
    place_candidate(client, tmp_path)

    result = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["broker_states"][0]["state"] == "OPEN_PROTECTED"
    assert all("order" not in call for call in mt5.history_deal_calls)
    assert any(call["ticket"] == 12345 for call in mt5.history_deal_calls)
    assert any(call["position"] == 700 for call in mt5.history_deal_calls)
    record_if_enabled(request, evidence_token)


def test_verified_terminal_intent_is_not_reclassified_unknown_after_35_days(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    client._record_broker_state(
        tmp_path,
        "order-1",
        "CLOSED_TP",
        {
            "broker_order_ticket": 12345,
            "filled_volume": 0.1,
            "exit_volume": 0.1,
            "position_id": 700,
            "checked_at": "2026-07-29T13:05:00Z",
        },
    )
    mt5.pending = []

    result = client._reconcile_persistent_intents(
        tmp_path, pd.Timestamp("2026-09-01T13:05:00Z")
    )

    assert result == []


def test_partial_fill_is_not_full_fill_and_does_not_resend(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = PartialFillMt5()
    client = demo_client(mt5)
    place_candidate(client, tmp_path)

    result = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["state"] == "BROKER_UNSAFE_NO_SEND"
    assert result["broker_states"][0]["state"] == "PARTIAL_FILL"
    details = result["broker_states"][0]
    assert details["filled_volume"] == 0.04
    assert details["pending_remainder_volume"] == pytest.approx(0.06)
    assert mt5.pending_send_count == 1
    record_if_enabled(request, evidence_token)


def test_fill_with_unprotected_position_blocks_new_orders(tmp_path) -> None:
    mt5 = PartialFillMt5(protected=True)
    client = demo_client(mt5)
    place_candidate(client, tmp_path)

    mt5.position.sl = 0.0
    result = client.reconcile_orders(
        tmp_path,
        {"state": "DATA_INVALID"},
        pd.Timestamp("2026-07-29T13:05:00Z"),
        {"runtime_config_hash": "runtime-hash"},
    )

    assert result["state"] == "BROKER_UNSAFE_NO_SEND"
    assert result["broker_states"][0]["state"] == "PROTECTION_MISMATCH_NO_SEND"
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
    metrics = SimpleNamespace(lock=threading.Lock(), sends=0)

    class IndependentThreadMt5(FakeOrderMt5):
        def order_send(self, request):
            if request["action"] == self.TRADE_ACTION_PENDING:
                with metrics.lock:
                    metrics.sends += 1
            return super().order_send(request)

    clients = [demo_client(IndependentThreadMt5()), demo_client(IndependentThreadMt5())]
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
                lambda client: place_candidate(client, tmp_path),
                clients,
            )
        )

    assert metrics.sends == 1
    assert sum(result["state"] == "SUBMITTED" for result in results) == 1
    assert all(
        result["state"] == "SUBMITTED" or result["state"].startswith("IDEMPOTENT_")
        for result in results
    )


@pytest.mark.parametrize("seed_state", ["CHECK_RETRYABLE", "PRE_SEND_DEFERRED"])
def test_parallel_retryable_lifecycle_states_arm_and_send_once(seed_state, tmp_path, monkeypatch) -> None:
    class SeedRetryCheckMt5(FakeOrderMt5):
        def __init__(self):
            super().__init__()
            self.check_count = 0

        def order_check(self, request):
            self.check_count += 1
            if self.check_count == 1:
                return SimpleNamespace(retcode=10021, comment="No quotes")
            return super().order_check(request)

    seed_mt5 = SeedRetryCheckMt5() if seed_state == "CHECK_RETRYABLE" else FakeOrderMt5()
    seed_client = demo_client(seed_mt5)
    if seed_state == "CHECK_RETRYABLE":
        assert place_candidate(seed_client, tmp_path)["state"] == "CHECK_RETRYABLE_NO_SEND"
    else:
        stale = {
            "date": "2026-07-29",
            "recorded_at": "2026-07-29T14:25:00Z",
            "cutoffs": {"nq": "2026-07-29T10:24:00-04:00", "spx": "2026-07-29T10:25:00-04:00"},
        }
        assert seed_client._place_candidate(
            tmp_path, order_candidate(), "US100Cash", 2.0, prefix_record=stale,
            send_now=pd.Timestamp("2026-07-29T14:28:00Z"),
        )["state"] == "STALE_PREFIX_NO_SEND"

    barrier = threading.Barrier(2)
    original_state = MODULE.XmMt5DemoOrderClient._intent_state

    def synchronized_state(self, output_root, order_id):
        state = original_state(self, output_root, order_id)
        barrier.wait(timeout=5)
        return state

    monkeypatch.setattr(MODULE.XmMt5DemoOrderClient, "_intent_state", synchronized_state)
    metrics = SimpleNamespace(lock=threading.Lock(), sends=0)

    class IndependentThreadRetryMt5(FakeOrderMt5):
        def order_send(self, request):
            if request["action"] == self.TRADE_ACTION_PENDING:
                with metrics.lock:
                    metrics.sends += 1
            return super().order_send(request)

    clients = [demo_client(IndependentThreadRetryMt5()), demo_client(IndependentThreadRetryMt5())]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = []
        for item in pool.map(lambda client: place_candidate(client, tmp_path), clients):
            results.append(item)

    assert metrics.sends == 1
    assert sum(result["state"] == "SUBMITTED" for result in results) == 1
    assert all(result["state"] == "SUBMITTED" or result["state"].startswith("IDEMPOTENT_") for result in results)


def test_pre_send_deferred_race_is_idempotent_and_emits_one_reevaluation(tmp_path, monkeypatch) -> None:
    seed_client = demo_client(FakeOrderMt5())
    stale = {
        "date": "2026-07-29",
        "recorded_at": "2026-07-29T14:25:00Z",
        "cutoffs": {"nq": "2026-07-29T10:24:00-04:00", "spx": "2026-07-29T10:25:00-04:00"},
    }
    assert seed_client._place_candidate(
        tmp_path, order_candidate(), "US100Cash", 2.0, prefix_record=stale,
        send_now=pd.Timestamp("2026-07-29T14:28:00Z"),
    )["state"] == "STALE_PREFIX_NO_SEND"

    metrics = SimpleNamespace(lock=threading.Lock(), sends=0)

    class IndependentThreadPreSendMt5(FakeOrderMt5):
        def order_send(self, request):
            if request["action"] == self.TRADE_ACTION_PENDING:
                with metrics.lock:
                    metrics.sends += 1
            return super().order_send(request)

    barrier = threading.Barrier(2)
    original_state = MODULE.XmMt5DemoOrderClient._intent_state

    def synchronized_state(self, output_root, order_id):
        state = original_state(self, output_root, order_id)
        if order_id == "order-1" and state and state["status"] == "PRE_SEND_DEFERRED":
            barrier.wait(timeout=15)
        return state

    monkeypatch.setattr(MODULE.XmMt5DemoOrderClient, "_intent_state", synchronized_state)
    clients = [demo_client(IndependentThreadPreSendMt5()), demo_client(IndependentThreadPreSendMt5())]
    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(place_candidate, client, tmp_path) for client in clients]
        for future in futures:
            try:
                outcomes.append({"result": future.result(timeout=15)})
            except Exception as exc:
                outcomes.append({"exception": f"{type(exc).__name__}: {exc}"})

    assert sum("exception" in outcome for outcome in outcomes) == 0
    assert metrics.sends == 1
    results = [outcome["result"] for outcome in outcomes]
    assert sum(result["state"] == "SUBMITTED" for result in results) == 1
    assert sum(result["state"].startswith("IDEMPOTENT_") for result in results) == 1
    events = seed_client._events(tmp_path)
    assert sum(item["event"] == "SEND_ARMED" for item in events) == 1
    assert sum(item["event"] == "INTENT_REEVALUATED" for item in events) == 1


def test_pre_send_deferred_race_is_process_safe_with_independent_mt5_clients(tmp_path) -> None:
    seed_client = demo_client(FakeOrderMt5())
    stale = {
        "date": "2026-07-29",
        "recorded_at": "2026-07-29T14:25:00Z",
        "cutoffs": {"nq": "2026-07-29T10:24:00-04:00", "spx": "2026-07-29T10:25:00-04:00"},
    }
    assert seed_client._place_candidate(
        tmp_path, order_candidate(), "US100Cash", 2.0, prefix_record=stale,
        send_now=pd.Timestamp("2026-07-29T14:28:00Z"),
    )["state"] == "STALE_PREFIX_NO_SEND"
    counter_path = tmp_path / "send-counter.sqlite"
    connection = sqlite3.connect(counter_path)
    try:
        connection.execute("CREATE TABLE counters (sends INTEGER NOT NULL)")
        connection.execute("INSERT INTO counters(sends) VALUES (0)")
        connection.commit()
    finally:
        connection.close()

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    ready = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=resume_pre_send_in_process,
            args=(str(tmp_path), start, ready, str(counter_path), results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=5) for _ in processes]
    assert sum("exception" in outcome for outcome in outcomes) == 0
    connection = sqlite3.connect(counter_path)
    try:
        assert connection.execute("SELECT sends FROM counters").fetchone()[0] == 1
    finally:
        connection.close()
    result_states = [outcome["result"]["state"] for outcome in outcomes]
    assert result_states.count("SUBMITTED") == 1
    assert sum(state.startswith("IDEMPOTENT_") for state in result_states) == 1
    events = seed_client._events(tmp_path)
    assert sum(item["event"] == "SEND_ARMED" for item in events) == 1
    assert sum(item["event"] == "INTENT_REEVALUATED" for item in events) == 1


def test_pre_send_deferred_request_mismatch_remains_fail_closed(tmp_path) -> None:
    client = demo_client(FakeOrderMt5())
    stale = {
        "date": "2026-07-29",
        "recorded_at": "2026-07-29T14:25:00Z",
        "cutoffs": {"nq": "2026-07-29T10:24:00-04:00", "spx": "2026-07-29T10:25:00-04:00"},
    }
    assert client._place_candidate(
        tmp_path, order_candidate(), "US100Cash", 2.0, prefix_record=stale,
        send_now=pd.Timestamp("2026-07-29T14:28:00Z"),
    )["state"] == "STALE_PREFIX_NO_SEND"
    request = client._pending_request(
        "US100Cash", "long", 97.5, 90.0, 120.0, client._comment("nq", "order-1")
    )
    connection = sqlite3.connect(client._order_db(tmp_path))
    try:
        connection.execute(
            "UPDATE order_intents SET request_json = ? WHERE order_id = ?",
            (MODULE.core.canonical_json(request), "order-1"),
        )
        connection.commit()
    finally:
        connection.close()
    mismatch = dict(request)
    mismatch["price"] = float(mismatch["price"]) + 1.0

    with pytest.raises(MODULE.core.CriticalLiveError, match="reevaluated request differs"):
        client._resume_pre_send_intent(tmp_path, "order-1", mismatch, {"event": "INTENT_REEVALUATED"})

    assert client._intent_state(tmp_path, "order-1")["status"] == "PRE_SEND_DEFERRED"
    assert client.mt5.pending_send_count == 0
    assert [item["event"] for item in client._events(tmp_path)] == ["PRE_SEND_DEFERRED"]


def test_pre_send_deferred_crash_after_cas_resumes_after_restart(tmp_path) -> None:
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    stale = {
        "date": "2026-07-29",
        "recorded_at": "2026-07-29T14:25:00Z",
        "cutoffs": {"nq": "2026-07-29T10:24:00-04:00", "spx": "2026-07-29T10:25:00-04:00"},
    }
    assert client._place_candidate(
        tmp_path, order_candidate(), "US100Cash", 2.0, prefix_record=stale,
        send_now=pd.Timestamp("2026-07-29T14:28:00Z"),
    )["state"] == "STALE_PREFIX_NO_SEND"
    original_check = mt5.order_check
    mt5.order_check = lambda request: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        place_candidate(client, tmp_path)
    mt5.order_check = original_check

    assert client._intent_state(tmp_path, "order-1")["status"] == "INTENT"
    assert mt5.pending_send_count == 0
    restarted = demo_client(FakeOrderMt5())
    result = place_candidate(restarted, tmp_path)
    assert result["state"] == "SUBMITTED"
    assert restarted.mt5.pending_send_count == 1
    assert client._intent_state(tmp_path, "order-1")["status"] == "SUBMITTED"


def test_sqlite_initializer_is_idempotent_and_verifies_wal_schema(tmp_path) -> None:
    client = demo_client(FakeOrderMt5())

    first = client._initialize_order_db(tmp_path)
    second = client._initialize_order_db(tmp_path)

    assert first == {"state": "READY", "journal_mode": "wal", "wrote": True}
    assert second == {"state": "READY", "journal_mode": "wal", "wrote": False}
    connection = sqlite3.connect(client._order_db(tmp_path))
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        for table, columns in MODULE.ORDER_SCHEMA.items():
            actual = {
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            assert set(columns).issubset(actual)
    finally:
        connection.close()


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
    place_candidate(client, tmp_path)

    result = client.cancel_all_pending(tmp_path, "TEST")
    assert result[0]["state"] == "CANCEL_UNKNOWN"
    assert client._intent_state(tmp_path, "order-1")["status"] == "CANCEL_UNKNOWN"
    assert mt5.pending


def test_pending_remove_keeps_order_specific_partial_exposure_without_fatal(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = PartialFillMt5(protected=False)
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    mt5.position = SimpleNamespace(
        ticket=700,
        identifier=700,
        magic=client.magic,
        comment=client._comment("nq", "order-1"),
        symbol="US100Cash",
        type=0,
        volume=0.04,
        sl=mt5.history_order.sl,
        tp=mt5.history_order.tp,
    )
    mt5.pending = [SimpleNamespace(**vars(mt5.history_order))]

    cancelled = client.cancel_all_pending(tmp_path, "DATA_INVALID")

    assert cancelled[0]["state"] == "PARTIAL_FILL"
    assert cancelled[0]["protection_state"] == "PROTECTED"
    assert cancelled[0]["cancelled_remainder_volume"] == pytest.approx(0.06)
    assert mt5.pending == []
    assert client._intent_state(tmp_path, "order-1")["status"] == "LINKED_EXISTING"
    record_if_enabled(request, evidence_token)


def test_pending_remove_does_not_bind_unrelated_same_magic_position(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = FakeOrderMt5()
    client = demo_client(mt5)
    mt5.pending = [
        SimpleNamespace(ticket=12345, magic=client.magic, comment="FSP:other:unrelated")
    ]
    mt5.positions_get = lambda ticket=None: (
        ()
        if ticket is not None
        else (SimpleNamespace(ticket=900, magic=client.magic),)
    )

    result = client.cancel_all_pending(tmp_path, "STALE_PREFIX")

    assert result == [
        {"ticket": 12345, "state": "CANCELLED", "reason": "STALE_PREFIX"}
    ]
    assert mt5.pending == []
    record_if_enabled(request, evidence_token)


def test_pending_remove_keeps_unknown_when_order_fill_readback_disappears(tmp_path) -> None:
    class LostReadbackMt5(FakeOrderMt5):
        def __init__(self):
            super().__init__()
            self.lost = False

        def order_send(self, request):
            result = super().order_send(request)
            if request["action"] == self.TRADE_ACTION_REMOVE:
                self.lost = True
            return result

        def history_deals_get(self, start=None, end=None, *, ticket=None, position=None):
            if self.lost and ticket is not None:
                return None
            return super().history_deals_get(start, end, ticket=ticket, position=position)

    mt5 = LostReadbackMt5()
    client = demo_client(mt5)
    mt5.pending = [
        SimpleNamespace(ticket=12345, magic=client.magic, comment="FSP:other:unrelated")
    ]

    result = client.cancel_all_pending(tmp_path, "TECHNICAL_RECOVERY")
    assert result[0]["state"] == "CANCEL_UNKNOWN"
    assert client._intent_state(tmp_path, "broker-cancel-12345")["status"] == "CANCEL_UNKNOWN"


def test_pending_remove_blocks_unprotected_order_specific_position(tmp_path, request) -> None:
    evidence_token = checkpoint_if_enabled(request)
    mt5 = PartialFillMt5(protected=False)
    client = demo_client(mt5)
    place_candidate(client, tmp_path)
    mt5.position = SimpleNamespace(
        ticket=700,
        identifier=700,
        magic=client.magic,
        comment=client._comment("nq", "order-1"),
        symbol="US100Cash",
        type=0,
        volume=0.04,
        sl=0.0,
        tp=mt5.history_order.tp,
    )
    mt5.pending = [SimpleNamespace(**vars(mt5.history_order))]

    cancelled = client.cancel_all_pending(tmp_path, "DATA_INVALID")
    assert cancelled[0]["state"] == "CANCEL_UNKNOWN"
    assert cancelled[0]["broker_execution_state"] == "PROTECTION_MISMATCH_NO_SEND"
    assert mt5.pending == []
    record_if_enabled(request, evidence_token)


def test_sqlite_unique_intent_is_process_safe(tmp_path) -> None:
    client = demo_client(FakeOrderMt5())
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=claim_intent_in_process,
            args=(str(tmp_path), start, results),
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
