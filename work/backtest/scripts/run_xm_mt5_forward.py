from __future__ import annotations

import json
from hashlib import sha256
import math
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_capital_forward as core


RUNTIME_CONFIG = ROOT / "live_forward" / "xm_mt5_demo_config.json"
FORWARD_SHADOW_ADAPTER = ROOT / "scripts" / "run_forward_shadow.py"
REQUIRED_ENV = ("XM_MT5_SERVER",)


class XmMt5Error(core.TransientLiveError):
    pass


class BrokerStateUnknownError(XmMt5Error):
    pass


class UnsafeOpenOrdersError(core.CriticalLiveError):
    pass


class CandidateNotExecutableError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CandidateRetryableError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class XmMt5ReadOnlyClient:
    """Read-only market-data adapter. This class intentionally has no order method."""

    def __init__(self, config: dict[str, Any], secrets: dict[str, str]):
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise XmMt5Error("MetaTrader5 Python package is not installed.") from exc
        self.mt5 = mt5
        self.login_id = int(config["account_login"])
        self.server = secrets["XM_MT5_SERVER"]
        self.password = (
            secrets.get("XM_MT5_READ_ONLY_PASSWORD", "").strip()
            or os.environ.get("XM_MT5_READ_ONLY_PASSWORD", "").strip()
        )
        self.terminal_path = (
            os.environ.get("XM_MT5_TERMINAL_PATH", "").strip()
            or str(config.get("terminal_path") or "").strip()
        )
        self.portable = bool(config.get("portable", False))
        self.closed_bar_delay_seconds = int(config.get("closed_bar_delay_seconds", 0))
        self.connected = False

    def _safe_last_error(self) -> str:
        detail = str(self.mt5.last_error())
        if self.password:
            detail = detail.replace(self.password, "***")
        return detail

    def _initialize_terminal_only(self) -> bool:
        kwargs: dict[str, Any] = {"timeout": 60_000}
        if getattr(self, "portable", False):
            kwargs["portable"] = True
        return bool(
            self.mt5.initialize(self.terminal_path, **kwargs)
            if self.terminal_path
            else self.mt5.initialize(**kwargs)
        )

    def login(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "login": self.login_id,
            "server": self.server,
            "timeout": 60_000,
        }
        if self.password:
            kwargs["password"] = self.password
        if getattr(self, "portable", False):
            kwargs["portable"] = True
        ok = (
            self.mt5.initialize(self.terminal_path, **kwargs)
            if self.terminal_path
            else self.mt5.initialize(**kwargs)
        )
        if not ok:
            primary_error = self._safe_last_error()
            self.mt5.shutdown()
            if not self._initialize_terminal_only():
                fallback_error = self._safe_last_error()
                self.mt5.shutdown()
                raise XmMt5Error(
                    "MT5 initialize failed; "
                    f"primary={primary_error}; terminal_only={fallback_error}"
                )
            login_kwargs: dict[str, Any] = {
                "server": self.server,
                "timeout": 60_000,
            }
            if self.password:
                login_kwargs["password"] = self.password
            if not self.mt5.login(self.login_id, **login_kwargs):
                login_error = self._safe_last_error()
                self.mt5.shutdown()
                raise XmMt5Error(f"MT5 explicit login failed: {login_error}")
        account = self.mt5.account_info()
        if account is None or int(account.login) != self.login_id:
            self.mt5.shutdown()
            raise XmMt5Error("MT5 connected to an unexpected account.")
        if str(account.server) != self.server:
            self.mt5.shutdown()
            raise XmMt5Error("MT5 connected to an unexpected server.")
        self.connected = True
        return {
            "login": int(account.login),
            "server": str(account.server),
            "trade_mode": int(account.trade_mode),
        }

    def _ensure_connected(self) -> None:
        if not self.connected:
            self.login()

    def prices(
        self,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> tuple[pd.Timestamp, list[dict[str, Any]]]:
        self._ensure_connected()
        rates = self.mt5.copy_rates_range(
            symbol,
            self.mt5.TIMEFRAME_M1,
            start.tz_convert("UTC").to_pydatetime(),
            end.tz_convert("UTC").to_pydatetime(),
        )
        known_time = core.utc_now()
        if rates is None:
            raise XmMt5Error(f"{symbol}: copy_rates_range failed: {self.mt5.last_error()}")
        prices: list[dict[str, Any]] = []
        safe_time = known_time - pd.Timedelta(
            seconds=max(0, int(getattr(self, "closed_bar_delay_seconds", 0)))
        )
        closed_rates: dict[pd.Timestamp, Any] = {}
        verified_no_tick_times: set[pd.Timestamp] = set()
        for row in rates:
            bar_time = pd.Timestamp(int(row["time"]), unit="s", tz="UTC")
            if bar_time + pd.Timedelta(minutes=1) > safe_time:
                continue
            closed_rates[bar_time] = row
        if closed_rates:
            first = start.tz_convert("UTC").ceil("1min")
            requested_last = end.tz_convert("UTC").floor("1min")
            safe_last = safe_time.tz_convert("UTC").floor("1min") - pd.Timedelta(minutes=1)
            last = min(requested_last, safe_last)
            expected = pd.date_range(first, last, freq="1min") if last >= first else []
            missing = [
                timestamp
                for timestamp in expected
                if timestamp not in closed_rates
                and not core.scheduled_closed_minute(timestamp.tz_convert(core.TZ), core.runtime_config())
            ]
            groups: list[list[pd.Timestamp]] = []
            for timestamp in missing:
                if not groups or timestamp - groups[-1][-1] != pd.Timedelta(minutes=1):
                    groups.append([timestamp])
                else:
                    groups[-1].append(timestamp)
            for group in groups:
                previous_times = [timestamp for timestamp in closed_rates if timestamp < group[0]]
                previous_time = max(previous_times) if previous_times else None
                fill_price = None
                if (
                    previous_time is not None
                    and group[0] - previous_time == pd.Timedelta(minutes=1)
                ):
                    fill_price = float(closed_rates[previous_time]["close"])
                else:
                    next_time = group[-1] + pd.Timedelta(minutes=1)
                    follows_scheduled_close = core.scheduled_closed_minute(
                        (group[0] - pd.Timedelta(minutes=1)).tz_convert(core.TZ),
                        core.runtime_config(),
                    )
                    if next_time in closed_rates and follows_scheduled_close:
                        fill_price = float(closed_rates[next_time]["open"])
                if fill_price is None:
                    continue
                ticks = self.mt5.copy_ticks_range(
                    symbol,
                    group[0].to_pydatetime(),
                    (
                        group[-1]
                        + pd.Timedelta(minutes=1)
                        - pd.Timedelta(milliseconds=1)
                    ).to_pydatetime(),
                    self.mt5.COPY_TICKS_ALL,
                )
                if ticks is None:
                    raise XmMt5Error(f"{symbol}: copy_ticks_range failed: {self.mt5.last_error()}")
                if len(ticks) != 0:
                    continue
                for timestamp in group:
                    closed_rates[timestamp] = {
                        "time": int(timestamp.timestamp()),
                        "open": fill_price,
                        "high": fill_price,
                        "low": fill_price,
                        "close": fill_price,
                        "tick_volume": 0,
                        "real_volume": 0,
                    }
                    verified_no_tick_times.add(timestamp)
        for bar_time, row in sorted(closed_rates.items()):
            volume = float(row["real_volume"] or row["tick_volume"])
            payload = {
                "snapshotTimeUTC": bar_time.isoformat(),
                "openPrice": {"bid": float(row["open"])},
                "highPrice": {"bid": float(row["high"])},
                "lowPrice": {"bid": float(row["low"])},
                "closePrice": {"bid": float(row["close"])},
                "lastTradedVolume": volume,
            }
            if bar_time in verified_no_tick_times:
                payload["verifiedNoTick"] = True
            prices.append(payload)
        return known_time, prices

    def market(self, symbol: str) -> dict[str, Any]:
        self._ensure_connected()
        if not self.mt5.symbol_select(symbol, True):
            raise XmMt5Error(f"{symbol}: symbol_select failed: {self.mt5.last_error()}")
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise XmMt5Error(f"{symbol}: symbol_info failed: {self.mt5.last_error()}")
        upper = f"{info.name} {info.description}".upper()
        if "US100" in symbol.upper() and not any(name in upper for name in ("NASDAQ", "US100")):
            raise core.CriticalLiveError(f"{symbol}: not an NQ/NASDAQ selector.")
        if "US500" in symbol.upper() and not any(name in upper for name in ("S&P", "SP500", "US500")):
            raise core.CriticalLiveError(f"{symbol}: not an SPX/S&P selector.")
        expected_name = "NASDAQ" if "US100" in symbol.upper() else "S&P"
        return {
            "instrument": {
                "name": f"{expected_name} {info.description}",
                "symbol": str(info.name),
                "type": "INDICES",
                "streamingPricesAvailable": bool(info.visible),
            }
        }

    def close(self) -> None:
        if self.connected:
            self.mt5.shutdown()
            self.connected = False


class XmMt5DemoOrderClient(XmMt5ReadOnlyClient):
    def __init__(self, config: dict[str, Any], secrets: dict[str, str]):
        super().__init__(config, secrets)
        self.config = config
        self.magic = int(config["magic_number"])
        self.demo_verified = False
        self.account = None
        self.terminal = None

    def _refresh_demo_identity(self) -> None:
        account = self.mt5.account_info()
        terminal = self.mt5.terminal_info()
        if account is None or terminal is None:
            self.demo_verified = False
            self.close()
            raise BrokerStateUnknownError(
                "MT5 account or terminal identity is unavailable; broker state is unknown."
            )
        demo_mode = int(getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
        expected_server = str(self.config["expected_server"])
        expected_company = str(self.config["expected_company"])
        if (
            int(getattr(account, "trade_mode", -1)) != demo_mode
            or str(getattr(account, "server", "")) != expected_server
            or str(getattr(account, "company", "")) != expected_company
            or int(getattr(account, "login", -1)) != self.login_id
        ):
            self.demo_verified = False
            self.close()
            raise core.CriticalLiveError("MT5 account failed the fixed XM demo identity gate.")
        self.account = account
        self.terminal = terminal
        self.demo_verified = True

    def login(self) -> dict[str, Any]:
        result = super().login()
        self._refresh_demo_identity()
        result.update(
            {
                "company": str(self.account.company),
                "demo_verified": True,
                "terminal_connected": bool(self.terminal.connected),
            }
        )
        return result

    def _ensure_demo(self) -> None:
        if not self.connected:
            self.login()
        else:
            self._refresh_demo_identity()

    def order_permission_status(self) -> dict[str, object]:
        self._ensure_demo()
        checks = {
            "demo_verified": self.demo_verified,
            "account_trade_allowed": bool(getattr(self.account, "trade_allowed", False)),
            "account_trade_expert": bool(getattr(self.account, "trade_expert", False)),
            "terminal_connected": bool(getattr(self.terminal, "connected", False)),
            "terminal_trade_allowed": bool(getattr(self.terminal, "trade_allowed", False)),
            "terminal_tradeapi_disabled": bool(getattr(self.terminal, "tradeapi_disabled", True)),
        }
        ready = all(
            (
                checks["demo_verified"],
                checks["account_trade_allowed"],
                checks["account_trade_expert"],
                checks["terminal_connected"],
                checks["terminal_trade_allowed"],
                not checks["terminal_tradeapi_disabled"],
            )
        )
        return {"state": "READY" if ready else "ORDER_PERMISSION_DISABLED", "checks": checks}

    def _require_order_permission(self) -> None:
        status = self.order_permission_status()
        if status["state"] != "READY":
            raise core.CriticalLiveError(f"MT5 demo order permission gate failed: {status['checks']}")

    def _smoke_exposure(self) -> dict[str, int]:
        """Return the dedicated demo account exposure gate, failing closed on unknown state."""
        orders = self._mt5_collection("orders_get")
        positions = self._mt5_collection("positions_get")
        return {
            "open_orders": len(orders),
            "open_positions": len(positions),
            "unknown_exposure": 0,
        }

    def _comment(self, leg_key: str, order_id: str) -> str:
        digest = sha256(order_id.encode("utf-8")).hexdigest()[:16]
        return f"{self.config['order_comment_prefix']}:{leg_key}:{digest}"[:31]

    @staticmethod
    def _order_log(output_root: Path) -> Path:
        return output_root / "orders" / "events.jsonl"

    @staticmethod
    def _order_db(output_root: Path) -> Path:
        return output_root / "orders" / "idempotency.sqlite3"

    def _mt5_collection(self, operation: str, *args: object, **kwargs: object) -> tuple[Any, ...]:
        self._ensure_demo()
        result = getattr(self.mt5, operation)(*args, **kwargs)
        if result is None:
            raise BrokerStateUnknownError(
                f"MT5 {operation} failed; broker state is unknown and order transmission is blocked: "
                f"{self.mt5.last_error()}"
            )
        return tuple(result)

    def _order_send_checked(
        self,
        request: dict[str, object],
        *,
        require_open_permission: bool = True,
    ) -> Any:
        if require_open_permission:
            self._require_order_permission()
        else:
            self._ensure_demo()
        return self.mt5.order_send(request)

    def _order_connection(self, output_root: Path) -> sqlite3.Connection:
        path = self._order_db(output_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_intents (
                order_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                comment TEXT NOT NULL,
                request_json TEXT,
                broker_ticket INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_event_outbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                event_json TEXT NOT NULL,
                delivered_at TEXT
            )
            """
        )
        return connection

    def _drain_order_outbox(self, output_root: Path) -> None:
        connection = self._order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT sequence, event_json FROM order_event_outbox "
                "WHERE delivered_at IS NULL ORDER BY sequence"
            ).fetchall()
            for sequence, event_json in rows:
                core.append_jsonl(self._order_log(output_root), json.loads(str(event_json)))
                connection.execute(
                    "UPDATE order_event_outbox SET delivered_at = ? WHERE sequence = ?",
                    (core.utc_now().isoformat(), int(sequence)),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _intent_state(self, output_root: Path, order_id: str) -> dict[str, Any] | None:
        connection = self._order_connection(output_root)
        try:
            row = connection.execute(
                "SELECT status, comment, broker_ticket FROM order_intents WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {"status": str(row[0]), "comment": str(row[1]), "broker_ticket": row[2]}

    def _claim_order_intent(
        self,
        output_root: Path,
        order_id: str,
        comment: str,
        request: dict[str, object],
        event: dict[str, object],
    ) -> dict[str, Any]:
        now = core.utc_now().isoformat()
        connection = self._order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                "INSERT OR IGNORE INTO order_intents "
                "(order_id, status, comment, request_json, broker_ticket, created_at, updated_at) "
                "VALUES (?, 'INTENT', ?, ?, NULL, ?, ?)",
                (order_id, comment, core.canonical_json(request), now, now),
            ).rowcount
            if inserted:
                payload = {"recorded_at": now, "magic": self.magic, **event}
                connection.execute(
                    "INSERT INTO order_event_outbox (order_id, event_json) VALUES (?, ?)",
                    (order_id, core.canonical_json(payload)),
                )
            row = connection.execute(
                "SELECT status, broker_ticket FROM order_intents WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if inserted:
            self._drain_order_outbox(output_root)
        return {
            "claimed": bool(inserted),
            "status": str(row[0]),
            "broker_ticket": row[1],
        }

    def _transition_order_intent(
        self,
        output_root: Path,
        order_id: str,
        status: str,
        event: dict[str, object],
        broker_ticket: int | None = None,
    ) -> None:
        now = core.utc_now().isoformat()
        connection = self._order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE order_intents SET status = ?, broker_ticket = COALESCE(?, broker_ticket), "
                "updated_at = ? WHERE order_id = ?",
                (status, broker_ticket, now, order_id),
            ).rowcount
            if updated != 1:
                raise core.CriticalLiveError(f"{order_id}: idempotency intent is missing.")
            payload = {"recorded_at": now, "magic": self.magic, **event}
            connection.execute(
                "INSERT INTO order_event_outbox (order_id, event_json) VALUES (?, ?)",
                (order_id, core.canonical_json(payload)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)

    def _adopt_order_intent(
        self,
        output_root: Path,
        order_id: str,
        comment: str,
        status: str,
        event: dict[str, object],
        broker_ticket: int | None = None,
    ) -> None:
        now = core.utc_now().isoformat()
        connection = self._order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO order_intents "
                "(order_id, status, comment, request_json, broker_ticket, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?)",
                (order_id, status, comment, broker_ticket, now, now),
            )
            connection.execute(
                "UPDATE order_intents SET status = ?, broker_ticket = COALESCE(?, broker_ticket), "
                "updated_at = ? WHERE order_id = ?",
                (status, broker_ticket, now, order_id),
            )
            payload = {"recorded_at": now, "magic": self.magic, **event}
            connection.execute(
                "INSERT INTO order_event_outbox (order_id, event_json) VALUES (?, ?)",
                (order_id, core.canonical_json(payload)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)

    def _append_order_event(self, output_root: Path, event: dict[str, object]) -> None:
        core.append_jsonl(
            self._order_log(output_root),
            {"recorded_at": core.utc_now().isoformat(), "magic": self.magic, **event},
        )

    def _events(self, output_root: Path) -> list[dict[str, Any]]:
        path = self._order_log(output_root)
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _events_for_trade_date(events: list[dict[str, Any]], trade_date: str) -> list[dict[str, Any]]:
        selected = []
        for event in events:
            timestamp = event.get("recorded_at")
            if not timestamp:
                continue
            if pd.Timestamp(timestamp).tz_convert(core.TZ).strftime("%Y-%m-%d") == trade_date:
                selected.append(event)
        return selected

    def _sync_broker_events(self, output_root: Path, now: pd.Timestamp) -> int:
        existing = {
            int(item["broker_ticket"])
            for item in self._events(output_root)
            if item.get("event") == "DEAL" and item.get("broker_ticket") is not None
        }
        start = (now - pd.Timedelta(days=35)).to_pydatetime()
        end = (now + pd.Timedelta(minutes=1)).to_pydatetime()
        inserted = 0
        for deal in self._mt5_collection("history_deals_get", start, end):
            ticket = int(deal.ticket)
            if int(getattr(deal, "magic", -1)) != self.magic or ticket in existing:
                continue
            self._append_order_event(
                output_root,
                {
                    "event": "DEAL",
                    "broker_ticket": ticket,
                    "order_ticket": int(deal.order),
                    "position_ticket": int(deal.position_id),
                    "symbol": str(deal.symbol),
                    "entry": int(deal.entry),
                    "reason": int(deal.reason),
                    "price": float(deal.price),
                    "volume": float(deal.volume),
                    "comment": str(deal.comment),
                    "known_time": pd.Timestamp(int(deal.time_msc), unit="ms", tz="UTC").isoformat(),
                },
            )
            inserted += 1
        return inserted

    def _broker_objects(self, comment: str) -> list[tuple[str, Any]]:
        self._ensure_demo()
        found: list[tuple[str, Any]] = []
        for kind, operation in (("ORDER", "orders_get"), ("POSITION", "positions_get")):
            for item in self._mt5_collection(operation):
                if int(getattr(item, "magic", -1)) == self.magic and str(getattr(item, "comment", "")) == comment:
                    found.append((kind, item))
        start = (core.utc_now() - pd.Timedelta(days=35)).to_pydatetime()
        end = (core.utc_now() + pd.Timedelta(minutes=1)).to_pydatetime()
        for item in self._mt5_collection("history_orders_get", start, end):
            if int(getattr(item, "magic", -1)) == self.magic and str(getattr(item, "comment", "")) == comment:
                found.append(("HISTORY_ORDER", item))
        return found

    def _minimum_volume(self, symbol: str) -> float:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise core.CriticalLiveError(f"{symbol}: symbol_info unavailable for order sizing.")
        step = float(info.volume_step)
        minimum = float(info.volume_min)
        steps = math.ceil((minimum - 1e-12) / step)
        return round(steps * step, 8)

    @staticmethod
    def _derived_entry(decision: dict[str, Any], reward_r: float) -> float:
        stop = float(decision["stop_price"])
        target = float(decision["target_price"])
        return (target + reward_r * stop) / (1.0 + reward_r)

    def _trade_window_expiration(self, symbol: str) -> object:
        leg_keys = [
            key
            for key in core.LEG_ORDER
            if str(self.config["legs"][key]["epic"]) == symbol
        ]
        if len(leg_keys) != 1:
            raise core.CriticalLiveError(f"{symbol}: trade-window expiration cannot be resolved.")
        configs, _, _ = core.live_strategy_objects()
        local_now = core.utc_now().tz_convert(core.TZ)
        expiration = pd.Timestamp(
            f"{local_now.date()} {configs[leg_keys[0]].trade_window_end}",
            tz=core.TZ,
        )
        # MetaTrader5's Windows Python binding requires ORDER_TIME_SPECIFIED
        # expiration as Unix seconds; datetime objects are rejected before the
        # request reaches the broker (last_error=-2, invalid expiration).
        return int(expiration.tz_convert("UTC").timestamp())

    def _pending_request(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop: float,
        target: float,
        comment: str,
    ) -> dict[str, object]:
        self._require_order_permission()
        if not self.mt5.symbol_select(symbol, True):
            raise core.CriticalLiveError(f"{symbol}: symbol_select failed before order.")
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            raise core.CriticalLiveError(f"{symbol}: live symbol/tick unavailable before order.")
        digits = int(info.digits)
        trade_mode = getattr(info, "trade_mode", None)
        full_trade_mode = getattr(self.mt5, "SYMBOL_TRADE_MODE_FULL", None)
        if trade_mode is not None and full_trade_mode is not None and int(trade_mode) != int(full_trade_mode):
            raise core.CriticalLiveError(f"{symbol}: broker symbol is not in full trade mode.")
        order_mode = getattr(info, "order_mode", None)
        limit_flag = getattr(self.mt5, "SYMBOL_ORDER_LIMIT", None)
        if order_mode is not None and limit_flag is not None and not (int(order_mode) & int(limit_flag)):
            raise core.CriticalLiveError(f"{symbol}: broker symbol does not allow limit orders.")
        point = float(getattr(info, "point", 0.0) or 10 ** (-digits))
        trade_tick_size = float(getattr(info, "trade_tick_size", 0.0) or point)
        if trade_tick_size <= 0:
            raise core.CriticalLiveError(f"{symbol}: invalid broker trade tick size.")

        def aligned_price(value: float) -> float:
            return round(round(float(value) / trade_tick_size) * trade_tick_size, digits)

        entry = aligned_price(entry)
        stop = aligned_price(stop)
        target = aligned_price(target)
        stop_distance = float(getattr(info, "trade_stops_level", 0) or 0) * point
        if direction == "long":
            if not stop < entry < target:
                raise core.CriticalLiveError(f"{symbol}: illegal long pending-order price geometry.")
            if not entry < float(tick.ask):
                raise CandidateNotExecutableError(
                    "STALE_ENTRY_CROSSED",
                    f"{symbol}: long limit entry is no longer below the live ask.",
                )
            if float(tick.ask) - entry < stop_distance:
                raise CandidateRetryableError(
                    "ENTRY_DISTANCE_WAIT",
                    f"{symbol}: long limit entry is temporarily inside the broker distance.",
                )
            if entry - stop < stop_distance or target - entry < stop_distance:
                raise CandidateNotExecutableError(
                    "BROKER_SL_TP_DISTANCE",
                    f"{symbol}: long stop/target violates broker distance rules.",
                )
            order_type = self.mt5.ORDER_TYPE_BUY_LIMIT
        elif direction == "short":
            if not target < entry < stop:
                raise core.CriticalLiveError(f"{symbol}: illegal short pending-order price geometry.")
            if not entry > float(tick.bid):
                raise CandidateNotExecutableError(
                    "STALE_ENTRY_CROSSED",
                    f"{symbol}: short limit entry is no longer above the live bid.",
                )
            if entry - float(tick.bid) < stop_distance:
                raise CandidateRetryableError(
                    "ENTRY_DISTANCE_WAIT",
                    f"{symbol}: short limit entry is temporarily inside the broker distance.",
                )
            if stop - entry < stop_distance or entry - target < stop_distance:
                raise CandidateNotExecutableError(
                    "BROKER_SL_TP_DISTANCE",
                    f"{symbol}: short stop/target violates broker distance rules.",
                )
            order_type = self.mt5.ORDER_TYPE_SELL_LIMIT
        else:
            raise core.CriticalLiveError(f"{symbol}: illegal order direction {direction}.")
        return {
            "action": self.mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": self._minimum_volume(symbol),
            "type": order_type,
            "price": entry,
            "sl": stop,
            "tp": target,
            "deviation": int(self.config["deviation_points"]),
            "magic": self.magic,
            "comment": comment,
            "type_time": int(getattr(self.mt5, "ORDER_TIME_SPECIFIED", 2)),
            "expiration": self._trade_window_expiration(symbol),
            "type_filling": self.mt5.ORDER_FILLING_RETURN,
        }

    def _preflight_risk_scales(self) -> tuple[float | None, ...]:
        return (None,)

    def preflight_order_transport(self, output_root: Path, now: pd.Timestamp) -> dict[str, object]:
        local = now.tz_convert(core.TZ)
        if local.weekday() >= 5:
            return {"state": "NON_TRADING_DAY", "reason": "WEEKEND"}
        marker = output_root / "preflight" / f"{local.date()}.json"
        if marker.exists():
            return {"state": "ALREADY_PASSED", "path": str(marker)}
        if local.strftime("%H:%M") < "09:30":
            return {"state": "WAITING_PREFLIGHT_WINDOW", "opens_at": "09:30"}

        configs, _, _ = core.live_strategy_objects()
        checks = []
        for leg_key in core.LEG_ORDER:
            symbol = str(self.config["legs"][leg_key]["epic"])
            if not self.mt5.symbol_select(symbol, True):
                raise core.CriticalLiveError(f"{symbol}: symbol_select failed for order preflight.")
            info = self.mt5.symbol_info(symbol)
            tick = self.mt5.symbol_info_tick(symbol)
            if info is None or tick is None:
                raise core.CriticalLiveError(f"{symbol}: live symbol/tick unavailable for order preflight.")
            point = float(getattr(info, "point", 0.0) or 10 ** (-int(info.digits)))
            tick_size = float(getattr(info, "trade_tick_size", 0.0) or point)
            stops = float(getattr(info, "trade_stops_level", 0) or 0) * point
            spread = max(0.0, float(tick.ask) - float(tick.bid))
            distance = max(stops + 2 * tick_size, 4 * spread, 10 * tick_size)
            reward = float(configs[leg_key].reward_r)
            for scale in self._preflight_risk_scales():
                self._active_risk_scale = scale
                try:
                    for direction in ("long", "short"):
                        if direction == "long":
                            entry = float(tick.ask) - distance
                            stop = entry - distance
                            target = entry + reward * distance
                        else:
                            entry = float(tick.bid) + distance
                            stop = entry + distance
                            target = entry - reward * distance
                        try:
                            request = self._pending_request(
                                symbol,
                                direction,
                                entry,
                                stop,
                                target,
                                f"{self.config['order_comment_prefix']}:CHECK"[:31],
                            )
                            # The strategy expiration may already be in the past when a
                            # post-session readiness check runs. Only the non-sending
                            # order_check probe gets a short future expiration; live
                            # order requests retain the fixed trade-window expiration.
                            request["expiration"] = (
                                int((now + pd.Timedelta(minutes=15)).timestamp())
                            )
                        except CandidateRetryableError as exc:
                            return {
                                "state": "WAITING_ORDER_CHECK",
                                "symbol": symbol,
                                "direction": direction,
                                "reason_code": exc.code,
                            }
                        result = self.mt5.order_check(request)
                        if result is None or int(result.retcode) != 0:
                            retcode = None if result is None else int(result.retcode)
                            if retcode is None or retcode in {
                                10004,
                                10012,
                                10018,
                                10020,
                                10021,
                                10024,
                                10028,
                                10031,
                            }:
                                return {
                                    "state": "WAITING_ORDER_CHECK",
                                    "symbol": symbol,
                                    "direction": direction,
                                    "retcode": retcode,
                                    "comment": None if result is None else str(result.comment),
                                }
                            raise core.CriticalLiveError(
                                f"{symbol}: {direction} order preflight rejected: "
                                f"{None if result is None else (retcode, str(result.comment))}"
                            )
                        checks.append(
                            {
                                "leg_key": leg_key,
                                "symbol": symbol,
                                "direction": direction,
                                "risk_scale": scale,
                                "volume": float(request["volume"]),
                                "retcode": int(result.retcode),
                            }
                        )
                finally:
                    self._active_risk_scale = None
                    self._last_sizing = None
        payload = {
            "state": "PASS",
            "checked_at": now.isoformat(),
            "order_send_called": False,
            "checks": checks,
        }
        marker.parent.mkdir(parents=True, exist_ok=True)
        temp = marker.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(marker)
        return {**payload, "path": str(marker)}

    def _place_candidate(
        self,
        output_root: Path,
        decision: dict[str, Any],
        symbol: str,
        reward_r: float,
    ) -> dict[str, object]:
        order_id = str(decision["order_id"])
        leg_key = str(decision["leg_key"])
        comment = self._comment(leg_key, order_id)
        self._drain_order_outbox(output_root)
        intent = self._intent_state(output_root, order_id)
        events = [item for item in self._events(output_root) if item.get("order_id") == order_id]
        broker = self._broker_objects(comment)
        retryable_check = False
        if intent is not None:
            status = str(intent["status"])
            if status in {"SUBMITTED", "LINKED_EXISTING"}:
                return {
                    "state": "IDEMPOTENT_ALREADY_SUBMITTED",
                    "order_id": order_id,
                    "ticket": intent.get("broker_ticket"),
                }
            if broker:
                ticket = int(getattr(broker[0][1], "ticket", 0) or 0)
                self._transition_order_intent(
                    output_root,
                    order_id,
                    "LINKED_EXISTING",
                    {
                        "event": "LINKED_EXISTING",
                        "order_id": order_id,
                        "comment": comment,
                        "broker_kinds": [kind for kind, _ in broker],
                    },
                    broker_ticket=ticket or None,
                )
                return {"state": "IDEMPOTENT_LINKED_EXISTING", "order_id": order_id}
            if status == "CHECK_RETRYABLE":
                retryable_check = True
            else:
                return {
                    "state": f"IDEMPOTENT_{status}_NO_SEND",
                    "order_id": order_id,
                }
        if not retryable_check:
            submitted = [
                item
                for item in events
                if item.get("event") in {"SUBMITTED", "LINKED_EXISTING"}
            ]
            if submitted:
                ticket = submitted[-1].get("ticket")
                self._adopt_order_intent(
                    output_root,
                    order_id,
                    comment,
                    "SUBMITTED",
                    {
                        "event": "MIGRATED_IDEMPOTENCY_LEDGER",
                        "order_id": order_id,
                        "comment": comment,
                    },
                    broker_ticket=int(ticket) if ticket is not None else None,
                )
                return {"state": "IDEMPOTENT_ALREADY_SUBMITTED", "order_id": order_id}
            if events:
                if broker:
                    ticket = int(getattr(broker[0][1], "ticket", 0) or 0)
                    self._adopt_order_intent(
                        output_root,
                        order_id,
                        comment,
                        "LINKED_EXISTING",
                        {
                            "event": "LINKED_EXISTING",
                            "order_id": order_id,
                            "comment": comment,
                            "broker_kinds": [kind for kind, _ in broker],
                        },
                        broker_ticket=ticket or None,
                    )
                    return {"state": "IDEMPOTENT_LINKED_EXISTING", "order_id": order_id}
                raise core.CriticalLiveError(
                    f"{order_id}: uncertain prior order intent; refusing duplicate."
                )
            if broker:
                raise core.CriticalLiveError(
                    f"{order_id}: broker object exists without idempotency ledger."
                )
        entry = self._derived_entry(decision, reward_r)
        try:
            request = self._pending_request(
                symbol,
                str(decision["direction"]),
                entry,
                float(decision["stop_price"]),
                float(decision["target_price"]),
                comment,
            )
        except CandidateRetryableError as exc:
            return {
                "state": "ENTRY_DISTANCE_WAIT_NO_SEND",
                "order_id": order_id,
                "reason_code": exc.code,
            }
        except CandidateNotExecutableError as exc:
            self._adopt_order_intent(
                output_root,
                order_id,
                comment,
                "NOT_EXECUTABLE",
                {
                    "event": "CANDIDATE_NOT_EXECUTABLE",
                    "order_id": order_id,
                    "comment": comment,
                    "leg_key": leg_key,
                    "symbol": symbol,
                    "reason_code": exc.code,
                    "reason": str(exc),
                },
            )
            return {
                "state": "CANDIDATE_NOT_EXECUTABLE_NO_SEND",
                "order_id": order_id,
                "reason_code": exc.code,
            }
        intent_event = {
            "event": "INTENT",
            "order_id": order_id,
            "thesis_id": decision.get("thesis_id"),
            "leg_key": leg_key,
            "symbol": symbol,
            "comment": comment,
            "direction": decision["direction"],
            "volume": request["volume"],
            "entry": request["price"],
            "sl": request["sl"],
            "tp": request["tp"],
            "expiration": request["expiration"],
            "bar_time": decision.get("fvg_time"),
            "known_time": decision.get("fvg_known_time"),
        }
        if not retryable_check:
            claimed = self._claim_order_intent(
                output_root,
                order_id,
                comment,
                request,
                intent_event,
            )
            if not claimed["claimed"]:
                broker = self._broker_objects(comment)
                if broker:
                    ticket = int(getattr(broker[0][1], "ticket", 0) or 0)
                    self._transition_order_intent(
                        output_root,
                        order_id,
                        "LINKED_EXISTING",
                        {
                            "event": "LINKED_EXISTING",
                            "order_id": order_id,
                            "comment": comment,
                            "broker_kinds": [kind for kind, _ in broker],
                        },
                        broker_ticket=ticket or None,
                    )
                    return {"state": "IDEMPOTENT_LINKED_EXISTING", "order_id": order_id}
                return {
                    "state": f"IDEMPOTENT_{claimed['status']}_NO_SEND",
                    "order_id": order_id,
                }
        check = self.mt5.order_check(request)
        if check is None or int(check.retcode) != 0:
            retcode = None if check is None else int(check.retcode)
            if retcode is None or retcode in {
                10004,
                10012,
                10018,
                10020,
                10021,
                10024,
                10028,
                10031,
            }:
                self._transition_order_intent(
                    output_root,
                    order_id,
                    "CHECK_RETRYABLE",
                    {
                        "event": "CHECK_RETRYABLE",
                        "order_id": order_id,
                        "comment": comment,
                        "retcode": retcode,
                        "reason": (
                            str(self.mt5.last_error()) if check is None else str(check.comment)
                        ),
                    },
                )
                return {
                    "state": "CHECK_RETRYABLE_NO_SEND",
                    "order_id": order_id,
                    "retcode": retcode,
                }
            self._transition_order_intent(
                output_root,
                order_id,
                "CHECK_REJECTED",
                {
                    "event": "CHECK_REJECTED",
                    "order_id": order_id,
                    "comment": comment,
                    "retcode": None if check is None else int(check.retcode),
                    "reason": str(self.mt5.last_error()) if check is None else str(check.comment),
                },
            )
            raise core.CriticalLiveError(f"{order_id}: MT5 order_check rejected the demo pending order.")
        self._transition_order_intent(
            output_root,
            order_id,
            "CHECK_PASSED",
            {
                "event": "CHECK_PASSED",
                "order_id": order_id,
                "comment": comment,
                "retcode": int(check.retcode),
            },
        )
        result = self._order_send_checked(request)
        accepted = {
            int(getattr(self.mt5, "TRADE_RETCODE_PLACED", 10008)),
            int(getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)),
        }
        if result is None or int(result.retcode) not in accepted:
            status = "SEND_UNKNOWN" if result is None else "SEND_REJECTED"
            self._transition_order_intent(
                output_root,
                order_id,
                status,
                {
                    "event": "SEND_REJECTED",
                    "order_id": order_id,
                    "comment": comment,
                    "retcode": None if result is None else int(result.retcode),
                    "reason": str(self.mt5.last_error()) if result is None else str(result.comment),
                },
            )
            raise core.CriticalLiveError(f"{order_id}: MT5 rejected the demo pending order.")
        self._transition_order_intent(
            output_root,
            order_id,
            "SUBMITTED",
            {
                "event": "SUBMITTED",
                "order_id": order_id,
                "comment": comment,
                "ticket": int(result.order),
                "deal": int(result.deal),
                "retcode": int(result.retcode),
            },
            broker_ticket=int(result.order),
        )
        return {"state": "SUBMITTED", "order_id": order_id, "ticket": int(result.order)}

    def _remove_order(
        self,
        output_root: Path,
        order: Any,
        reason: str,
        order_id: str | None = None,
    ) -> dict[str, object]:
        request = {
            "action": self.mt5.TRADE_ACTION_REMOVE,
            "order": int(order.ticket),
            "magic": self.magic,
            "comment": str(getattr(order, "comment", "")),
        }
        result = self._order_send_checked(request, require_open_permission=False)
        accepted = int(getattr(self.mt5, "TRADE_RETCODE_DONE", 10009))
        if result is None or int(result.retcode) != accepted:
            raise UnsafeOpenOrdersError(f"Failed to cancel demo pending order {order.ticket}.")
        remaining = self._mt5_collection("orders_get", ticket=int(order.ticket))
        if any(int(getattr(item, "ticket", -1)) == int(order.ticket) for item in remaining):
            raise UnsafeOpenOrdersError(
                f"Demo pending order {order.ticket} remained open after cancellation acknowledgement."
            )
        self._append_order_event(
            output_root,
            {
                "event": "CANCELLED",
                "order_id": order_id,
                "comment": str(getattr(order, "comment", "")),
                "ticket": int(order.ticket),
                "reason": reason,
                "retcode": int(result.retcode),
            },
        )
        return {"ticket": int(order.ticket), "state": "CANCELLED", "reason": reason}

    def cancel_all_pending(self, output_root: Path, reason: str) -> list[dict[str, object]]:
        self._ensure_demo()
        cancelled = []
        for order in self._mt5_collection("orders_get"):
            if int(getattr(order, "magic", -1)) == self.magic:
                cancelled.append(self._remove_order(output_root, order, reason))
        remaining = [
            order
            for order in self._mt5_collection("orders_get")
            if int(getattr(order, "magic", -1)) == self.magic
        ]
        if remaining:
            raise UnsafeOpenOrdersError(
                f"{len(remaining)} demo pending order(s) remained after cancel-all readback."
            )
        return cancelled

    def _pair_cap_state(
        self,
        now: pd.Timestamp,
        configs: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, object]:
        day_start = now.tz_convert(core.TZ).normalize().tz_convert("UTC").to_pydatetime()
        day_end = (now + pd.Timedelta(minutes=1)).to_pydatetime()
        deals = [
            item
            for item in self._mt5_collection("history_deals_get", day_start, day_end)
            if int(getattr(item, "magic", -1)) == self.magic
            and int(getattr(item, "entry", -1))
            == int(getattr(self.mt5, "DEAL_ENTRY_OUT", 1))
        ]
        realized_r = 0.0
        unresolved = False
        symbol_to_reward = {
            str(runtime["legs"][key]["epic"]): float(configs[key].reward_r)
            for key in core.LEG_ORDER
        }
        cap_breached = False
        for deal in sorted(
            deals,
            key=lambda item: (
                int(getattr(item, "time_msc", 0) or 0),
                int(getattr(item, "ticket", 0) or 0),
            ),
        ):
            reason = int(getattr(deal, "reason", -1))
            if reason == int(getattr(self.mt5, "DEAL_REASON_SL", 4)):
                realized_r -= 1.0
            elif reason == int(getattr(self.mt5, "DEAL_REASON_TP", 5)):
                realized_r += symbol_to_reward.get(str(deal.symbol), 0.0)
            else:
                unresolved = True
            if realized_r <= float(runtime["pair_cap_r"]):
                cap_breached = True
        open_positions = [
            item
            for item in self._mt5_collection("positions_get")
            if int(getattr(item, "magic", -1)) == self.magic
        ]
        if unresolved or open_positions:
            state = "WATCH_UNRESOLVED_PRIOR"
        elif cap_breached:
            state = "SUPPRESSED_DAILY_CAP"
        else:
            state = "ALLOWED"
        return {
            "state": state,
            "realized_r": realized_r,
            "cap_breached": cap_breached,
            "closed_deals": len(deals),
            "open_positions": len(open_positions),
        }

    def reconcile_orders(
        self,
        output_root: Path,
        prefix: dict[str, Any],
        now: pd.Timestamp,
        lock: dict[str, Any],
    ) -> dict[str, object]:
        try:
            return self._reconcile_orders_known(output_root, prefix, now, lock)
        except BrokerStateUnknownError as exc:
            self._append_order_event(
                output_root,
                {
                    "event": "BROKER_STATE_UNKNOWN",
                    "reason": str(exc),
                    "known_time": now.isoformat(),
                },
            )
            return {
                "state": "UNKNOWN_NO_SEND",
                "reason": str(exc),
                "known_time": now.isoformat(),
            }

    def _reconcile_orders_known(
        self,
        output_root: Path,
        prefix: dict[str, Any],
        now: pd.Timestamp,
        lock: dict[str, Any],
    ) -> dict[str, object]:
        self._ensure_demo()
        synced_deals = self._sync_broker_events(output_root, now)
        if prefix.get("state") == "DATA_INVALID":
            return {
                "state": "DATA_INVALID_NO_SEND",
                "cancelled": self.cancel_all_pending(output_root, "DATA_INVALID"),
            }
        if prefix.get("state") not in {"VALID", "ALREADY_RECORDED"} or not prefix.get("path"):
            cancelled = self.cancel_all_pending(output_root, "NO_EXECUTABLE_PREFIX")
            return {
                "state": "NO_EXECUTABLE_PREFIX",
                "prefix_state": prefix.get("state"),
                "cancelled": cancelled,
            }
        record = core.read_json(Path(prefix["path"]))
        if record.get("state") == "DATA_INVALID":
            return {
                "state": "DATA_INVALID_NO_SEND",
                "cancelled": self.cancel_all_pending(output_root, "DATA_INVALID"),
            }
        if (
            record.get("state") != "VALID"
            or not record.get("deterministic_rerun")
            or record.get("invariant_errors")
            or record["hashes"]["config"] != lock["live_config_hash"]
            or record["hashes"]["code"] != core.source_code_hash()
        ):
            cancelled = self.cancel_all_pending(output_root, "ILLEGAL_PREFIX_STATE")
            raise core.CriticalLiveError(
                f"Illegal/hash-changed executable prefix; cancelled {len(cancelled)} pending orders."
            )
        configs, _, _ = core.live_strategy_objects()
        runtime = core.runtime_config()
        pair_cap = self._pair_cap_state(now, configs, runtime)
        decisions = list(record["payload"]["decisions"])
        candidates = []
        for decision in decisions:
            leg_key = str(decision["leg_key"])
            cutoff = pd.Timestamp(record["cutoffs"][leg_key])
            frozen_end = pd.Timestamp(
                f"{record['date']} {configs[leg_key].trade_window_end}",
                tz=core.TZ,
            )
            fvg_known = pd.Timestamp(decision["fvg_known_time"]) if decision.get("fvg_known_time") else None
            if (
                cutoff < frozen_end
                and
                decision.get("setup_state") == "VALID"
                and decision.get("order_state") == "CANCELLED"
                and decision.get("terminal_reason") == "CANCELLED_TRADE_WINDOW_END"
                and decision.get("stop_price") is not None
                and decision.get("target_price") is not None
                and fvg_known is not None
                and fvg_known <= cutoff
                and not decision.get("intrabar_ambiguity")
                and not str(decision.get("pair_cap_state", "")).startswith(("WATCH", "SUPPRESSED"))
            ):
                candidates.append(decision)
        results = []
        active_comments = set()
        if pair_cap["state"] == "SUPPRESSED_DAILY_CAP":
            cancelled = self.cancel_all_pending(output_root, "PAIR_CAP")
            return {
                "state": "PAIR_CAP_SUPPRESSED",
                "pair_cap": pair_cap,
                "candidate_count": len(candidates),
                "cancelled": cancelled,
            }
        for decision in candidates:
            leg_key = str(decision["leg_key"])
            order_id = str(decision["order_id"])
            active_comments.add(self._comment(leg_key, order_id))
            prior = [
                item
                for item in self._events(output_root)
                if item.get("order_id") == order_id
                and item.get("event") in {"SUBMITTED", "LINKED_EXISTING"}
            ]
            if pair_cap["state"].startswith("WATCH") and not prior:
                results.append(
                    {
                        "state": "WATCH_PAIR_CAP_NO_SEND",
                        "order_id": order_id,
                        "pair_cap": pair_cap,
                    }
                )
                continue
            results.append(
                self._place_candidate(
                    output_root,
                    decision,
                    str(runtime["legs"][leg_key]["epic"]),
                    float(configs[leg_key].reward_r),
                )
            )
        stale = []
        for order in self._mt5_collection("orders_get"):
            if (
                int(getattr(order, "magic", -1)) == self.magic
                and str(getattr(order, "comment", "")) not in active_comments
            ):
                stale.append(self._remove_order(output_root, order, "NO_LONGER_ACTIVE_PREFIX"))
        return {
            "state": "RECONCILED",
            "known_time": now.isoformat(),
            "candidate_count": len(candidates),
            "pair_cap": pair_cap,
            "synced_deals": synced_deals,
            "results": results,
            "cancelled_stale": stale,
        }

    def daily_health_report(
        self,
        output_root: Path,
        now: pd.Timestamp,
        prefix: dict[str, Any],
        final: dict[str, Any],
        lock: dict[str, Any],
    ) -> dict[str, object]:
        self._ensure_demo()
        self._sync_broker_events(output_root, now)
        trade_date = str(now.tz_convert(core.TZ).date())
        decisions: list[dict[str, Any]] = []
        violations = 0
        data_state = prefix.get("state")
        if prefix.get("path") and Path(prefix["path"]).exists():
            record = core.read_json(Path(prefix["path"]))
            decisions = list(record.get("payload", {}).get("decisions", []))
            violations = len(record.get("invariant_errors") or [])
            data_state = record.get("state")
        events = self._events(output_root)
        day_events = self._events_for_trade_date(events, trade_date)
        day_start = pd.Timestamp(trade_date, tz=core.TZ).tz_convert("UTC").to_pydatetime()
        day_end = (now + pd.Timedelta(minutes=1)).to_pydatetime()
        deals = [
            item
            for item in self._mt5_collection("history_deals_get", day_start, day_end)
            if int(getattr(item, "magic", -1)) == self.magic
        ]
        open_orders = [
            item
            for item in self._mt5_collection("orders_get")
            if int(getattr(item, "magic", -1)) == self.magic
        ]
        positions = [
            {
                "ticket": int(item.ticket),
                "symbol": str(item.symbol),
                "type": int(item.type),
                "volume": float(item.volume),
                "price_open": float(item.price_open),
                "sl": float(item.sl),
                "tp": float(item.tp),
                "comment": str(item.comment),
            }
            for item in self._mt5_collection("positions_get")
            if int(getattr(item, "magic", -1)) == self.magic
        ]
        valid_sessions = 0
        scoreable_decisions = 0
        for marker_path in sorted((output_root / "finalized").glob("*.json")):
            marker = core.read_json(marker_path)
            if marker.get("state") != "VALID":
                continue
            valid_sessions += 1
            session_path = Path(str(marker.get("attempt") or "")) / "session.json"
            if session_path.exists():
                scoreable_decisions += int(core.read_json(session_path).get("decision_count") or 0)
        elapsed_days = max(
            0,
            int(
                (
                    now
                    - pd.Timestamp(lock["created_at"]).tz_convert("UTC")
                )
                / pd.Timedelta(days=1)
            ),
        )
        progress = {
            "elapsed_calendar_days": elapsed_days,
            "valid_sessions": valid_sessions,
            "scoreable_engine_decisions": scoreable_decisions,
            "minimum_calendar_days": 30,
            "minimum_valid_sessions": 30,
            "minimum_scoreable_engine_decisions": 20,
        }
        progress["complete"] = (
            elapsed_days >= 30
            and valid_sessions >= 30
            and scoreable_decisions >= 20
        )
        report = {
            "schema_version": 1,
            "generated_at": now.isoformat(),
            "date": trade_date,
            "state": data_state,
            "decisions": {
                "TAKE": sum(item.get("final_decision") == "TAKE" for item in decisions),
                "SKIP": sum(item.get("final_decision") == "SKIP" for item in decisions),
                "DATA_INVALID": int(data_state == "DATA_INVALID"),
            },
            "orders": {
                "submitted": sum(item.get("event") == "SUBMITTED" for item in day_events),
                "cancelled": sum(item.get("event") == "CANCELLED" for item in day_events),
                "open_pending": len(open_orders),
                "opened_deals": sum(
                    int(getattr(item, "entry", -1))
                    == int(getattr(self.mt5, "DEAL_ENTRY_IN", 0))
                    for item in deals
                ),
                "closed_deals": sum(
                    int(getattr(item, "entry", -1))
                    == int(getattr(self.mt5, "DEAL_ENTRY_OUT", 1))
                    for item in deals
                ),
                "SL": sum(
                    int(getattr(item, "reason", -1))
                    == int(getattr(self.mt5, "DEAL_REASON_SL", 4))
                    for item in deals
                ),
                "TP": sum(
                    int(getattr(item, "reason", -1))
                    == int(getattr(self.mt5, "DEAL_REASON_TP", 5))
                    for item in deals
                ),
                "open_positions": len(positions),
            },
            "positions": positions,
            "campaign_progress": progress,
            "violations": violations,
            "hashes": {
                "code": core.source_code_hash(),
                "config": lock["live_config_hash"],
                "runtime": lock["runtime_config_hash"],
                "status": "LOCKED",
            },
            "prefix": prefix,
            "final": final,
        }
        path = output_root / "daily_health" / f"{trade_date}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        temp.replace(path)
        core.append_jsonl(output_root / "daily_health" / f"{trade_date}.jsonl", report)
        return {"state": "WRITTEN", "path": str(path)}

    def smoke_order(self, output_root: Path, lock: dict[str, Any]) -> dict[str, object]:
        self._require_order_permission()
        exposure_before = self._smoke_exposure()
        if any(exposure_before.values()):
            raise core.CriticalLiveError(
                f"Smoke requires a flat dedicated demo account: {exposure_before}"
            )
        symbol = str(self.config["legs"]["nq"]["epic"])
        if not self.mt5.symbol_select(symbol, True):
            raise core.CriticalLiveError(f"{symbol}: symbol_select failed for smoke order.")
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            raise core.CriticalLiveError(f"{symbol}: no live tick for smoke order.")
        stamp = core.utc_now().strftime("%y%m%d%H%M%S")
        comment = f"{self.config['order_comment_prefix']}:SMOKE:{stamp}"[:31]
        entry = float(tick.bid) * 0.5
        request = self._pending_request(
            symbol,
            "long",
            entry,
            entry * 0.9,
            entry * 1.1,
            comment,
        )
        check = self.mt5.order_check(request)
        if check is None or int(check.retcode) != 0:
            raise core.CriticalLiveError("MT5 smoke order_check failed.")
        sent = self._order_send_checked(request)
        accepted = {
            int(getattr(self.mt5, "TRADE_RETCODE_PLACED", 10008)),
            int(getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)),
        }
        if sent is None or int(sent.retcode) not in accepted:
            raise core.CriticalLiveError("MT5 smoke pending order was rejected.")
        self._append_order_event(
            output_root,
            {
                "event": "SMOKE_SUBMITTED",
                "comment": comment,
                "ticket": int(sent.order),
                "symbol": symbol,
                "volume": request["volume"],
                "price": request["price"],
                "runtime_hash": lock["runtime_config_hash"],
            },
        )
        current = next(
            (
                item
                for item in self._mt5_collection("orders_get", ticket=int(sent.order))
                if int(item.ticket) == int(sent.order)
            ),
            None,
        )
        if current is None:
            raise core.CriticalLiveError("Smoke pending order was not observable after submission.")
        cancelled = self._remove_order(output_root, current, "SMOKE_TEST")
        exposure_after = self._smoke_exposure()
        if any(exposure_after.values()):
            raise core.CriticalLiveError(
                f"Smoke did not return the dedicated demo account to flat: {exposure_after}"
            )
        return {
            "state": "PASS",
            "demo_verified": True,
            "symbol": symbol,
            "minimum_volume": request["volume"],
            "submitted_ticket": int(sent.order),
            "cancelled": cancelled,
            "open_orders_after": exposure_after["open_orders"],
            "open_positions_after": exposure_after["open_positions"],
            "unknown_exposure_after": exposure_after["unknown_exposure"],
        }


def configure_core() -> None:
    core.RUNTIME_CONFIG = RUNTIME_CONFIG
    core.SCRIPT_PATH = Path(__file__).resolve()
    core.HARNESS_PATHS = (
        Path(__file__).resolve(),
        Path(core.__file__).resolve(),
        FORWARD_SHADOW_ADAPTER.resolve(),
    )
    core.REQUIRED_ENV = REQUIRED_ENV
    core.CapitalDemoClient = (
        XmMt5DemoOrderClient
        if core.runtime_config().get("execution") == "MT5_DEMO_ORDERS"
        else XmMt5ReadOnlyClient
    )
    core.install_xm_scheduled_gap_integrity()


def main() -> None:
    configure_core()
    core.main()


if __name__ == "__main__":
    main()
