from __future__ import annotations

import json
from hashlib import sha256
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4
from contextlib import contextmanager
from typing import Any

import pandas as pd

from backtest.numeric_contracts import FinancialMathError, quantize_price


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_capital_forward as core
from backtest.numeric_contracts import validate_trade_geometry
from backtest.live.settings import SettingsError, environment_snapshot, load_settings
from backtest.live.execution import Mt5WritePort
from backtest.live.retry import AllowedTransportError, CircuitBreaker, CircuitOpenError, NonRetryableReadError, RetryError, RetryPolicy
from super1_runtime_guard import order_mutex


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


CANCEL_CONTROL_STATES = {
    "CANCEL_ARMED",
    "CANCEL_ACKNOWLEDGED",
    "CANCEL_UNKNOWN",
    "CANCEL_REJECTED",
}

TERMINAL_INTENT_STATES = frozenset(
    {
        "CHECK_REJECTED",
        "NOT_EXECUTABLE",
        "WINDOW_EXPIRED",
        "WINDOW_NOT_OPEN",
        "PREFIX_REQUIRED",
        "SEND_REJECTED",
        "CLOSED_SL",
        "CLOSED_TP",
        "FILLED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
        "UNKNOWN_NO_SEND",
        "BROKER_REQUEST_MISMATCH_NO_SEND",
        "FILTER_BLOCKED",
        "FILTER_EXPIRED_NO_SEND",
    }
)


ORDER_SCHEMA: dict[str, tuple[str, ...]] = {
    "order_intents": (
        "order_id",
        "status",
        "comment",
        "request_json",
        "broker_ticket",
        "created_at",
        "updated_at",
    ),
    "order_event_outbox": ("sequence", "event_id", "order_id", "event_json", "delivered_at"),
    "broker_execution_states": (
        "order_id",
        "account_login",
        "strategy_order_id",
        "broker_order_ticket",
        "deal_ticket",
        "position_id",
        "state",
        "requested_volume",
        "filled_volume",
        "fill_price",
        "protection_state",
        "details_json",
        "checked_at",
        "updated_at",
    ),
    "strategy_filter_evidence": (
        "strategy",
        "order_id",
        "state",
        "trade_date",
        "targeted_cutoff",
        "full_cutoff",
        "full_cutoffs_json",
        "observed_at",
        "raw_byte_count",
        "raw_sha256",
        "filter_evidence_sha256",
        "created_at",
        "updated_at",
    ),
    "staged_proposals": (
        "proposal_id", "proposal_hash", "proposal_json", "campaign_id", "account_key",
        "release_id", "candidate_hash", "approval_type", "state", "created_at_utc", "updated_at_utc",
    ),
    "approvals": (
        "approval_id", "state", "lease_id", "lease_nonce", "operator_sid", "campaign_id",
        "account_key", "proposal_id", "proposal_hash", "approval_type", "issued_at_utc",
        "expires_at_utc", "release_id", "candidate_hash", "reason", "wire_request_hash",
    ),
    "audit_chain_events": (
        "schema_version", "sequence", "event_id", "occurred_at_utc", "campaign_id", "account_key",
        "entity_type", "entity_id", "event_type", "payload_canonical_json", "previous_hash", "event_hash",
    ),
    "audit_anchor_outbox": (
        "event_id", "sequence", "event_hash", "state", "queued_at_utc",
        "attempted_at_utc", "acked_at_utc", "last_error",
    ),
    "health_checkpoints": (
        "checkpoint_id", "trade_date", "state", "report_json", "created_at_utc",
    ),
    "position_risk_records": (
        "position_id", "order_id", "candidate_hash", "starting_risk_cash", "created_at_utc",
    ),
}


class XmMt5ReadOnlyClient:
    """Read-only market-data adapter. This class intentionally has no order method."""

    def __init__(self, config: dict[str, Any], secrets: dict[str, str], *, mt5_module: Any | None = None):
        if mt5_module is None:
            try:
                import MetaTrader5 as mt5
            except ImportError as exc:
                raise XmMt5Error("MetaTrader5 Python package is not installed.") from exc
        else:
            mt5 = mt5_module
        self.mt5 = mt5
        self.login_id = int(config["account_login"])
        try:
            settings = load_settings(config, environment={**environment_snapshot(), **secrets}, enforce_required=False)
        except SettingsError as exc:
            raise XmMt5Error(str(exc)) from exc
        self.server = settings.server
        self.password = settings.read_only_password or settings.password
        self.terminal_path = settings.terminal_path
        self.portable = bool(config.get("portable", False))
        self.closed_bar_delay_seconds = int(config.get("closed_bar_delay_seconds", 0))
        self.connected = False
        self._read_breaker = CircuitBreaker()
        self._read_retry_policy = RetryPolicy(breaker=self._read_breaker)
        self._bound_output_root: Path | None = None

    def bind_output_root(self, output_root: Path) -> None:
        self._bound_output_root = Path(output_root).resolve()

    def _retry_mt5_read(self, operation: str, *args: object, **kwargs: object) -> Any:
        def read_once() -> Any:
            try:
                value = getattr(self.mt5, operation)(*args, **kwargs)
            except (ConnectionError, TimeoutError, OSError) as exc:
                raise AllowedTransportError(f"MT5 {operation} transport failure") from exc
            except (PermissionError, ValueError, TypeError) as exc:
                raise NonRetryableReadError(f"MT5 {operation} contract/auth failure") from exc
            if value is None:
                raise AllowedTransportError(f"MT5 {operation} returned no state")
            return value

        policy = getattr(self, "_read_retry_policy", None)
        if not isinstance(policy, RetryPolicy):
            policy = RetryPolicy(breaker=getattr(self, "_read_breaker", None))
            self._read_retry_policy = policy
        try:
            return policy.read(read_once)
        except (CircuitOpenError, RetryError) as exc:
            bound_output_root = getattr(self, "_bound_output_root", None)
            if bound_output_root is not None:
                core.write_no_send_sentinel(bound_output_root, "BROKER_READ_CIRCUIT_OPEN", operation=operation)
                core.write_fatal_latch(bound_output_root, "BROKER_READ_CIRCUIT_OPEN", str(exc), operation=operation)
            raise BrokerStateUnknownError(f"MT5 {operation} read is unknown; no-send/HALT is active") from exc

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
        def connect_once() -> Any:
            kwargs: dict[str, Any] = {"login": self.login_id, "server": self.server, "timeout": 60_000}
            if self.password:
                kwargs["password"] = self.password
            if getattr(self, "portable", False):
                kwargs["portable"] = True
            try:
                ok = self.mt5.initialize(self.terminal_path, **kwargs) if self.terminal_path else self.mt5.initialize(**kwargs)
            except (ConnectionError, TimeoutError, OSError) as exc:
                raise AllowedTransportError("MT5 initialize transport failure") from exc
            if not ok:
                self.mt5.shutdown()
                if not self._initialize_terminal_only():
                    self.mt5.shutdown()
                    raise AllowedTransportError("MT5 initialize did not establish a session")
                login_kwargs: dict[str, Any] = {"server": self.server, "timeout": 60_000}
                if self.password:
                    login_kwargs["password"] = self.password
                if not self.mt5.login(self.login_id, **login_kwargs):
                    self.mt5.shutdown()
                    raise AllowedTransportError("MT5 explicit login did not establish a session")
            return self._retry_mt5_read("account_info")

        try:
            policy = getattr(self, "_read_retry_policy", None)
            if not isinstance(policy, RetryPolicy):
                policy = RetryPolicy(breaker=getattr(self, "_read_breaker", None))
                self._read_retry_policy = policy
            account = policy.read(connect_once)
        except RetryError as exc:
            raise XmMt5Error("MT5 connection retry deadline exhausted; broker detail=***") from exc
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
        rates = self._retry_mt5_read(
            "copy_rates_range",
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
                ticks = self._retry_mt5_read(
                    "copy_ticks_range",
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
        info = self._retry_mt5_read("symbol_info", symbol)
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
    def __init__(self, config: dict[str, Any], secrets: dict[str, str], *, mt5_module: Any | None = None):
        super().__init__(config, secrets, mt5_module=mt5_module)
        self.config = config
        self.magic = int(config["magic_number"])
        self.demo_verified = False
        self.account = None
        self.terminal = None

    def _refresh_demo_identity(self) -> None:
        account = self._retry_mt5_read("account_info")
        terminal = self._retry_mt5_read("terminal_info")
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

    @staticmethod
    @contextmanager
    def _order_schema_lock(lock_path: Path):
        """Serialize first-use SQLite schema work across threads and processes."""
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        acquired = False
        deadline = time.monotonic() + 30.0
        try:
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out acquiring SQLite schema lock: {lock_path}"
                        )
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @staticmethod
    def _order_schema_snapshot(connection: sqlite3.Connection) -> dict[str, set[str]]:
        snapshot: dict[str, set[str]] = {}
        for table in ORDER_SCHEMA:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchall()
            if not rows:
                snapshot[table] = set()
                continue
            snapshot[table] = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
        return snapshot

    def _initialize_order_db(self, output_root: Path) -> dict[str, object]:
        """Create and validate the order ledger once under a cross-process lock."""
        path = self._order_db(output_root)
        lock_path = path.with_name("schema.lock")
        with self._order_schema_lock(lock_path):
            connection: sqlite3.Connection | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
                connection.execute("PRAGMA busy_timeout = 30000")
                journal_row = connection.execute("PRAGMA journal_mode").fetchone()
                journal_mode = "" if journal_row is None else str(journal_row[0]).lower()
                snapshot = self._order_schema_snapshot(connection)
                complete = all(
                    set(columns).issubset(snapshot.get(table, set()))
                    for table, columns in ORDER_SCHEMA.items()
                )
                if journal_mode == "wal" and complete:
                    legacy_rows = connection.execute(
                        "SELECT event_id, event_json FROM order_event_outbox"
                    ).fetchall()
                    needs_event_migration = False
                    for event_id, event_json in legacy_rows:
                        try:
                            payload = json.loads(str(event_json))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if isinstance(payload, dict) and (
                            not event_id or str(payload.get("event_id") or "") != str(event_id)
                        ):
                            needs_event_migration = True
                            break
                    if not needs_event_migration:
                        return {"state": "READY", "journal_mode": journal_mode, "wrote": False}

                enabled = connection.execute("PRAGMA journal_mode=WAL").fetchone()
                if enabled is None or str(enabled[0]).lower() != "wal":
                    raise core.CriticalLiveError("SQLite WAL could not be enabled for order ledger.")
                connection.execute("BEGIN IMMEDIATE")
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
                        event_id TEXT,
                        order_id TEXT NOT NULL,
                        event_json TEXT NOT NULL,
                        delivered_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS broker_execution_states (
                        order_id TEXT PRIMARY KEY,
                        account_login INTEGER,
                        strategy_order_id TEXT NOT NULL,
                        broker_order_ticket INTEGER,
                        deal_ticket INTEGER,
                        position_id INTEGER,
                        state TEXT NOT NULL,
                        requested_volume REAL,
                        filled_volume REAL,
                        fill_price REAL,
                        protection_state TEXT,
                        details_json TEXT NOT NULL,
                        checked_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS strategy_filter_evidence (
                        strategy TEXT NOT NULL,
                        order_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        trade_date TEXT,
                        targeted_cutoff TEXT,
                        full_cutoff TEXT,
                        full_cutoffs_json TEXT,
                        observed_at TEXT,
                        raw_byte_count INTEGER NOT NULL,
                        raw_sha256 TEXT NOT NULL,
                        filter_evidence_sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(strategy, order_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS staged_proposals (
                        proposal_id TEXT PRIMARY KEY,
                        proposal_hash TEXT NOT NULL,
                        proposal_json TEXT NOT NULL,
                        campaign_id TEXT NOT NULL,
                        account_key TEXT NOT NULL,
                        release_id TEXT NOT NULL,
                        candidate_hash TEXT NOT NULL,
                        approval_type TEXT NOT NULL DEFAULT 'limit',
                        state TEXT NOT NULL,
                        created_at_utc TEXT NOT NULL,
                        updated_at_utc TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS approvals (
                        approval_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL,
                        lease_id TEXT NOT NULL,
                        lease_nonce TEXT NOT NULL,
                        operator_sid TEXT NOT NULL,
                        campaign_id TEXT NOT NULL,
                        account_key TEXT NOT NULL,
                        proposal_id TEXT NOT NULL UNIQUE,
                        proposal_hash TEXT NOT NULL,
                        approval_type TEXT NOT NULL,
                        issued_at_utc TEXT NOT NULL,
                        expires_at_utc TEXT NOT NULL,
                        release_id TEXT NOT NULL,
                        candidate_hash TEXT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        wire_request_hash TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_chain_events (
                        schema_version INTEGER NOT NULL,
                        sequence INTEGER PRIMARY KEY,
                        event_id TEXT NOT NULL UNIQUE,
                        occurred_at_utc TEXT NOT NULL,
                        campaign_id TEXT NOT NULL,
                        account_key TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_canonical_json TEXT NOT NULL,
                        previous_hash TEXT NOT NULL,
                        event_hash TEXT NOT NULL UNIQUE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_anchor_outbox (
                        event_id TEXT PRIMARY KEY,
                        sequence INTEGER NOT NULL UNIQUE,
                        event_hash TEXT NOT NULL UNIQUE,
                        state TEXT NOT NULL,
                        queued_at_utc TEXT NOT NULL,
                        attempted_at_utc TEXT,
                        acked_at_utc TEXT,
                        last_error TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS health_checkpoints (
                        checkpoint_id TEXT PRIMARY KEY,
                        trade_date TEXT NOT NULL,
                        state TEXT NOT NULL,
                        report_json TEXT NOT NULL,
                        created_at_utc TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS position_risk_records (
                        position_id TEXT PRIMARY KEY,
                        order_id TEXT NOT NULL,
                        candidate_hash TEXT NOT NULL,
                        starting_risk_cash REAL NOT NULL,
                        created_at_utc TEXT NOT NULL
                    )
                    """
                )
                # Preserve legacy rows while completing a partially-created table.
                definitions = {
                    "order_intents": {
                        "order_id": "TEXT",
                        "status": "TEXT",
                        "comment": "TEXT",
                        "request_json": "TEXT",
                        "broker_ticket": "INTEGER",
                        "created_at": "TEXT",
                        "updated_at": "TEXT",
                    },
                    "order_event_outbox": {
                        "sequence": "INTEGER",
                        "event_id": "TEXT",
                        "order_id": "TEXT",
                        "event_json": "TEXT",
                        "delivered_at": "TEXT",
                    },
                    "broker_execution_states": {
                        "order_id": "TEXT",
                        "account_login": "INTEGER",
                        "strategy_order_id": "TEXT",
                        "broker_order_ticket": "INTEGER",
                        "deal_ticket": "INTEGER",
                        "position_id": "INTEGER",
                        "state": "TEXT",
                        "requested_volume": "REAL",
                        "filled_volume": "REAL",
                        "fill_price": "REAL",
                        "protection_state": "TEXT",
                        "details_json": "TEXT",
                        "checked_at": "TEXT",
                        "updated_at": "TEXT",
                    },
                    "strategy_filter_evidence": {
                        "strategy": "TEXT",
                        "order_id": "TEXT",
                        "state": "TEXT",
                        "trade_date": "TEXT",
                        "targeted_cutoff": "TEXT",
                        "full_cutoff": "TEXT",
                        "full_cutoffs_json": "TEXT",
                        "observed_at": "TEXT",
                        "raw_byte_count": "INTEGER",
                        "raw_sha256": "TEXT",
                        "filter_evidence_sha256": "TEXT",
                        "created_at": "TEXT",
                        "updated_at": "TEXT",
                    },
                    "staged_proposals": {
                        "proposal_id": "TEXT",
                        "proposal_hash": "TEXT",
                        "proposal_json": "TEXT",
                        "campaign_id": "TEXT",
                        "account_key": "TEXT",
                        "release_id": "TEXT",
                        "candidate_hash": "TEXT",
                        "approval_type": "TEXT",
                        "state": "TEXT",
                        "created_at_utc": "TEXT",
                        "updated_at_utc": "TEXT",
                    },
                    "approvals": {
                        "approval_id": "TEXT",
                        "state": "TEXT",
                        "lease_id": "TEXT",
                        "lease_nonce": "TEXT",
                        "operator_sid": "TEXT",
                        "campaign_id": "TEXT",
                        "account_key": "TEXT",
                        "proposal_id": "TEXT",
                        "proposal_hash": "TEXT",
                        "approval_type": "TEXT",
                        "issued_at_utc": "TEXT",
                        "expires_at_utc": "TEXT",
                        "release_id": "TEXT",
                        "candidate_hash": "TEXT",
                        "reason": "TEXT",
                        "wire_request_hash": "TEXT",
                    },
                    "audit_chain_events": {
                        "schema_version": "INTEGER",
                        "sequence": "INTEGER",
                        "event_id": "TEXT",
                        "occurred_at_utc": "TEXT",
                        "campaign_id": "TEXT",
                        "account_key": "TEXT",
                        "entity_type": "TEXT",
                        "entity_id": "TEXT",
                        "event_type": "TEXT",
                        "payload_canonical_json": "TEXT",
                        "previous_hash": "TEXT",
                        "event_hash": "TEXT",
                    },
                    "audit_anchor_outbox": {
                        "event_id": "TEXT",
                        "sequence": "INTEGER",
                        "event_hash": "TEXT",
                        "state": "TEXT",
                        "queued_at_utc": "TEXT",
                        "attempted_at_utc": "TEXT",
                        "acked_at_utc": "TEXT",
                        "last_error": "TEXT",
                    },
                    "position_risk_records": {
                        "position_id": "TEXT",
                        "order_id": "TEXT",
                        "candidate_hash": "TEXT",
                        "starting_risk_cash": "REAL",
                        "created_at_utc": "TEXT",
                    },
                    "health_checkpoints": {
                        "checkpoint_id": "TEXT",
                        "trade_date": "TEXT",
                        "state": "TEXT",
                        "report_json": "TEXT",
                        "created_at_utc": "TEXT",
                    },
                }
                snapshot = self._order_schema_snapshot(connection)
                for table, table_definitions in definitions.items():
                    for column, definition in table_definitions.items():
                        if column not in snapshot.get(table, set()):
                            connection.execute(
                                f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
                            )
                # Legacy rows did not carry a stable id.  Sequence is part of
                # the migration input so identical payloads cannot collide.
                legacy_rows = connection.execute(
                    "SELECT sequence, event_id, event_json FROM order_event_outbox"
                ).fetchall()
                for sequence, event_id, event_json in legacy_rows:
                    if event_id and str(event_id).strip():
                        try:
                            payload = json.loads(str(event_json))
                            if isinstance(payload, dict) and str(payload.get("event_id") or "") != str(event_id):
                                if "event_id" not in payload or not payload.get("event_id"):
                                    payload["event_id"] = str(event_id)
                                    connection.execute(
                                        "UPDATE order_event_outbox SET event_json = ? WHERE sequence = ?",
                                        (core.canonical_json(payload), int(sequence)),
                                    )
                        except (TypeError, ValueError, json.JSONDecodeError):
                            pass
                        continue
                    legacy_id = sha256(
                        f"{int(sequence)}\0{str(event_json)}".encode("utf-8")
                    ).hexdigest()
                    migrated_json = str(event_json)
                    try:
                        payload = json.loads(str(event_json))
                        if isinstance(payload, dict):
                            payload["event_id"] = legacy_id
                            migrated_json = core.canonical_json(payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                    connection.execute(
                        "UPDATE order_event_outbox SET event_id = ?, event_json = ? WHERE sequence = ?",
                        (legacy_id, migrated_json, int(sequence)),
                    )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_order_event_outbox_event_id "
                    "ON order_event_outbox(event_id)"
                )
                connection.commit()

                verified_journal = connection.execute("PRAGMA journal_mode").fetchone()
                verified_mode = "" if verified_journal is None else str(verified_journal[0]).lower()
                verified_snapshot = self._order_schema_snapshot(connection)
                if verified_mode != "wal" or any(
                    set(columns).difference(verified_snapshot.get(table, set()))
                    for table, columns in ORDER_SCHEMA.items()
                ):
                    raise core.CriticalLiveError("SQLite order ledger schema/WAL validation failed.")
                return {"state": "READY", "journal_mode": verified_mode, "wrote": True}
            except Exception:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if connection is not None:
                    connection.close()

    def _mt5_collection(self, operation: str, *args: object, **kwargs: object) -> tuple[Any, ...]:
        self._ensure_demo()
        result = self._retry_mt5_read(operation, *args, **kwargs)
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
        adapter = getattr(self, "_write_adapter", None)
        if adapter is None:
            raise core.CriticalLiveError("raw broker writes are disabled; a durable write adapter is required")
        return adapter.send(request)

    def _persist_unknown_send(self, output_root: Path, order_id: str, reason: str, **details: object) -> None:
        """Latch an unresolved post-send broker state before any next candidate."""
        core.write_no_send_sentinel(output_root, reason, order_id=order_id, **details)
        core.write_fatal_latch(output_root, "BROKER_STATE_UNKNOWN", reason, order_id=order_id, **details)

    def _order_connection(self, output_root: Path) -> sqlite3.Connection:
        path = self._order_db(output_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise

    def _ready_order_connection(self, output_root: Path) -> sqlite3.Connection:
        self._initialize_order_db(output_root)
        return self._order_connection(output_root)

    def _drain_order_outbox(self, output_root: Path) -> None:
        with order_mutex():
            connection = self._ready_order_connection(output_root)
            temporary: Path | None = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT sequence, event_id, event_json FROM order_event_outbox ORDER BY sequence"
                ).fetchall()
                log_path = self._order_log(output_root)
                records: list[dict[str, Any]] = []
                event_ids: list[str] = []
                for sequence, event_id, event_json in rows:
                    payload = json.loads(str(event_json))
                    if not isinstance(payload, dict):
                        raise core.CriticalLiveError(
                            f"Order outbox payload is not a JSON object at sequence {sequence}."
                        )
                    if str(payload.get("event_id")) != str(event_id):
                        raise core.CriticalLiveError(
                            f"Order outbox event_id mismatch at sequence {sequence}."
                        )
                    records.append(payload)
                    event_ids.append(str(event_id))
                if len(event_ids) != len(set(event_ids)):
                    raise core.CriticalLiveError("Order outbox contains duplicate event_id values.")
                log_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = log_path.with_name(f"{log_path.name}.{uuid4().hex}.tmp")
                with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                    for record in records:
                        handle.write(core.canonical_json(record) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._replace_projection(temporary, log_path)
                projected_ids = [
                    str(json.loads(line)["event_id"])
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if projected_ids != event_ids:
                    raise core.CriticalLiveError("Order JSONL projection verification failed.")
                if event_ids:
                    placeholders = ",".join("?" for _ in event_ids)
                    connection.execute(
                        f"UPDATE order_event_outbox SET delivered_at = ? "
                        f"WHERE sequence IN (SELECT sequence FROM order_event_outbox "
                        f"WHERE event_id IN ({placeholders}))",
                        (core.utc_now().isoformat(), *event_ids),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
                connection.close()

    @staticmethod
    def _replace_projection(temporary: Path, target: Path) -> None:
        # os.replace is atomic on the supported local filesystems. The
        # temporary file is fsynced before this point; the SQLite commit is
        # deliberately held until the projection has been verified.
        os.replace(temporary, target)

    @staticmethod
    def _insert_outbox(
        connection: sqlite3.Connection,
        order_id: str,
        payload: dict[str, object],
    ) -> str:
        """Insert one immutable event with a UUID generated exactly once."""
        comparable = dict(payload)
        comparable.pop("event_id", None)
        comparable_json = core.canonical_json(comparable)
        for existing_id, existing_json in connection.execute(
            "SELECT event_id, event_json FROM order_event_outbox WHERE order_id = ?",
            (order_id,),
        ).fetchall():
            try:
                existing_payload = json.loads(str(existing_json))
                if isinstance(existing_payload, dict):
                    existing_payload.pop("event_id", None)
                    if core.canonical_json(existing_payload) == comparable_json:
                        return str(existing_id)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        event_id = str(uuid4())
        stored_payload = {**comparable, "event_id": event_id}
        canonical = core.canonical_json(stored_payload)
        connection.execute(
            "INSERT INTO order_event_outbox "
            "(event_id, order_id, event_json) VALUES (?, ?, ?)",
            (event_id, order_id, canonical),
        )
        return event_id

    def _intent_state(self, output_root: Path, order_id: str) -> dict[str, Any] | None:
        connection = self._ready_order_connection(output_root)
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

    def _intent_request(self, output_root: Path, order_id: str) -> dict[str, Any] | None:
        connection = self._ready_order_connection(output_root)
        try:
            row = connection.execute(
                "SELECT request_json FROM order_intents WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or row[0] is None:
            return None
        payload = json.loads(str(row[0]))
        return payload if isinstance(payload, dict) else None

    def _resume_pre_send_intent(
        self,
        output_root: Path,
        order_id: str,
        request: dict[str, object],
        event: dict[str, object],
    ) -> dict[str, object]:
        now = core.utc_now().isoformat()
        canonical_request = core.canonical_json(request)
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, request_json, broker_ticket FROM order_intents WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise core.CriticalLiveError(f"{order_id}: idempotency intent is missing.")
            stored = None
            if row[1] is not None:
                stored = json.loads(str(row[1]))
            if stored is not None and core.canonical_json(stored) != core.canonical_json(request):
                raise core.CriticalLiveError(
                    f"{order_id}: reevaluated request differs from the registered request."
                )
            status = str(row[0])
            if status != "PRE_SEND_DEFERRED":
                connection.commit()
                return {
                    "resumed": False,
                    "status": status,
                    "broker_ticket": row[2],
                }
            updated = connection.execute(
                "UPDATE order_intents SET status = 'INTENT', request_json = ?, updated_at = ? "
                "WHERE order_id = ? AND status = 'PRE_SEND_DEFERRED'",
                (canonical_request, now, order_id),
            ).rowcount
            if updated != 1:
                current = connection.execute(
                    "SELECT status, broker_ticket FROM order_intents WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                if current is None:
                    raise core.CriticalLiveError(f"{order_id}: idempotency intent is missing.")
                if str(current[0]) == "PRE_SEND_DEFERRED":
                    raise core.CriticalLiveError(
                        f"{order_id}: PRE_SEND_DEFERRED CAS update did not affect exactly one row."
                    )
                connection.commit()
                return {
                    "resumed": False,
                    "status": str(current[0]),
                    "broker_ticket": current[1],
                }
            if not self._outbox_event_exists(connection, order_id, "INTENT_REEVALUATED"):
                payload = {"recorded_at": now, "magic": self.magic, **event}
                self._insert_outbox(connection, order_id, payload)
            connection.commit()
            result = {
                "resumed": True,
                "status": "INTENT",
                "broker_ticket": None,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)
        return result

    @staticmethod
    def _outbox_event_exists(
        connection: sqlite3.Connection, order_id: str, event_name: str
    ) -> bool:
        for (event_json,) in connection.execute(
            "SELECT event_json FROM order_event_outbox WHERE order_id = ?",
            (order_id,),
        ).fetchall():
            try:
                if json.loads(str(event_json)).get("event") == event_name:
                    return True
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return False

    def _reject_terminal_transition(
        self,
        output_root: Path,
        connection: sqlite3.Connection,
        order_id: str,
        current_status: str,
        target_status: str,
        event: dict[str, object],
        now: str,
    ) -> None:
        details = {
            "order_id": order_id,
            "from_state": current_status,
            "to_state": target_status,
        }
        core.write_no_send_sentinel(
            output_root,
            "ILLEGAL_ORDER_TRANSITION",
            reason_code="ILLEGAL_ORDER_TRANSITION",
            **details,
        )
        core.write_fatal_latch(
            output_root,
            "ILLEGAL_ORDER_TRANSITION",
            f"{order_id}: terminal state {current_status} cannot transition to {target_status}",
            reason="ILLEGAL_ORDER_TRANSITION",
            **details,
        )
        payload = {
            "recorded_at": now,
            "magic": self.magic,
            "event": "ILLEGAL_ORDER_TRANSITION",
            **details,
            "reason": "ILLEGAL_ORDER_TRANSITION",
            "requested_event": event,
        }
        self._insert_outbox(connection, order_id, payload)
        audit = getattr(self, "_insert_canonical_audit", None)
        if callable(audit):
            audit(connection, order_id, "ILLEGAL_ORDER_TRANSITION", payload)
        connection.commit()
        raise core.CriticalLiveError(
            f"{order_id}: illegal terminal order transition {current_status} -> {target_status}"
        )

    def _record_intent_event(
        self,
        output_root: Path,
        order_id: str,
        event: dict[str, object],
        *,
        request: dict[str, object] | None = None,
        allowed_statuses: set[str] | None = None,
    ) -> dict[str, object]:
        """Append an intent event without changing its durable lifecycle status."""
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, request_json FROM order_intents WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise core.CriticalLiveError(f"{order_id}: idempotency intent is missing.")
            status = str(row[0])
            if allowed_statuses is not None and status not in allowed_statuses:
                connection.commit()
                return {"status": status, "recorded": False}
            if request is not None and row[1] is not None:
                stored = json.loads(str(row[1]))
                if core.canonical_json(stored) != core.canonical_json(request):
                    raise core.CriticalLiveError(
                        f"{order_id}: event request differs from the registered request."
                    )
            event_name = str(event.get("event") or "")
            if not self._outbox_event_exists(connection, order_id, event_name):
                payload = {"recorded_at": now, "magic": self.magic, **event}
                self._insert_outbox(connection, order_id, payload)
                recorded = True
            else:
                recorded = False
            connection.commit()
            result = {"status": status, "recorded": recorded}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if result["recorded"]:
            self._drain_order_outbox(output_root)
        return result

    def _arm_send(
        self,
        output_root: Path,
        order_id: str,
        request: dict[str, object],
        event: dict[str, object],
    ) -> dict[str, object]:
        """CAS an executable intent to SEND_ARMED before the sole SDK send."""
        now = core.utc_now().isoformat()
        allowed = {"INTENT", "CHECK_RETRYABLE", "PRE_SEND_DEFERRED"}
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, request_json FROM order_intents WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise core.CriticalLiveError(f"{order_id}: idempotency intent is missing.")
            status = str(row[0])
            if status not in allowed:
                connection.commit()
                return {
                    "armed": False,
                    "status": status,
                    "order_id": order_id,
                    "reason": "CAS_LOST_OR_INTENT_ALREADY_TERMINAL",
                }
            if row[1] is None:
                raise core.CriticalLiveError(f"{order_id}: registered request is missing.")
            stored = json.loads(str(row[1]))
            if core.canonical_json(stored) != core.canonical_json(request):
                raise core.CriticalLiveError(
                    f"{order_id}: send request differs from the registered request."
                )
            controls = connection.execute(
                "SELECT order_id, status, comment, broker_ticket FROM order_intents "
                "WHERE status IN ('CANCEL_ARMED', 'CANCEL_ACKNOWLEDGED', "
                "'CANCEL_UNKNOWN', 'CANCEL_REJECTED') ORDER BY created_at"
            ).fetchall()
            if controls:
                connection.commit()
                return {
                    "armed": False,
                    "status": status,
                    "order_id": order_id,
                    "reason": "A durable cancellation attempt is unresolved.",
                    "persistent_cancel_controls": [
                        {
                            "order_id": str(control[0]),
                            "status": str(control[1]),
                            "comment": str(control[2]),
                            "broker_ticket": control[3],
                        }
                        for control in controls
                    ],
                }
            updated = connection.execute(
                "UPDATE order_intents SET status = 'SEND_ARMED', updated_at = ? "
                "WHERE order_id = ? AND status IN ('INTENT', 'CHECK_RETRYABLE', 'PRE_SEND_DEFERRED')",
                (now, order_id),
            ).rowcount
            if updated != 1:
                current = connection.execute(
                    "SELECT status FROM order_intents WHERE order_id = ?", (order_id,)
                ).fetchone()
                connection.commit()
                return {
                    "armed": False,
                    "status": None if current is None else str(current[0]),
                    "order_id": order_id,
                    "reason": "CAS_LOST_OR_INTENT_ALREADY_TERMINAL",
                }
            if not self._outbox_event_exists(connection, order_id, "SEND_ARMED"):
                payload = {"recorded_at": now, "magic": self.magic, **event}
                self._insert_outbox(connection, order_id, payload)
            connection.commit()
            return {"armed": True, "status": "SEND_ARMED", "order_id": order_id}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _request_from_pending_order(self, order: Any) -> dict[str, object]:
        fields = {
            "symbol": getattr(order, "symbol", None),
            "type": getattr(order, "type", None),
            "volume": getattr(order, "volume_initial", None),
            "price": getattr(order, "price_open", None),
            "sl": getattr(order, "sl", None),
            "tp": getattr(order, "tp", None),
            "expiration": getattr(order, "time_expiration", None),
        }
        if any(value is None for value in fields.values()):
            raise UnsafeOpenOrdersError(
                f"Pending order {getattr(order, 'ticket', '?')} lacks immutable broker fields."
            )
        return {
            "action": self.mt5.TRADE_ACTION_PENDING,
            **fields,
            "magic": self.magic,
            "comment": str(getattr(order, "comment", "")),
        }

    def _record_broker_state(
        self,
        output_root: Path,
        order_id: str,
        state: str,
        details: dict[str, object],
    ) -> None:
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute(
                """
                INSERT INTO broker_execution_states (
                    order_id, account_login, strategy_order_id, broker_order_ticket,
                    deal_ticket, position_id, state, requested_volume, filled_volume,
                    fill_price, protection_state, details_json, checked_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    account_login=excluded.account_login,
                    broker_order_ticket=excluded.broker_order_ticket,
                    deal_ticket=excluded.deal_ticket,
                    position_id=excluded.position_id,
                    state=excluded.state,
                    requested_volume=excluded.requested_volume,
                    filled_volume=excluded.filled_volume,
                    fill_price=excluded.fill_price,
                    protection_state=excluded.protection_state,
                    details_json=excluded.details_json,
                    checked_at=excluded.checked_at,
                    updated_at=excluded.updated_at
                """,
                (
                    order_id,
                    int(getattr(self, "login_id", 0) or 0) or None,
                    order_id,
                    details.get("broker_order_ticket"),
                    details.get("deal_ticket"),
                    details.get("position_id"),
                    state,
                    details.get("requested_volume"),
                    details.get("filled_volume"),
                    details.get("fill_price"),
                    details.get("protection_state"),
                    core.canonical_json(details),
                    now,
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _record_health_checkpoint(self, output_root: Path, report: Mapping[str, Any]) -> None:
        """Persist health as canonical order-DB state; JSON is only a projection."""
        connection = self._ready_order_connection(output_root)
        audit_required = callable(getattr(self, "_insert_canonical_audit", None))
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS health_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    state TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                )
                """
            )
            payload = core.canonical_json(report)
            checkpoint_id = sha256(payload.encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT OR REPLACE INTO health_checkpoints(checkpoint_id,trade_date,state,report_json,created_at_utc) VALUES(?,?,?,?,?)",
                (checkpoint_id, str(report.get("date") or ""), str(report.get("state") or ""), payload, core.utc_now().isoformat()),
            )
            audit = getattr(self, "_insert_canonical_audit", None)
            if callable(audit):
                audit_payload = {
                    "event": "HEALTH_CHECKPOINT",
                    "checkpoint_id": checkpoint_id,
                    "report_hash": checkpoint_id,
                    "trade_date": str(report.get("date") or ""),
                    "state": str(report.get("state") or ""),
                    "campaign_id": str(getattr(self, "config", {}).get("campaign_id") or ""),
                    "account_key": str(getattr(self, "config", {}).get("account_login") or ""),
                }
                audit(connection, f"HEALTH:{checkpoint_id}", "HEALTH_CHECKPOINT", audit_payload)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if audit_required:
            try:
                self._deliver_audit_anchors(output_root)
            except Exception as exc:
                core.write_no_send_sentinel(
                    output_root,
                    "AUDIT_ANCHOR_UNACKED",
                    error=str(exc),
                )
                core.write_fatal_latch(
                    output_root,
                    "AUDIT_ANCHOR_UNACKED",
                    str(exc),
                )
                raise
        self._append_order_event(
            output_root,
            {
                "event": "BROKER_STATE",
                "order_id": order_id,
                "broker_state": state,
                "account_login": int(getattr(self, "login_id", 0) or 0),
                "checked_at": now,
                **details,
            },
        )

    def _claim_order_intent(
        self,
        output_root: Path,
        order_id: str,
        comment: str,
        request: dict[str, object],
        event: dict[str, object],
    ) -> dict[str, Any]:
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
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
                self._insert_outbox(connection, order_id, payload)
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
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT status FROM order_intents WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if current_row is None:
                raise core.CriticalLiveError(f"{order_id}: idempotency intent is missing.")
            current_status = str(current_row[0])
            if current_status == status:
                connection.commit()
                return
            if current_status in TERMINAL_INTENT_STATES:
                self._reject_terminal_transition(
                    output_root, connection, order_id, current_status, status, event, now
                )
            updated = connection.execute(
                "UPDATE order_intents SET status = ?, broker_ticket = COALESCE(?, broker_ticket), "
                "updated_at = ? WHERE order_id = ?",
                (status, broker_ticket, now, order_id),
            ).rowcount
            if updated != 1:
                raise core.CriticalLiveError(f"{order_id}: idempotency intent is missing.")
            payload = {"recorded_at": now, "magic": self.magic, **event}
            self._insert_outbox(connection, order_id, payload)
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
        request: dict[str, object] | None = None,
    ) -> None:
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT status FROM order_intents WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if (
                current_row is not None
                and str(current_row[0]) != status
                and str(current_row[0]) in TERMINAL_INTENT_STATES
            ):
                self._reject_terminal_transition(
                    output_root,
                    connection,
                    order_id,
                    str(current_row[0]),
                    status,
                    event,
                    now,
                )
            connection.execute(
                "INSERT OR IGNORE INTO order_intents "
                "(order_id, status, comment, request_json, broker_ticket, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    status,
                    comment,
                    None if request is None else core.canonical_json(request),
                    broker_ticket,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE order_intents SET status = ?, request_json = COALESCE(?, request_json), "
                "broker_ticket = COALESCE(?, broker_ticket), updated_at = ? WHERE order_id = ?",
                (
                    status,
                    None if request is None else core.canonical_json(request),
                    broker_ticket,
                    now,
                    order_id,
                ),
            )
            payload = {"recorded_at": now, "magic": self.magic, **event}
            self._insert_outbox(connection, order_id, payload)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)

    def _append_order_event(self, output_root: Path, event: dict[str, object]) -> None:
        payload = {"recorded_at": core.utc_now().isoformat(), "magic": self.magic, **event}
        order_key = str(
            payload.get("order_id")
            or f"EVENT:{payload.get('event', '')}:{payload.get('broker_ticket', payload.get('ticket', ''))}:{payload.get('known_time', payload['recorded_at'])}"
        )
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_outbox(connection, order_key, payload)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)

    def _events(self, output_root: Path) -> list[dict[str, Any]]:
        connection = self._ready_order_connection(output_root)
        try:
            rows = connection.execute(
                "SELECT event_json FROM order_event_outbox ORDER BY sequence"
            ).fetchall()
            events = []
            for (event_json,) in rows:
                payload = json.loads(str(event_json))
                if not isinstance(payload, dict):
                    raise core.CriticalLiveError("Order outbox payload is not a JSON object.")
                events.append(payload)
            return events
        finally:
            connection.close()

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

    def _broker_objects(
        self,
        comment: str,
        *,
        broker_ticket: int | None = None,
        request: dict[str, Any] | None = None,
    ) -> list[tuple[str, Any]]:
        self._ensure_demo()
        found: list[tuple[str, Any]] = []
        for item in self._mt5_collection(
            "orders_get", **({"ticket": int(broker_ticket)} if broker_ticket else {})
        ):
            if int(getattr(item, "magic", -1)) != self.magic:
                continue
            if broker_ticket and int(getattr(item, "ticket", 0) or 0) != int(broker_ticket):
                continue
            if not broker_ticket and str(getattr(item, "comment", "")) != comment:
                continue
            if request is not None and not self._broker_request_matches(item, request):
                found.append(("ORDER_MISMATCH", item))
            else:
                found.append(("ORDER", item))
        for item in self._mt5_collection("positions_get"):
            if int(getattr(item, "magic", -1)) != self.magic:
                continue
            if not broker_ticket and str(getattr(item, "comment", "")) != comment:
                continue
            if request is not None and str(getattr(item, "symbol", request.get("symbol"))) != str(
                request.get("symbol")
            ):
                continue
            found.append(("POSITION", item))
        start = (core.utc_now() - pd.Timedelta(days=35)).to_pydatetime()
        end = (core.utc_now() + pd.Timedelta(minutes=1)).to_pydatetime()
        for item in self._mt5_collection("history_orders_get", start, end):
            if int(getattr(item, "magic", -1)) != self.magic:
                continue
            if broker_ticket and int(getattr(item, "ticket", 0) or 0) != int(broker_ticket):
                continue
            if not broker_ticket and str(getattr(item, "comment", "")) != comment:
                continue
            found.append(("HISTORY_ORDER", item))
        return found

    def _broker_request_matches(self, item: Any, request: dict[str, Any]) -> bool:
        def matches_number(actual: object, expected: object, tolerance: float = 1e-8) -> bool:
            if actual is None or expected is None:
                return False
            try:
                return abs(float(actual) - float(expected)) <= tolerance
            except (TypeError, ValueError):
                return False

        for field in ("symbol", "type"):
            actual = getattr(item, field, None)
            if actual is None or str(actual) != str(request.get(field)):
                return False
        # Identity is bound to the broker's initial order volume.  The
        # current volume is a lifecycle remainder and may legitimately be
        # zero after a full fill or smaller after a partial fill.
        initial_volume = getattr(item, "volume_initial", None)
        if initial_volume is None or not matches_number(initial_volume, request.get("volume")):
            return False
        for names, expected_name in (
            (("price_open", "price"), "price"),
            (("sl",), "sl"),
            (("tp",), "tp"),
            (("time_expiration", "expiration"), "expiration"),
        ):
            actuals = [getattr(item, name, None) for name in names]
            actual = next((value for value in actuals if value is not None), None)
            tolerance = 1e-8 if expected_name == "expiration" else 1e-6
            if not matches_number(actual, request.get(expected_name), tolerance):
                return False
            if any(
                value is not None
                and not matches_number(value, request.get(expected_name), tolerance)
                for value in actuals
            ):
                return False
        return True

    def _broker_execution_chain(
        self,
        order_id: str,
        comment: str,
        request: dict[str, Any],
        broker_ticket: int | None,
        now: pd.Timestamp,
    ) -> tuple[str, dict[str, object]]:
        self._ensure_demo()
        start = (now - pd.Timedelta(days=35)).to_pydatetime()
        end = (now + pd.Timedelta(minutes=1)).to_pydatetime()

        if broker_ticket:
            current_orders = list(
                self._mt5_collection("orders_get", ticket=int(broker_ticket))
            )
            history_orders = list(
                self._mt5_collection("history_orders_get", ticket=int(broker_ticket))
            )
        else:
            current_orders = [
                item
                for item in self._mt5_collection("orders_get")
                if int(getattr(item, "magic", -1)) == self.magic
                and str(getattr(item, "comment", "")) == comment
            ]
            history_candidates = [
                item
                for item in self._mt5_collection("history_orders_get", start, end)
                if int(getattr(item, "magic", -1)) == self.magic
                and str(getattr(item, "comment", "")) == comment
            ]
            tickets = {
                int(getattr(item, "ticket", 0) or 0)
                for item in [*current_orders, *history_candidates]
                if int(getattr(item, "ticket", 0) or 0)
            }
            if len(tickets) > 1:
                return "BROKER_REQUEST_MISMATCH_NO_SEND", {
                    "account_login": int(getattr(self, "login_id", 0) or 0),
                    "strategy_order_id": order_id,
                    "requested_volume": float(request.get("volume") or 0.0),
                    "reason": "Multiple broker entry order tickets match one strategy intent.",
                    "matching_order_tickets": sorted(tickets),
                    "checked_at": now.isoformat(),
                }
            broker_ticket = next(iter(tickets), None)
            history_orders = (
                list(self._mt5_collection("history_orders_get", ticket=int(broker_ticket)))
                if broker_ticket
                else history_candidates
            )

        if broker_ticket:
            # MetaTrader5 history_deals_get(ticket=...) means the deals
            # belonging to this entry order.  `order=` is not a documented
            # keyword for this API and must never be used as a ticket alias.
            entry_deals = list(
                self._mt5_collection("history_deals_get", ticket=int(broker_ticket))
            )
        else:
            entry_deals = []

        details: dict[str, object] = {
            "account_login": int(getattr(self, "login_id", 0) or 0),
            "strategy_order_id": order_id,
            "broker_order_ticket": broker_ticket,
            "requested_volume": float(request.get("volume") or 0.0),
            "history_order_count": len(history_orders),
            "deal_count": len(entry_deals),
            "checked_at": now.isoformat(),
        }

        order_evidence = [*current_orders, *history_orders]
        if broker_ticket and not order_evidence:
            details["reason"] = "Entry order readback is missing."
            return "BROKER_REQUEST_MISMATCH_NO_SEND", details
        if any(
            int(getattr(item, "magic", -1)) != self.magic
            or str(getattr(item, "comment", "")) != comment
            or not self._broker_request_matches(item, request)
            for item in order_evidence
        ):
            details["mismatched_order_tickets"] = [
                int(getattr(item, "ticket", 0) or 0) for item in order_evidence
            ]
            return "BROKER_REQUEST_MISMATCH_NO_SEND", details

        info = self._retry_mt5_read("symbol_info", str(request.get("symbol") or ""))
        volume_step = float(getattr(info, "volume_step", 0.0) or 0.0) if info is not None else 0.0
        volume_tolerance = max(volume_step * 1e-6, 1e-8)
        if current_orders:
            if len(current_orders) != 1:
                details["reason"] = "Multiple current broker orders match one strategy intent."
                return "BROKER_REQUEST_MISMATCH_NO_SEND", details
            orders = current_orders
            details["broker_order_ticket"] = int(
                getattr(orders[0], "ticket", broker_ticket or 0) or broker_ticket or 0
            )
            current_volume = getattr(orders[0], "volume_current", None)
            if current_volume is None:
                details["reason"] = "Current pending order volume is missing."
                return "BROKER_REQUEST_MISMATCH_NO_SEND", details
            pending_volume = float(current_volume)
            if pending_volume < -volume_tolerance:
                details["reason"] = "Current pending order volume is negative."
                return "BROKER_REQUEST_MISMATCH_NO_SEND", details
            details["pending_volume"] = pending_volume
        else:
            pending_volume = 0.0

        order_state = int(getattr(history_orders[-1], "state", -1)) if history_orders else -1
        state_rejected = int(getattr(self.mt5, "ORDER_STATE_REJECTED", 5))
        state_canceled = int(getattr(self.mt5, "ORDER_STATE_CANCELED", 2))
        state_expired = int(getattr(self.mt5, "ORDER_STATE_EXPIRED", 6))
        if history_orders and order_state == state_rejected and not entry_deals:
            details["history_order_state"] = order_state
            return "REJECTED", details
        if history_orders and order_state == state_canceled and not entry_deals:
            details["history_order_state"] = order_state
            return "CANCELLED", details
        if history_orders and order_state == state_expired and not entry_deals:
            details["history_order_state"] = order_state
            return "EXPIRED", details

        entry_in = int(getattr(self.mt5, "DEAL_ENTRY_IN", 0))
        entry_out = int(getattr(self.mt5, "DEAL_ENTRY_OUT", 1))
        expected_deal_type: int | None = None
        if int(request.get("type", -1)) == int(getattr(self.mt5, "ORDER_TYPE_BUY_LIMIT", -2)):
            expected_deal_type = int(getattr(self.mt5, "ORDER_TYPE_BUY", 0))
        elif int(request.get("type", -1)) == int(getattr(self.mt5, "ORDER_TYPE_SELL_LIMIT", -3)):
            expected_deal_type = int(getattr(self.mt5, "ORDER_TYPE_SELL", 1))
        if expected_deal_type is None:
            details["reason"] = "The pending order type cannot determine entry direction."
            return "BROKER_REQUEST_MISMATCH_NO_SEND", details

        if any(
            int(getattr(item, "entry", -1)) != entry_in
            or int(getattr(item, "order", 0) or 0) != int(broker_ticket or 0)
            or str(getattr(item, "symbol", "")) != str(request.get("symbol"))
            or int(getattr(item, "type", -1)) != expected_deal_type
            or float(getattr(item, "volume", 0.0) or 0.0) <= 0
            or int(getattr(item, "position_id", 0) or 0) <= 0
            for item in entry_deals
        ):
            details["reason"] = "Entry deal is missing exact order, position, symbol, direction, or role evidence."
            return "BROKER_REQUEST_MISMATCH_NO_SEND", details
        fills = list(entry_deals)
        position_ids = {
            int(getattr(item, "position_id", 0) or 0) for item in fills
        }
        position_id = next(iter(position_ids), None) if len(position_ids) == 1 else None
        if len(position_ids) > 1:
            details["position_ids"] = sorted(position_ids)
            return "BROKER_REQUEST_MISMATCH_NO_SEND", details

        filled_volume = sum(float(getattr(item, "volume", 0.0) or 0.0) for item in entry_deals)
        exits: list[Any] = []
        if position_id is not None:
            position_deals = list(
                self._mt5_collection("history_deals_get", position=int(position_id))
            )
            if not position_deals:
                details["reason"] = "Position-linked deal history is missing."
                return "BROKER_REQUEST_MISMATCH_NO_SEND", details
            position_entry_volume = 0.0
            for item in position_deals:
                if int(getattr(item, "position_id", 0) or 0) != position_id:
                    continue
                if str(getattr(item, "symbol", "")) != str(request.get("symbol")):
                    details["reason"] = "Position history contains a different symbol."
                    return "BROKER_REQUEST_MISMATCH_NO_SEND", details
                role = int(getattr(item, "entry", -1))
                if role == entry_in:
                    if (
                        int(getattr(item, "type", -1)) != expected_deal_type
                        or int(getattr(item, "order", 0) or 0) != int(broker_ticket or 0)
                    ):
                        details["reason"] = "Position-linked entry deal is not owned by the entry order."
                        return "BROKER_REQUEST_MISMATCH_NO_SEND", details
                    position_entry_volume += float(getattr(item, "volume", 0.0) or 0.0)
                elif role == entry_out:
                    expected_exit_type = (
                        int(getattr(self.mt5, "ORDER_TYPE_SELL", 1))
                        if expected_deal_type == int(getattr(self.mt5, "ORDER_TYPE_BUY", 0))
                        else int(getattr(self.mt5, "ORDER_TYPE_BUY", 0))
                    )
                    if int(getattr(item, "type", -1)) != expected_exit_type:
                        return "BROKER_REQUEST_MISMATCH_NO_SEND", details
                    exits.append(item)
                else:
                    details["reason"] = "Position history contains a deal with an unknown entry role."
                    return "BROKER_REQUEST_MISMATCH_NO_SEND", details
            if abs(position_entry_volume - filled_volume) > volume_tolerance:
                details["reason"] = "Position-linked entry volume does not match entry-order deals."
                return "BROKER_REQUEST_MISMATCH_NO_SEND", details

        exit_volume = sum(float(getattr(item, "volume", 0.0) or 0.0) for item in exits)
        requested_volume = float(request.get("volume") or 0.0)
        details.update(
            {
                "filled_volume": filled_volume,
                "entry_filled_volume": filled_volume,
                "exit_volume": exit_volume,
                "position_id": position_id,
                "entry_deal_tickets": [int(getattr(item, "ticket", 0) or 0) for item in fills],
                "exit_deal_tickets": [int(getattr(item, "ticket", 0) or 0) for item in exits],
            }
        )
        if fills:
            details["deal_ticket"] = int(getattr(fills[-1], "ticket", 0) or 0) or None
            details["fill_price"] = (
                sum(
                    float(getattr(item, "price", 0.0) or 0.0)
                    * float(getattr(item, "volume", 0.0) or 0.0)
                    for item in fills
                )
                / filled_volume
                if filled_volume
                else None
            )

        position: Any | None = None
        if position_id is not None:
            positions = list(
                self._mt5_collection("positions_get", ticket=int(position_id))
            )
            details["position_count"] = len(positions)
            if len(positions) > 1:
                return "BROKER_REQUEST_MISMATCH_NO_SEND", details
            position = positions[0] if positions else None
        else:
            details["position_count"] = 0

        if current_orders and (not fills or pending_volume > volume_tolerance):
            expected_remainder = max(requested_volume - filled_volume, 0.0)
            if abs(pending_volume - expected_remainder) > volume_tolerance:
                details["reason"] = "Pending remainder does not balance initial and entry volumes."
                return "BROKER_REQUEST_MISMATCH_NO_SEND", details
            if not fills:
                return "PENDING_CONFIRMED", details

        if not fills:
            details["reason"] = "No exact entry deal is visible yet; broker state remains unresolved."
            return "UNKNOWN_NO_SEND", details

        # A half-step fill is a genuine partial, not a rounding tolerance.
        details["pending_remainder_volume"] = max(requested_volume - filled_volume, 0.0)
        details["open_position_volume"] = (
            float(getattr(position, "volume", 0.0) or 0.0) if position is not None else 0.0
        )

        if filled_volume - requested_volume > volume_tolerance:
            details["reason"] = "Entry fill volume exceeds the broker order initial volume."
            return "BROKER_REQUEST_MISMATCH_NO_SEND", details
        if exit_volume - filled_volume > volume_tolerance:
            details["reason"] = "Exit volume exceeds verified entry volume."
            return "BROKER_REQUEST_MISMATCH_NO_SEND", details

        if position is not None:
            expected_position_type = (
                int(getattr(self.mt5, "POSITION_TYPE_BUY", 0))
                if expected_deal_type == int(getattr(self.mt5, "ORDER_TYPE_BUY", 0))
                else int(getattr(self.mt5, "POSITION_TYPE_SELL", 1))
            )
            if (
                str(getattr(position, "symbol", "")) != str(request.get("symbol"))
                or int(getattr(position, "type", -1)) != expected_position_type
            ):
                details["reason"] = "Broker position symbol or direction does not match the strategy request."
                return "BROKER_REQUEST_MISMATCH_NO_SEND", details
            open_volume = float(getattr(position, "volume", 0.0) or 0.0)
            expected_open_volume = max(filled_volume - exit_volume, 0.0)
            if abs(open_volume - expected_open_volume) > volume_tolerance:
                details["reason"] = "Open position volume does not balance entry and exit deal volumes."
                return "POSITION_VOLUME_MISMATCH_NO_SEND", details
            expected_sl = float(request.get("sl") or 0.0)
            expected_tp = float(request.get("tp") or 0.0)
            actual_sl = float(getattr(position, "sl", 0.0) or 0.0)
            actual_tp = float(getattr(position, "tp", 0.0) or 0.0)
            tolerance = max(volume_tolerance, 1e-6)
            if info is not None:
                point = float(getattr(info, "point", 0.0) or 0.0)
                tick_size = float(getattr(info, "trade_tick_size", 0.0) or point)
                tolerance = max(tolerance, tick_size)
            protected = (
                actual_sl > 0
                and actual_tp > 0
                and abs(actual_sl - expected_sl) <= tolerance
                and abs(actual_tp - expected_tp) <= tolerance
            )
            details.update(
                {
                    "position_ticket": int(getattr(position, "ticket", 0) or 0),
                    "protection_state": "PROTECTED" if protected else "MISMATCH",
                    "position_sl": actual_sl,
                    "position_tp": actual_tp,
                }
            )
            if not protected:
                return "PROTECTION_MISMATCH_NO_SEND", details

        if abs(exit_volume - filled_volume) <= volume_tolerance and not current_orders and exits:
            sl_reason = int(getattr(self.mt5, "DEAL_REASON_SL", 4))
            tp_reason = int(getattr(self.mt5, "DEAL_REASON_TP", 5))
            reasons = {int(getattr(item, "reason", -1)) for item in exits}
            details["close_volume_balanced"] = True
            if reasons == {sl_reason}:
                return "CLOSED_SL", details
            if reasons == {tp_reason}:
                return "CLOSED_TP", details
            return "CLOSED_UNKNOWN", details
        if filled_volume + volume_tolerance < requested_volume:
            return "PARTIAL_FILL", details

        if position is not None:
            return "OPEN_PROTECTED", details

        if exits and exit_volume + volume_tolerance < filled_volume:
            return "PARTIAL_EXIT_NO_SEND", details
        details["reason"] = "Entry deal exists without a visible position or complete exit evidence."
        return "ENTRY_UNPROTECTED_NO_SEND", details

    def _reconcile_persistent_intents(
        self,
        output_root: Path,
        now: pd.Timestamp,
    ) -> list[dict[str, object]]:
        connection = self._ready_order_connection(output_root)
        try:
            rows = connection.execute(
                "SELECT order_id, status, comment, request_json, broker_ticket "
                "FROM order_intents ORDER BY created_at"
            ).fetchall()
        finally:
            connection.close()
        terminal = {
            "PRE_SEND_DEFERRED",
            "NOT_EXECUTABLE",
            "CHECK_RETRYABLE",
            "CHECK_REJECTED",
            "FILTER_BLOCKED",
            "FILTER_UNRESOLVED_DEFERRED",
            "FILTER_EXPIRED_NO_SEND",
            "SEND_REJECTED",
            "WINDOW_EXPIRED",
            "WINDOW_NOT_OPEN",
            "PREFIX_REQUIRED",
        }
        verified_terminal = {"CLOSED_SL", "CLOSED_TP", "REJECTED", "CANCELLED", "EXPIRED"}
        results: list[dict[str, object]] = []
        for order_id, status, comment, request_json, broker_ticket in rows:
            if str(status) in CANCEL_CONTROL_STATES:
                legacy_ticket, legacy_error = self._legacy_cancel_ticket(output_root, str(order_id))
                if legacy_error or (
                    broker_ticket is not None
                    and legacy_ticket is not None
                    and int(broker_ticket) != legacy_ticket
                ):
                    details = {
                        "strategy_order_id": str(order_id),
                        "broker_order_ticket": None if broker_ticket is None else int(broker_ticket),
                        "legacy_outbox_ticket": legacy_ticket,
                        "reason": legacy_error or "Ledger and legacy outbox cancellation tickets disagree.",
                        "checked_at": now.isoformat(),
                    }
                    self._record_broker_state(
                        output_root, str(order_id), "BROKER_REQUEST_MISMATCH_NO_SEND", details
                    )
                    results.append(
                        {
                            "order_id": str(order_id),
                            "state": "BROKER_REQUEST_MISMATCH_NO_SEND",
                            **details,
                        }
                    )
                    continue
                effective_ticket = (
                    None
                    if broker_ticket is None and legacy_ticket is None
                    else int(broker_ticket if broker_ticket is not None else legacy_ticket)
                )
                request = None
                if request_json:
                    try:
                        parsed_request = json.loads(str(request_json))
                        if isinstance(parsed_request, dict):
                            request = parsed_request
                    except (TypeError, ValueError, json.JSONDecodeError):
                        request = None
                results.append(
                    self._resolve_cancel_unknown(
                        output_root,
                        str(order_id),
                        str(comment),
                        effective_ticket,
                        now,
                        request=request,
                        control_status=str(status),
                    )
                )
                continue
            if str(status) in terminal or not request_json:
                continue
            stored = self._stored_broker_state(output_root, str(order_id))
            if stored is not None and stored[0] in verified_terminal:
                continue
            request = json.loads(str(request_json))
            state, details = self._broker_execution_chain(
                str(order_id),
                str(comment),
                request,
                None if broker_ticket is None else int(broker_ticket),
                now,
            )
            self._record_broker_state(output_root, str(order_id), state, details)
            if str(status) == "LINKED_EXISTING" and state in verified_terminal:
                self._transition_order_intent(
                    output_root,
                    str(order_id),
                    state,
                    {
                        "event": "LIFECYCLE_TERMINAL",
                        "order_id": str(order_id),
                        "broker_order_ticket": details.get("broker_order_ticket"),
                        "broker_execution_state": state,
                        "reason": "Linked economic exposure reached a verified terminal state.",
                    },
                    broker_ticket=(
                        int(details["broker_order_ticket"])
                        if details.get("broker_order_ticket") is not None
                        else None
                    ),
                )
            if str(status) in {"SEND_ARMED", "SEND_UNKNOWN", "SEND_PARTIAL"} and state in {
                "PENDING_CONFIRMED",
                "PARTIAL_FILL",
                "OPEN_PROTECTED",
            }:
                self._transition_order_intent(
                    output_root,
                    str(order_id),
                    "LINKED_EXISTING",
                    {
                        "event": "LINKED_EXISTING",
                        "order_id": str(order_id),
                        "broker_order_ticket": details.get("broker_order_ticket"),
                        "broker_state": state,
                        "reason": "Later broker evidence linked the original uncertain send.",
                    },
                    broker_ticket=(
                        int(details["broker_order_ticket"])
                        if details.get("broker_order_ticket") is not None
                        else None
                    ),
                )
            results.append({"order_id": str(order_id), "state": state, **details})
        return results

    def _persistent_cancel_controls(self, output_root: Path) -> list[dict[str, object]]:
        connection = self._ready_order_connection(output_root)
        try:
            rows = connection.execute(
                "SELECT order_id, status, comment, broker_ticket FROM order_intents "
                "WHERE status IN ('CANCEL_ARMED', 'CANCEL_ACKNOWLEDGED', "
                "'CANCEL_UNKNOWN', 'CANCEL_REJECTED') ORDER BY created_at"
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "order_id": str(order_id),
                "status": str(status),
                "comment": str(comment),
                "broker_ticket": None if ticket is None else int(ticket),
            }
            for order_id, status, comment, ticket in rows
        ]

    def _legacy_cancel_ticket(
        self, output_root: Path, order_id: str
    ) -> tuple[int | None, str | None]:
        """Recover a legacy cancellation ticket from immutable outbox evidence."""
        connection = self._ready_order_connection(output_root)
        try:
            rows = connection.execute(
                "SELECT event_json FROM order_event_outbox "
                "WHERE order_id = ? ORDER BY sequence DESC",
                (order_id,),
            ).fetchall()
        finally:
            connection.close()
        tickets: set[int] = set()
        for (event_json,) in rows:
            try:
                event = json.loads(str(event_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if str(event.get("event")) not in CANCEL_CONTROL_STATES:
                continue
            value = event.get("broker_order_ticket", event.get("ticket"))
            try:
                ticket = int(value)
            except (TypeError, ValueError):
                continue
            if ticket > 0:
                tickets.add(ticket)
        if len(tickets) > 1:
            return None, "Multiple cancellation tickets exist in legacy outbox evidence."
        return (next(iter(tickets)) if tickets else None), None

    def _arm_cancel_attempt(
        self,
        output_root: Path,
        order: Any,
        order_id: str,
        remove_request: dict[str, object],
        original_request: dict[str, object] | None,
        reason: str,
    ) -> dict[str, object]:
        """Atomically record the one permitted REMOVE attempt before SDK I/O."""
        ticket = int(order.ticket)
        comment = str(getattr(order, "comment", ""))
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT order_id, status, request_json, broker_ticket FROM order_intents "
                "WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is not None and row[3] is not None and int(row[3]) != ticket:
                raise core.CriticalLiveError(
                    f"{order_id}: pending order ticket conflicts with the idempotency ledger."
                )
            if row is None:
                matching = connection.execute(
                    "SELECT order_id, status, request_json, broker_ticket FROM order_intents "
                    "WHERE broker_ticket = ? ORDER BY created_at",
                    (ticket,),
                ).fetchall()
                if len(matching) > 1:
                    raise core.CriticalLiveError(
                        f"broker ticket {ticket}: multiple strategy intents claim one order."
                    )
                row = matching[0] if matching else None
            bound_order_id = str(row[0]) if row is not None else order_id
            status = str(row[1]) if row is not None else None
            stored_request = None
            if row is not None and row[2]:
                try:
                    parsed = json.loads(str(row[2]))
                    if isinstance(parsed, dict):
                        stored_request = parsed
                except (TypeError, ValueError, json.JSONDecodeError):
                    stored_request = None
            prior_status = status in CANCEL_CONTROL_STATES or status in {
                "CANCELLED",
                "EXPIRED",
                "REJECTED",
            }
            prior_events = connection.execute(
                "SELECT event_json FROM order_event_outbox WHERE order_id = ?",
                (bound_order_id,),
            ).fetchall()
            prior_event = False
            for (event_json,) in prior_events:
                try:
                    event = json.loads(str(event_json))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    str(event.get("event")) in CANCEL_CONTROL_STATES
                    and int(event.get("broker_order_ticket", event.get("ticket", 0)) or 0)
                    == ticket
                ):
                    prior_event = True
                    break
            original = stored_request or original_request
            if prior_status or prior_event:
                connection.commit()
                return {
                    "order_id": bound_order_id,
                    "status": status,
                    "request": original,
                    "prior_attempt": True,
                }
            now = core.utc_now().isoformat()
            event = {
                "event": "CANCEL_ARMED",
                "order_id": bound_order_id,
                "comment": comment,
                "broker_order_ticket": ticket,
                "ticket": ticket,
                "reason": reason,
                "request": original,
                "cancel_request": remove_request,
                "requested_volume": None if original is None else original.get("volume"),
                "position_id": getattr(order, "position_id", None),
                "position_ticket": getattr(order, "position_ticket", None),
                "send_started": False,
            }
            if row is None:
                connection.execute(
                    "INSERT INTO order_intents "
                    "(order_id, status, comment, request_json, broker_ticket, created_at, updated_at) "
                    "VALUES (?, 'CANCEL_ARMED', ?, ?, ?, ?, ?)",
                    (
                        bound_order_id,
                        comment,
                        None if original is None else core.canonical_json(original),
                        ticket,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE order_intents SET status = 'CANCEL_ARMED', "
                    "request_json = COALESCE(?, request_json), broker_ticket = ?, updated_at = ? "
                    "WHERE order_id = ?",
                    (
                        None if original is None else core.canonical_json(original),
                        ticket,
                        now,
                        bound_order_id,
                    ),
                )
            payload = {"recorded_at": now, "magic": self.magic, **event}
            self._insert_outbox(connection, bound_order_id, payload)
            connection.commit()
            return {
                "order_id": bound_order_id,
                "status": "CANCEL_ARMED",
                "request": original,
                "prior_attempt": False,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _persist_cancel_unresolved(
        self,
        output_root: Path,
        order_id: str,
        comment: str,
        ticket: int,
        control_status: str,
        reason: str,
    ) -> str:
        next_status = (
            "CANCEL_REJECTED"
            if control_status == "CANCEL_REJECTED"
            else "CANCEL_UNKNOWN"
        )
        current = self._intent_state(output_root, order_id)
        if current is None:
            self._adopt_order_intent(
                output_root,
                order_id,
                comment,
                next_status,
                {
                    "event": next_status,
                    "order_id": order_id,
                    "comment": comment,
                    "broker_order_ticket": ticket,
                    "ticket": ticket,
                    "detail": reason,
                },
                broker_ticket=ticket,
            )
        elif str(current["status"]) != next_status:
            self._transition_order_intent(
                output_root,
                order_id,
                next_status,
                {
                    "event": next_status,
                    "order_id": order_id,
                    "comment": comment,
                    "broker_order_ticket": ticket,
                    "ticket": ticket,
                    "detail": reason,
                },
                broker_ticket=ticket,
            )
        return next_status

    def _record_cancel_control_if_missing(
        self,
        output_root: Path,
        order_id: str,
        state: str,
        details: dict[str, object],
    ) -> None:
        """Keep an existing economic state; only create a control-only state if needed."""
        stored = self._stored_broker_state(output_root, order_id)
        if stored is None or stored[0] in CANCEL_CONTROL_STATES:
            self._record_broker_state(output_root, order_id, state, details)

    def _cancel_control_evidence(
        self,
        history_orders: list[Any],
        current_orders: list[Any],
        entry_deals: list[Any],
        request: dict[str, Any],
        broker_ticket: int,
    ) -> tuple[str, dict[str, object]] | None:
        """Prove the cancelled remainder independently from economic exposure."""
        if current_orders or not history_orders:
            return None
        history = history_orders[-1]
        state_map = {
            int(getattr(self.mt5, "ORDER_STATE_CANCELED", 2)): "CANCELLED",
            int(getattr(self.mt5, "ORDER_STATE_EXPIRED", 6)): "EXPIRED",
            int(getattr(self.mt5, "ORDER_STATE_REJECTED", 5)): "REJECTED",
        }
        terminal_state = state_map.get(int(getattr(history, "state", -1)))
        if terminal_state is None:
            return None
        requested = float(request.get("volume") or 0.0)
        initial = getattr(history, "volume_initial", None)
        if initial is None or abs(float(initial) - requested) > 1e-8:
            return "BROKER_REQUEST_MISMATCH_NO_SEND", {
                "broker_order_ticket": broker_ticket,
                "reason": "Cancellation history volume_initial disagrees with original request.",
            }
        entry_in = int(getattr(self.mt5, "DEAL_ENTRY_IN", 0))
        fills = [
            item
            for item in entry_deals
            if int(getattr(item, "entry", -1)) == entry_in
            and int(getattr(item, "order", 0) or 0) == broker_ticket
        ]
        filled = sum(float(getattr(item, "volume", 0.0) or 0.0) for item in fills)
        remainder = round(max(requested - filled, 0.0), 8)
        current_remainder = getattr(history, "volume_current", None)
        if current_remainder is not None and abs(float(current_remainder) - remainder) > 1e-8:
            return "BROKER_REQUEST_MISMATCH_NO_SEND", {
                "broker_order_ticket": broker_ticket,
                "requested_volume": requested,
                "entry_filled_volume": filled,
                "expected_cancelled_remainder": remainder,
                "history_volume_current": float(current_remainder),
                "reason": "Cancellation history volume_current does not balance the remainder.",
            }
        if terminal_state == "REJECTED" and fills:
            return "BROKER_REQUEST_MISMATCH_NO_SEND", {
                "broker_order_ticket": broker_ticket,
                "reason": "Rejected order history cannot also prove an entry fill.",
            }
        return "CANCEL_RESOLVED", {
            "broker_order_ticket": broker_ticket,
            "cancelled_order_state": terminal_state,
            "requested_volume": requested,
            "entry_filled_volume": filled,
            "cancelled_remainder_volume": remainder,
            "history_volume_current": (
                None if current_remainder is None else float(current_remainder)
            ),
            "entry_deal_tickets": [int(getattr(item, "ticket", 0) or 0) for item in fills],
        }

    def _resolve_cancel_unknown(
        self,
        output_root: Path,
        order_id: str,
        comment: str,
        broker_ticket: int | None,
        now: pd.Timestamp,
        *,
        request: dict[str, Any] | None = None,
        control_status: str | None = None,
    ) -> dict[str, object]:
        """Resolve cancellation control without overwriting economic evidence."""
        control_status = control_status or str(
            (self._intent_state(output_root, order_id) or {}).get(
                "status", "CANCEL_UNKNOWN"
            )
        )
        if broker_ticket is None:
            stored = self._stored_broker_state(output_root, order_id)
            if stored is not None:
                broker_ticket = int(stored[1].get("broker_order_ticket") or 0) or None
        if broker_ticket is None:
            return {
                "order_id": order_id,
                "ticket": None,
                "state": control_status,
                "reason": "cancel ticket missing",
            }
        try:
            current = list(self._mt5_collection("orders_get", ticket=broker_ticket))
            history = list(self._mt5_collection("history_orders_get", ticket=broker_ticket))
            deals = list(self._mt5_collection("history_deals_get", ticket=broker_ticket))
        except BrokerStateUnknownError as exc:
            details = {
                "broker_order_ticket": broker_ticket,
                "comment": comment,
                "reason": str(exc),
                "checked_at": now.isoformat(),
            }
            self._persist_cancel_unresolved(
                output_root, order_id, comment, broker_ticket, control_status, str(exc)
            )
            return {"order_id": order_id, "ticket": broker_ticket, "state": "CANCEL_UNKNOWN", **details}
        cancel_evidence = None
        if request is not None:
            cancel_evidence = self._cancel_control_evidence(
                history, current, deals, request, broker_ticket
            )
            if cancel_evidence is not None and cancel_evidence[0] == "BROKER_REQUEST_MISMATCH_NO_SEND":
                details = {
                    "broker_order_ticket": broker_ticket,
                    "comment": comment,
                    **cancel_evidence[1],
                    "checked_at": now.isoformat(),
                }
                self._record_broker_state(output_root, order_id, cancel_evidence[0], details)
                self._persist_cancel_unresolved(
                    output_root, order_id, comment, broker_ticket, control_status, details["reason"]
                )
                return {"order_id": order_id, "ticket": broker_ticket, "state": cancel_evidence[0], **details}
        if (current or deals) and request is not None:
            chain_state, chain_details = self._broker_execution_chain(
                order_id, comment, request, broker_ticket, now
            )
            self._record_broker_state(output_root, order_id, chain_state, chain_details)
            if cancel_evidence is not None and cancel_evidence[0] == "CANCEL_RESOLVED":
                resolved_details = {
                    **chain_details,
                    **cancel_evidence[1],
                    "cancel_resolution": "CANCEL_RESOLVED",
                    "cancel_control_independent_of_economic_state": True,
                }
                if chain_state in {"PARTIAL_FILL", "OPEN_PROTECTED"}:
                    self._transition_order_intent(
                        output_root,
                        order_id,
                        "LINKED_EXISTING",
                        {
                            "event": "CANCEL_RESOLVED",
                            "order_id": order_id,
                            "broker_order_ticket": broker_ticket,
                            "cancel_outcome": cancel_evidence[1]["cancelled_order_state"],
                            "cancelled_remainder_volume": cancel_evidence[1]["cancelled_remainder_volume"],
                            "entry_filled_volume": cancel_evidence[1]["entry_filled_volume"],
                            "position_id": chain_details.get("position_id"),
                            "broker_execution_state": chain_state,
                        },
                        broker_ticket=broker_ticket,
                    )
                    return {"order_id": order_id, "ticket": broker_ticket, "state": chain_state, **resolved_details}
            safe_terminal = {"CLOSED_SL", "CLOSED_TP", "REJECTED", "CANCELLED", "EXPIRED"}
            safe_linked = {"OPEN_PROTECTED"}
            if chain_state in safe_terminal:
                self._transition_order_intent(
                    output_root,
                    order_id,
                    chain_state,
                    {
                        "event": "CANCEL_RESOLVED",
                        "order_id": order_id,
                        "broker_order_ticket": broker_ticket,
                        "cancel_outcome": "FILLED_OR_TERMINAL",
                        "broker_execution_state": chain_state,
                        "details": chain_details,
                    },
                    broker_ticket=broker_ticket,
                )
                return {
                    "order_id": order_id,
                    "ticket": broker_ticket,
                    "state": chain_state,
                    "cancel_resolution": "FILLED_OR_TERMINAL",
                    **chain_details,
                }
            if chain_state in safe_linked:
                self._transition_order_intent(
                    output_root,
                    order_id,
                    "LINKED_EXISTING",
                    {
                        "event": "CANCEL_RESOLVED",
                        "order_id": order_id,
                        "broker_order_ticket": broker_ticket,
                        "cancel_outcome": "OPEN_POSITION",
                        "broker_execution_state": chain_state,
                        "details": chain_details,
                    },
                    broker_ticket=broker_ticket,
                )
                return {
                    "order_id": order_id,
                    "ticket": broker_ticket,
                    "state": chain_state,
                    "cancel_resolution": "OPEN_POSITION",
                    **chain_details,
                }
            details = {
                "broker_order_ticket": broker_ticket,
                "comment": comment,
                "reason": "REMOVE outcome remains economically unresolved.",
                "current_order_count": len(current),
                "entry_deal_count": len(deals),
                "checked_at": now.isoformat(),
                "broker_execution_state": chain_state,
                "broker_execution_details": chain_details,
            }
            next_status = self._persist_cancel_unresolved(
                output_root, order_id, comment, broker_ticket, control_status, details["reason"]
            )
            return {
                "order_id": order_id,
                "ticket": broker_ticket,
                "state": next_status,
                **details,
            }
        if current or deals:
            next_status = self._persist_cancel_unresolved(
                output_root,
                order_id,
                comment,
                broker_ticket,
                control_status,
                "Economic request_json is missing; no request was fabricated.",
            )
            return {
                "order_id": order_id,
                "ticket": broker_ticket,
                "state": next_status,
                "broker_order_ticket": broker_ticket,
                "comment": comment,
                "reason": "Economic request_json is missing; no request was fabricated.",
            }
        cancelled_states = {
            int(getattr(self.mt5, "ORDER_STATE_CANCELED", 2)),
            int(getattr(self.mt5, "ORDER_STATE_EXPIRED", 6)),
            int(getattr(self.mt5, "ORDER_STATE_REJECTED", 5)),
        }
        if not history or int(getattr(history[-1], "state", -1)) not in cancelled_states:
            details = {
                "broker_order_ticket": broker_ticket,
                "comment": comment,
                "reason": "No broker cancellation state is visible yet.",
                "history_order_count": len(history),
                "checked_at": now.isoformat(),
            }
            next_status = self._persist_cancel_unresolved(
                output_root, order_id, comment, broker_ticket, control_status, details["reason"]
            )
            if request is not None:
                self._record_cancel_control_if_missing(
                    output_root, order_id, next_status, details
                )
            return {"order_id": order_id, "ticket": broker_ticket, "state": next_status, **details}
        terminal_state = (
            "CANCELLED"
            if int(getattr(history[-1], "state", -1))
            == int(getattr(self.mt5, "ORDER_STATE_CANCELED", 2))
            else "EXPIRED"
            if int(getattr(history[-1], "state", -1))
            == int(getattr(self.mt5, "ORDER_STATE_EXPIRED", 6))
            else "REJECTED"
        )
        details = {
            "broker_order_ticket": broker_ticket,
            "comment": comment,
            "reason": f"Broker history confirms {terminal_state.lower()} and no entry deal is present.",
            "history_order_count": len(history),
            "checked_at": now.isoformat(),
            "broker_execution_state": terminal_state,
        }
        self._record_broker_state(output_root, order_id, terminal_state, details)
        self._transition_order_intent(
            output_root,
            order_id,
            terminal_state,
            {
                "event": "CANCEL_RESOLVED",
                "order_id": order_id,
                "broker_order_ticket": broker_ticket,
                "cancel_outcome": terminal_state,
                **details,
            },
            broker_ticket=broker_ticket,
        )
        return {"order_id": order_id, "ticket": broker_ticket, "state": terminal_state, **details}

    def _stored_broker_state(
        self, output_root: Path, order_id: str
    ) -> tuple[str, dict[str, object]] | None:
        connection = self._ready_order_connection(output_root)
        try:
            row = connection.execute(
                "SELECT state, details_json FROM broker_execution_states WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return str(row[0]), json.loads(str(row[1]))

    def _minimum_volume(self, symbol: str) -> float:
        info = self._retry_mt5_read("symbol_info", symbol)
        step = float(info.volume_step)
        minimum = float(info.volume_min)
        steps = math.ceil((minimum - 1e-12) / step)
        return round(steps * step, 8)

    @staticmethod
    def _derived_entry(decision: dict[str, Any], reward_r: float) -> float:
        stop = float(decision["stop_price"])
        target = float(decision["target_price"])
        if not math.isfinite(reward_r) or reward_r <= 0:
            raise CandidateNotExecutableError("INVALID_REWARD_R", "reward_r must be finite and positive")
        entry = (target + reward_r * stop) / (1.0 + reward_r)
        try:
            validate_trade_geometry(str(decision.get("direction") or ""), entry, stop, target)
        except ValueError as exc:
            raise CandidateNotExecutableError("INVALID_TRADE_GEOMETRY", str(exc)) from exc
        return entry

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
        info = self._retry_mt5_read("symbol_info", symbol)
        tick = self._retry_mt5_read("symbol_info_tick", symbol)
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
            try:
                return quantize_price(value, trade_tick_size)
            except FinancialMathError as exc:
                raise core.CriticalLiveError(f"{symbol}: invalid price quantization input.") from exc

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
        self.bind_output_root(output_root)
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
            info = self._retry_mt5_read("symbol_info", symbol)
            tick = self._retry_mt5_read("symbol_info_tick", symbol)
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
                        result = self._retry_mt5_read("order_check", request)
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

    def _candidate_send_context(
        self,
        decision: dict[str, Any],
        prefix_record: dict[str, Any] | None,
        now: pd.Timestamp | None = None,
    ) -> dict[str, object]:
        observed = core.utc_now() if now is None else now
        local = observed.tz_convert(core.TZ)
        if prefix_record is None:
            return {"state": "PREFIX_MISSING", "observed_at": observed.isoformat()}
        trade_date = str(prefix_record.get("date") or decision.get("date") or local.date())
        configs, _, _ = core.live_strategy_objects()
        leg_key = str(decision["leg_key"])
        if leg_key not in configs:
            raise core.CriticalLiveError(f"Unknown Super1 leg for send guard: {leg_key}")
        config = configs[leg_key]
        start = pd.Timestamp(f"{trade_date} {config.trade_window_start}", tz=core.TZ)
        end = pd.Timestamp(f"{trade_date} {config.trade_window_end}", tz=core.TZ)
        delay = int(self.config.get("closed_bar_delay_seconds", 8))
        fresh_cutoffs: dict[str, pd.Timestamp] = {}
        prefix_cutoffs: dict[str, pd.Timestamp] = {}
        try:
            for key in core.LEG_ORDER:
                leg_minutes = int(str(self.config["legs"][key]["timeframe"]).removesuffix("m"))
                leg_end = pd.Timestamp(
                    f"{trade_date} {configs[key].trade_window_end}", tz=core.TZ
                )
                fresh_cutoffs[key] = min(
                    core.closed_cutoff(observed, leg_minutes, delay), leg_end
                )
                prefix_cutoffs[key] = pd.Timestamp(
                    (prefix_record.get("cutoffs") or {})[key]
                )
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "state": "STALE_PREFIX",
                "reason_code": "PREFIX_CUTOFF_VECTOR_MISSING",
                "observed_at": observed.isoformat(),
                "error": str(exc),
            }
        fresh_cutoff = fresh_cutoffs[leg_key]
        prefix_cutoff = prefix_cutoffs[leg_key]
        context: dict[str, object] = {
            "observed_at": observed.isoformat(),
            "trade_date": trade_date,
            "local_now": local.isoformat(),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "prefix_cutoff": prefix_cutoff.isoformat(),
            "fresh_cutoff": fresh_cutoff.isoformat(),
            "prefix_cutoffs": {key: value.isoformat() for key, value in prefix_cutoffs.items()},
            "fresh_cutoffs": {key: value.isoformat() for key, value in fresh_cutoffs.items()},
            "decision_bar_time": decision.get("fvg_time"),
            "decision_known_time": decision.get("fvg_known_time"),
            "prefix_recorded_at": prefix_record.get("recorded_at"),
        }
        if local.date() != pd.Timestamp(trade_date).date() or local >= end:
            return {"state": "WINDOW_EXPIRED", **context}
        if local < start:
            return {"state": "WINDOW_NOT_OPEN", **context}
        if any(prefix_cutoffs[key] != fresh_cutoffs[key] for key in core.LEG_ORDER):
            return {"state": "STALE_PREFIX", **context}
        if end <= observed:
            return {"state": "WINDOW_EXPIRED", **context}
        context["expiration"] = int(end.tz_convert("UTC").timestamp())
        return {"state": "ALLOW", **context}

    def _load_prefix_record(self, path: Path) -> tuple[dict[str, Any], bytes]:
        cached_path = getattr(self, "_prefix_raw_path", None)
        cached_raw = getattr(self, "_prefix_raw_bytes", None)
        if cached_path == path and isinstance(cached_raw, bytes):
            raw = cached_raw
        else:
            raw = path.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise core.CriticalLiveError("Executable prefix JSON root must be an object.")
        return parsed, raw

    def _cancel_candidate_pending(
        self,
        output_root: Path,
        comment: str,
        reason: str,
        order_id: str,
    ) -> list[dict[str, object]]:
        cancelled = []
        for kind, item in self._broker_objects(comment):
            if kind == "ORDER":
                cancelled.append(self._remove_order(output_root, item, reason, order_id))
        return cancelled

    def _is_owned_pending_order(self, order: Any) -> bool:
        if int(getattr(order, "magic", -1)) != self.magic:
            return False
        comment = str(getattr(order, "comment", ""))
        prefix = str(self.config.get("order_comment_prefix", ""))
        return bool(prefix) and (
            comment == prefix
            or comment.startswith(f"{prefix}:")
            or comment.startswith(f"{prefix}-")
        )

    def _defer_pre_send(
        self,
        output_root: Path,
        order_id: str,
        comment: str,
        context: dict[str, object],
        reason_code: str = "STALE_PREFIX",
    ) -> None:
        event = {
            "event": "PRE_SEND_DEFERRED",
            "order_id": order_id,
            "comment": comment,
            "reason_code": reason_code,
            "send_started": False,
            **context,
        }
        intent = self._intent_state(output_root, order_id)
        if intent is None:
            self._adopt_order_intent(
                output_root,
                order_id,
                comment,
                "PRE_SEND_DEFERRED",
                event,
            )
        else:
            self._transition_order_intent(
                output_root,
                order_id,
                "PRE_SEND_DEFERRED",
                event,
            )

    def _final_send_gate(
        self,
        output_root: Path,
        order_id: str,
        request: dict[str, object],
        decision: dict[str, Any],
        symbol: str,
        final_context: dict[str, Any],
    ) -> None:
        """Strategy extension point immediately before SEND_ARMED."""
        del output_root, order_id, request, decision, symbol, final_context

    def _place_candidate(
        self,
        output_root: Path,
        decision: dict[str, Any],
        symbol: str,
        reward_r: float,
        *,
        prefix_record: dict[str, Any] | None = None,
        prefix_raw: bytes | None = None,
        send_now: pd.Timestamp | None = None,
    ) -> dict[str, object]:
        self.bind_output_root(output_root)
        order_id = str(decision["order_id"])
        leg_key = str(decision["leg_key"])
        comment = self._comment(leg_key, order_id)
        core.assert_no_send_sentinel_clear(output_root)

        def guard_now() -> pd.Timestamp:
            # send_now is an explicit controlled-clock injection for tests;
            # production reconcile calls leave it unset and read immediately.
            if callable(send_now):
                return pd.Timestamp(send_now())
            return core.utc_now() if send_now is None else send_now

        self._drain_order_outbox(output_root)
        intent = self._intent_state(output_root, order_id)
        events = [item for item in self._events(output_root) if item.get("order_id") == order_id]
        retryable_check = False
        retryable_pre_send = False
        retryable_intent = False
        if intent is not None:
            status = str(intent["status"])
            if status in {"SUBMITTED", "LINKED_EXISTING"}:
                return {
                    "state": "IDEMPOTENT_ALREADY_SUBMITTED",
                    "order_id": order_id,
                    "ticket": intent.get("broker_ticket"),
                }
            if status == "CHECK_RETRYABLE":
                retryable_check = True
            elif status == "PRE_SEND_DEFERRED":
                retryable_pre_send = True
            elif status == "SEND_ARMED":
                return {
                    "state": "IDEMPOTENT_SEND_ARMED_RECONCILE_REQUIRED",
                    "order_id": order_id,
                }
            elif status == "INTENT":
                retryable_intent = True
            else:
                return {
                    "state": f"IDEMPOTENT_{status}_NO_SEND",
                    "order_id": order_id,
                }
        broker: list[tuple[str, Any]] = []
        try:
            broker = self._broker_objects(comment)
        except BrokerStateUnknownError as exc:
            deferred_context = {
                "state": "BROKER_SDK_UNAVAILABLE",
                "observed_at": guard_now().isoformat(),
                "reason": str(exc),
            }
            self._defer_pre_send(
                output_root, order_id, comment, deferred_context, "BROKER_SDK_UNAVAILABLE"
            )
            return {
                "state": "PRE_SEND_DEFERRED_NO_SEND",
                "order_id": order_id,
                "reason_code": "BROKER_SDK_UNAVAILABLE",
            }
        if broker:
            if intent is not None:
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
            if not events:
                raise core.CriticalLiveError(
                    f"{order_id}: broker object exists without idempotency ledger."
                )
        if not retryable_check and not retryable_pre_send and not retryable_intent:
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
                if intent is None:
                    # Re-read the CAS row without invoking the overridable
                    # observation hook again; this second read is what makes
                    # the parallel-send race deterministic.
                    fresh_connection = self._ready_order_connection(output_root)
                    try:
                        fresh_row = fresh_connection.execute(
                            "SELECT status, comment, broker_ticket FROM order_intents WHERE order_id = ?",
                            (order_id,),
                        ).fetchone()
                    finally:
                        fresh_connection.close()
                    fresh_intent = None if fresh_row is None else {
                        "status": str(fresh_row[0]),
                        "comment": str(fresh_row[1]),
                        "broker_ticket": fresh_row[2],
                    }
                    if fresh_intent is not None:
                        intent = fresh_intent
                        fresh_status = str(fresh_intent["status"])
                        if fresh_status == "INTENT":
                            retryable_intent = True
                        elif fresh_status == "CHECK_RETRYABLE":
                            retryable_check = True
                        elif fresh_status == "PRE_SEND_DEFERRED":
                            retryable_pre_send = True
                        else:
                            return {
                                "state": f"IDEMPOTENT_{fresh_status}_NO_SEND",
                                "order_id": order_id,
                            }
                if retryable_intent or retryable_check or retryable_pre_send:
                    pass
                elif broker:
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
                else:
                    raise core.CriticalLiveError(
                        f"{order_id}: uncertain prior order intent; refusing duplicate."
                    )
            if broker:
                raise core.CriticalLiveError(
                    f"{order_id}: broker object exists without idempotency ledger."
                )
        initial_context = self._candidate_send_context(
            decision, prefix_record, guard_now()
        )
        if initial_context["state"] == "STALE_PREFIX":
            self._defer_pre_send(output_root, order_id, comment, initial_context)
            cancelled = self._cancel_candidate_pending(
                output_root, comment, "STALE_PREFIX", order_id
            )
            return {
                "state": "STALE_PREFIX_NO_SEND",
                "order_id": order_id,
                "reason_code": "STALE_PREFIX",
                "cancelled_pending": cancelled,
            }
        if initial_context["state"] in {"WINDOW_EXPIRED", "WINDOW_NOT_OPEN"}:
            reason_code = str(initial_context["state"])
            self._terminal_no_send(
                output_root,
                order_id,
                comment,
                reason_code,
                {
                    "event": reason_code,
                    "order_id": order_id,
                    "comment": comment,
                    "send_started": False,
                    **initial_context,
                },
            )
            cancelled = self._cancel_candidate_pending(
                output_root, comment, reason_code, order_id
            )
            return {
                "state": f"{reason_code}_NO_SEND",
                "order_id": order_id,
                "reason_code": reason_code,
                "cancelled_pending": cancelled,
            }
        if initial_context["state"] == "PREFIX_MISSING":
            self._terminal_no_send(
                output_root,
                order_id,
                comment,
                "PREFIX_REQUIRED",
                {
                    "event": "PREFIX_REQUIRED",
                    "order_id": order_id,
                    "comment": comment,
                    "send_started": False,
                    **initial_context,
                },
            )
            cancelled = self._cancel_candidate_pending(
                output_root, comment, "PREFIX_REQUIRED", order_id
            )
            return {
                "state": "PREFIX_REQUIRED_NO_SEND",
                "order_id": order_id,
                "reason_code": "PREFIX_REQUIRED",
                "cancelled_pending": cancelled,
            }
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
        if initial_context.get("expiration") is not None:
            request["expiration"] = int(initial_context["expiration"])
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
            "decision_produced_at": None if prefix_record is None else prefix_record.get("recorded_at"),
            "prefix_cutoff": initial_context.get("prefix_cutoff"),
            "data_cutoff": initial_context.get("fresh_cutoff"),
        }
        if retryable_pre_send:
            resumed = self._resume_pre_send_intent(
                output_root,
                order_id,
                request,
                {
                    "event": "INTENT_REEVALUATED",
                    "order_id": order_id,
                    "comment": comment,
                    "request": request,
                    "prefix_cutoff": initial_context.get("prefix_cutoff"),
                    "fresh_cutoff": initial_context.get("fresh_cutoff"),
                },
            )
            if not resumed["resumed"]:
                status = str(resumed.get("status") or "UNKNOWN")
                if status == "SUBMITTED":
                    state = "IDEMPOTENT_ALREADY_SUBMITTED"
                elif status == "LINKED_EXISTING":
                    state = "IDEMPOTENT_LINKED_EXISTING"
                elif status == "SEND_ARMED":
                    state = "IDEMPOTENT_SEND_ARMED_RECONCILE_REQUIRED"
                else:
                    state = f"IDEMPOTENT_{status}_NO_SEND"
                return {
                    "state": state,
                    "order_id": order_id,
                    "broker_ticket": resumed.get("broker_ticket"),
                }
        if not retryable_check and not retryable_pre_send and not retryable_intent:
            claimed = self._claim_order_intent(
                output_root,
                order_id,
                comment,
                request,
                intent_event,
            )
            if not claimed["claimed"]:
                if claimed["status"] not in {"INTENT", "CHECK_RETRYABLE", "PRE_SEND_DEFERRED"}:
                    return {
                        "state": f"IDEMPOTENT_{claimed['status']}_NO_SEND",
                        "order_id": order_id,
                    }
        check_error: str | None = None
        try:
            check = self._retry_mt5_read("order_check", request)
        except Exception as exc:
            check = None
            check_error = str(exc)
        try:
            check_retcode = None if check is None else int(check.retcode)
        except (AttributeError, TypeError, ValueError) as exc:
            check_retcode = None
            check_error = check_error or str(exc)
        if check_error is not None:
            self._transition_order_intent(
                output_root,
                order_id,
                "CHECK_RETRYABLE",
                {
                    "event": "CHECK_ERROR",
                    "order_id": order_id,
                    "comment": comment,
                    "retcode": None,
                    "reason": check_error,
                    "send_started": False,
                },
            )
            return {
                "state": "CHECK_RETRYABLE_NO_SEND",
                "order_id": order_id,
                "retcode": None,
                "reason": check_error,
            }
        if check is None or check_retcode != 0:
            retcode = check_retcode
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
                    "retcode": check_retcode,
                    "reason": str(self.mt5.last_error()) if check is None else str(getattr(check, "comment", "")),
                    },
                )
            return {
                "state": "CHECK_REJECTED_NO_SEND",
                "order_id": order_id,
                "retcode": check_retcode,
            }
        try:
            permission = self.order_permission_status()
        except XmMt5Error as exc:
            deferred_context = {
                "state": "BROKER_SDK_UNAVAILABLE",
                "observed_at": guard_now().isoformat(),
                "reason": str(exc),
            }
            self._defer_pre_send(
                output_root, order_id, comment, deferred_context, "BROKER_SDK_UNAVAILABLE"
            )
            return {
                "state": "PRE_SEND_DEFERRED_NO_SEND",
                "order_id": order_id,
                "reason_code": "BROKER_SDK_UNAVAILABLE",
            }
        if permission["state"] != "READY":
            return {
                "state": "ORDER_PERMISSION_DISABLED_NO_SEND",
                "order_id": order_id,
                "permission": permission,
            }
        persistent_cancel_controls = self._persistent_cancel_controls(output_root)
        if persistent_cancel_controls:
            return {
                "state": "BLOCKED_BY_PERSISTENT_CANCEL_NO_SEND",
                "order_id": order_id,
                "reason": "A durable cancellation attempt is unresolved.",
                "persistent_cancel_controls": persistent_cancel_controls,
            }
        final_context = self._candidate_send_context(decision, prefix_record, guard_now())
        if final_context["state"] == "STALE_PREFIX":
            self._defer_pre_send(output_root, order_id, comment, final_context)
            cancelled = self._cancel_candidate_pending(
                output_root, comment, "STALE_PREFIX", order_id
            )
            return {
                "state": "STALE_PREFIX_NO_SEND",
                "order_id": order_id,
                "reason_code": "STALE_PREFIX",
                "cancelled_pending": cancelled,
            }
        if final_context["state"] in {"WINDOW_EXPIRED", "WINDOW_NOT_OPEN"}:
            reason_code = str(final_context["state"])
            self._terminal_no_send(
                output_root,
                order_id,
                comment,
                reason_code,
                {
                    "event": reason_code,
                    "order_id": order_id,
                    "comment": comment,
                    "send_started": False,
                    **final_context,
                },
                request=request,
            )
            cancelled = self._cancel_candidate_pending(
                output_root, comment, reason_code, order_id
            )
            return {
                "state": f"{reason_code}_NO_SEND",
                "order_id": order_id,
                "reason_code": reason_code,
                "cancelled_pending": cancelled,
            }
        if final_context["state"] == "PREFIX_MISSING":
            self._terminal_no_send(
                output_root,
                order_id,
                comment,
                "PREFIX_REQUIRED",
                {
                    "event": "PREFIX_REQUIRED",
                    "order_id": order_id,
                    "comment": comment,
                    "send_started": False,
                    **final_context,
                },
                request=request,
            )
            return {
                "state": "PREFIX_REQUIRED_NO_SEND",
                "order_id": order_id,
                "reason_code": "PREFIX_REQUIRED",
            }
        if final_context["state"] != "ALLOW":
            raise core.CriticalLiveError(
                f"{order_id}: final send guard returned an unsupported state: {final_context['state']}"
            )
        if final_context.get("expiration") is not None:
            request["expiration"] = int(final_context["expiration"])
        send_started_at = pd.Timestamp(str(final_context["observed_at"]))
        self._record_intent_event(
            output_root,
            order_id,
            {
                "event": "CHECK_PASSED",
                "order_id": order_id,
                "comment": comment,
                "retcode": check_retcode,
                "send_guard": final_context,
            },
            request=request,
            allowed_statuses={"INTENT", "CHECK_RETRYABLE", "PRE_SEND_DEFERRED"},
        )
        self._final_send_gate(
            output_root,
            order_id,
            request,
            decision,
            symbol,
            final_context,
        )
        armed = self._arm_send(
            output_root,
            order_id,
            request,
            {
                "event": "SEND_ARMED",
                "order_id": order_id,
                "comment": comment,
                "send_attempted": False,
                "send_guard": final_context,
            },
        )
        if not armed.get("armed"):
            if armed.get("persistent_cancel_controls"):
                return {
                    "state": "BLOCKED_BY_PERSISTENT_CANCEL_NO_SEND",
                    "order_id": order_id,
                    "reason": str(armed.get("reason")),
                    "persistent_cancel_controls": armed["persistent_cancel_controls"],
                }
            status = str(armed.get("status") or "UNKNOWN")
            return {
                "state": f"IDEMPOTENT_{status}_NO_SEND",
                "order_id": order_id,
            }
        # SEND_ARMED is the durable last-write-before-send boundary.  Do not
        # drain the outbox, write files, read the broker, or recalculate time here.
        send_context = final_context
        try:
            result = self._order_send_checked(request)
        except core.CriticalLiveError:
            raise
        except Exception as exc:
            if getattr(self, "_strict_reconciliation", False):
                self._persist_unknown_send(
                    output_root,
                    order_id,
                    "BROKER_STATE_UNKNOWN_AFTER_SEND",
                    error=str(exc),
                )
            self._transition_order_intent(
                output_root,
                order_id,
                "SEND_UNKNOWN",
                {
                    "event": "SEND_UNKNOWN",
                    "order_id": order_id,
                    "comment": comment,
                    "reason": str(exc),
                    "send_started_at": send_started_at.isoformat(),
                    "response_at": core.utc_now().isoformat(),
                    "broker_execution_state": "UNKNOWN_NO_SEND",
                },
            )
            return {
                "state": "SEND_UNKNOWN_NO_SEND",
                "order_id": order_id,
                "reason": str(exc),
            }
        accepted = {
            int(getattr(self.mt5, "TRADE_RETCODE_PLACED", 10008)),
            int(getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)),
        }
        retcode = None
        try:
            retcode = int(result.retcode) if result is not None else None
        except (AttributeError, TypeError, ValueError):
            retcode = None
        partial_code = int(getattr(self.mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010))
        definitive_rejections = {
            int(getattr(self.mt5, "TRADE_RETCODE_REJECT", 10006)),
            int(getattr(self.mt5, "TRADE_RETCODE_CANCEL", 10007)),
            int(getattr(self.mt5, "TRADE_RETCODE_INVALID", 10013)),
            int(getattr(self.mt5, "TRADE_RETCODE_INVALID_VOLUME", 10014)),
            int(getattr(self.mt5, "TRADE_RETCODE_INVALID_PRICE", 10015)),
            int(getattr(self.mt5, "TRADE_RETCODE_INVALID_STOPS", 10016)),
            int(getattr(self.mt5, "TRADE_RETCODE_TRADE_DISABLED", 10017)),
            int(getattr(self.mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018)),
            int(getattr(self.mt5, "TRADE_RETCODE_INVALID_FILL", 10030)),
        }
        if retcode in accepted or (
            retcode == partial_code and getattr(self, "_strict_reconciliation", False)
        ):
            broker_ticket = int(getattr(result, "order", 0) or 0) or None
            broker_deal = int(getattr(result, "deal", 0) or 0) or None
            if getattr(self, "_strict_reconciliation", False):
                if broker_ticket is None:
                    reason = "Accepted MT5 result has no valid order ticket for exact readback."
                    self._persist_unknown_send(output_root, order_id, reason, retcode=retcode, deal=broker_deal)
                    self._transition_order_intent(
                        output_root,
                        order_id,
                        "SEND_UNKNOWN",
                        {
                            "event": "SEND_UNKNOWN",
                            "order_id": order_id,
                            "comment": comment,
                            "retcode": retcode,
                            "deal": broker_deal,
                            "reason": reason,
                            "broker_execution_state": "UNKNOWN_NO_SEND",
                        },
                    )
                    return {"state": "SEND_UNKNOWN_NO_SEND", "order_id": order_id, "retcode": retcode}
                try:
                    readback_state, readback = self._broker_execution_chain(
                        order_id,
                        comment,
                        request,
                        broker_ticket,
                        pd.Timestamp(str(send_context["observed_at"])),
                    )
                except Exception as exc:
                    readback_state, readback = "UNKNOWN_NO_SEND", {"reason": str(exc)}
                if readback_state in {"UNKNOWN_NO_SEND", "BROKER_REQUEST_MISMATCH_NO_SEND"}:
                    reason = str(readback.get("reason") or "Accepted MT5 result lacks exact broker readback.")
                    self._persist_unknown_send(
                        output_root,
                        order_id,
                        "BROKER_STATE_UNKNOWN_AFTER_SEND",
                        retcode=retcode,
                        broker_ticket=broker_ticket,
                        readback=readback,
                    )
                    self._transition_order_intent(
                        output_root,
                        order_id,
                        "SEND_UNKNOWN",
                        {
                            "event": "SEND_UNKNOWN",
                            "order_id": order_id,
                            "comment": comment,
                            "retcode": retcode,
                            "ticket": broker_ticket,
                            "reason": reason,
                            "broker_execution_state": "UNKNOWN_NO_SEND",
                            "readback": readback,
                        },
                        broker_ticket=broker_ticket,
                    )
                    return {"state": "SEND_UNKNOWN_NO_SEND", "order_id": order_id, "ticket": broker_ticket, "retcode": retcode}
                strict_status = (
                    "SEND_PARTIAL"
                    if retcode == partial_code and readback_state == "PARTIAL_FILL"
                    else "SUBMITTED"
                )
                strict_event = "SEND_PARTIAL" if strict_status == "SEND_PARTIAL" else "SUBMITTED"
                self._transition_order_intent(
                    output_root,
                    order_id,
                    strict_status,
                    {
                        "event": strict_event,
                        "order_id": order_id,
                        "comment": comment,
                        "ticket": broker_ticket,
                        "deal": broker_deal,
                        "retcode": retcode,
                        "result_class": "PARTIAL" if retcode == partial_code else "CONFIRMED",
                        "send_started_at": send_started_at.isoformat(),
                        "response_at": core.utc_now().isoformat(),
                        "broker_execution_state": readback_state,
                        "readback": readback,
                        "send_guard": send_context,
                    },
                    broker_ticket=broker_ticket,
                )
                return {"state": readback_state, "order_id": order_id, "ticket": broker_ticket}
            self._transition_order_intent(
                output_root,
                order_id,
                "SUBMITTED",
                {
                    "event": "SUBMITTED",
                    "order_id": order_id,
                    "comment": comment,
                    "ticket": broker_ticket,
                    "deal": int(getattr(result, "deal", 0) or 0) or None,
                    "retcode": retcode,
                    "send_started_at": send_started_at.isoformat(),
                    "response_at": core.utc_now().isoformat(),
                    "broker_execution_state": "UNCONFIRMED",
                    "send_guard": send_context,
                },
                broker_ticket=broker_ticket,
            )
            return {"state": "SUBMITTED", "order_id": order_id, "ticket": broker_ticket}
        if retcode == partial_code:
            status = "SEND_PARTIAL"
            event_name = "SEND_PARTIAL"
            broker_state = "PARTIAL_UNCONFIRMED"
        elif retcode in definitive_rejections:
            status = "SEND_REJECTED"
            event_name = "SEND_REJECTED"
            broker_state = "REJECTED"
        else:
            status = "SEND_UNKNOWN"
            event_name = "SEND_UNKNOWN"
            broker_state = "UNKNOWN_NO_SEND"
        broker_ticket = int(getattr(result, "order", 0) or 0) if result is not None else 0
        self._transition_order_intent(
            output_root,
            order_id,
            status,
            {
                "event": event_name,
                "order_id": order_id,
                "comment": comment,
                "retcode": retcode,
                "reason": (
                    str(self.mt5.last_error()) if result is None else str(getattr(result, "comment", ""))
                ),
                "send_started_at": send_started_at.isoformat(),
                "response_at": core.utc_now().isoformat(),
                "broker_execution_state": broker_state,
            },
            broker_ticket=broker_ticket or None,
        )
        if status == "SEND_REJECTED":
            return {"state": "SEND_REJECTED_NO_SEND", "order_id": order_id, "retcode": retcode}
        if status == "SEND_PARTIAL":
            return {"state": "SEND_PARTIAL_NO_SEND", "order_id": order_id, "retcode": retcode}
        return {"state": "SEND_UNKNOWN_NO_SEND", "order_id": order_id, "retcode": retcode}

    def _terminal_no_send(
        self,
        output_root: Path,
        order_id: str,
        comment: str,
        status: str,
        event: dict[str, object],
        request: dict[str, object] | None = None,
    ) -> None:
        if status not in {"WINDOW_EXPIRED", "WINDOW_NOT_OPEN", "PREFIX_REQUIRED"}:
            raise core.CriticalLiveError(f"{order_id}: invalid terminal no-send state {status}.")
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            payload_request = None if request is None else core.canonical_json(request)
            updated = connection.execute(
                "UPDATE order_intents SET status = ?, request_json = COALESCE(?, request_json), updated_at = ? "
                "WHERE order_id = ? AND status IN ('INTENT', 'CHECK_RETRYABLE', 'PRE_SEND_DEFERRED', 'SEND_ARMED')",
                (status, payload_request, now, order_id),
            ).rowcount
            inserted = False
            if updated == 0:
                inserted = bool(
                    connection.execute(
                        "INSERT OR IGNORE INTO order_intents "
                        "(order_id, status, comment, request_json, broker_ticket, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                        (order_id, status, comment, payload_request, now, now),
                    ).rowcount
                )
                current = connection.execute(
                    "SELECT status FROM order_intents WHERE order_id = ?", (order_id,)
                ).fetchone()
                if current is None or (
                    not inserted
                    and str(current[0])
                    not in {status, "WINDOW_EXPIRED", "WINDOW_NOT_OPEN", "PREFIX_REQUIRED"}
                ):
                    raise core.CriticalLiveError(
                        f"{order_id}: terminal no-send transition raced with a non-terminal intent."
                    )
            if updated or inserted:
                payload = {"recorded_at": now, "magic": self.magic, **event}
                self._insert_outbox(connection, order_id, payload)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)

    def _remove_order(
        self,
        output_root: Path,
        order: Any,
        reason: str,
        order_id: str | None = None,
    ) -> dict[str, object]:
        remove_request = {
            "action": self.mt5.TRADE_ACTION_REMOVE,
            "order": int(order.ticket),
            "magic": self.magic,
            "comment": str(getattr(order, "comment", "")),
        }
        if order_id is None:
            connection = self._ready_order_connection(output_root)
            try:
                row = connection.execute(
                    "SELECT order_id FROM order_intents WHERE comment = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (str(getattr(order, "comment", "")),),
                ).fetchone()
            finally:
                connection.close()
            order_id = None if row is None else str(row[0])
        bound_order_id = str(order_id or f"broker-cancel-{int(order.ticket)}")
        try:
            original_request = self._request_from_pending_order(order)
        except UnsafeOpenOrdersError:
            original_request = None
        armed = self._arm_cancel_attempt(
            output_root,
            order,
            bound_order_id,
            remove_request,
            original_request,
            reason,
        )
        bound_order_id = str(armed["order_id"])
        original_request = armed.get("request")
        control_status = str(armed.get("status") or "CANCEL_ARMED")
        if bool(armed.get("prior_attempt")):
            resolved = self._resolve_cancel_unknown(
                output_root,
                bound_order_id,
                str(getattr(order, "comment", "")),
                int(order.ticket),
                core.utc_now(),
                request=original_request if isinstance(original_request, dict) else None,
                control_status=control_status,
            )
            if resolved.get("state") == "CANCELLED":
                return {"ticket": int(order.ticket), "state": "CANCELLED", "reason": reason}
            return resolved
        # The durable ARM is committed before this is allowed to reach the SDK.
        self._drain_order_outbox(output_root)

        def persist_unknown(state: str, reason_text: str) -> dict[str, object]:
            next_status = self._persist_cancel_unresolved(
                output_root,
                bound_order_id,
                str(getattr(order, "comment", "")),
                int(order.ticket),
                state,
                reason_text,
            )
            details = {
                "broker_order_ticket": int(order.ticket),
                "comment": str(getattr(order, "comment", "")),
                "reason": reason_text,
                "cancel_reason": reason,
                "request": original_request,
            }
            self._record_cancel_control_if_missing(
                output_root, bound_order_id, next_status, details
            )
            return {
                "ticket": int(order.ticket),
                "state": next_status,
                "reason": reason,
                "broker_state": next_status,
            }

        try:
            result = self._order_send_checked(remove_request, require_open_permission=False)
        except core.CriticalLiveError:
            raise
        except Exception as exc:
            return persist_unknown("CANCEL_UNKNOWN", f"REMOVE exception: {exc}")
        accepted = int(getattr(self.mt5, "TRADE_RETCODE_DONE", 10009))
        try:
            retcode = None if result is None else int(result.retcode)
        except (AttributeError, TypeError, ValueError) as exc:
            return persist_unknown("CANCEL_UNKNOWN", f"REMOVE retcode unreadable: {exc}")
        if retcode != accepted:
            definitive = {
                int(getattr(self.mt5, "TRADE_RETCODE_REJECT", 10006)),
                int(getattr(self.mt5, "TRADE_RETCODE_INVALID", 10013)),
                int(getattr(self.mt5, "TRADE_RETCODE_INVALID_ORDER", 10035)),
            }
            state = "CANCEL_REJECTED" if retcode in definitive else "CANCEL_UNKNOWN"
            return persist_unknown(state, f"REMOVE retcode={retcode}")
        self._transition_order_intent(
            output_root,
            bound_order_id,
            "CANCEL_ACKNOWLEDGED",
            {
                "event": "CANCEL_ACKNOWLEDGED",
                "order_id": bound_order_id,
                "comment": str(getattr(order, "comment", "")),
                "broker_order_ticket": int(order.ticket),
                "ticket": int(order.ticket),
                "reason": reason,
                "retcode": retcode,
                "request": original_request,
                "cancel_request": remove_request,
            },
            broker_ticket=int(order.ticket),
        )
        resolved = self._resolve_cancel_unknown(
            output_root,
            bound_order_id,
            str(getattr(order, "comment", "")),
            int(order.ticket),
            core.utc_now(),
            request=original_request if isinstance(original_request, dict) else None,
            control_status="CANCEL_ACKNOWLEDGED",
        )
        if resolved.get("state") == "CANCELLED":
            return {"ticket": int(order.ticket), "state": "CANCELLED", "reason": reason}
        return resolved

    def cancel_all_pending(self, output_root: Path, reason: str) -> list[dict[str, object]]:
        self._ensure_demo()
        cancelled = []
        for order in self._mt5_collection("orders_get"):
            if self._is_owned_pending_order(order):
                cancelled.append(self._remove_order(output_root, order, reason))
        remaining = [
            order
            for order in self._mt5_collection("orders_get")
            if self._is_owned_pending_order(order)
        ]
        if remaining:
            unresolved_tickets = {
                int(item.get("ticket", item.get("broker_order_ticket", 0)) or 0)
                for item in cancelled
                if item.get("state") in CANCEL_CONTROL_STATES
            }
            if all(int(getattr(order, "ticket", 0) or 0) in unresolved_tickets for order in remaining):
                return cancelled
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
        cancel_controls_at_cycle_start = self._persistent_cancel_controls(output_root)
        synced_deals = self._sync_broker_events(output_root, now)
        broker_states = self._reconcile_persistent_intents(output_root, now)
        unknown_states = [
            item
            for item in broker_states
            if item["state"] in {"UNKNOWN_NO_SEND", "ENTRY_UNPROTECTED_NO_SEND"}
            or item.get("broker_execution_state") in {"UNKNOWN_NO_SEND", "ENTRY_UNPROTECTED_NO_SEND"}
        ]
        economic_unsafe_states = {
            "BROKER_REQUEST_MISMATCH_NO_SEND",
            "PROTECTION_MISMATCH_NO_SEND",
            "PARTIAL_FILL",
            "PARTIAL_EXIT_NO_SEND",
            "POSITION_VOLUME_MISMATCH_NO_SEND",
            "CLOSED_UNKNOWN",
        }
        unsafe_states = [
            item
            for item in broker_states
            if item["state"] in economic_unsafe_states
            or item.get("broker_execution_state") in economic_unsafe_states
        ]
        cleanup_reason = None
        if prefix.get("state") == "DATA_INVALID":
            cleanup_reason = "DATA_INVALID"
        elif prefix.get("state") not in {"VALID", "ALREADY_RECORDED"} or not prefix.get("path"):
            cleanup_reason = "NO_EXECUTABLE_PREFIX"
        elif unknown_states or unsafe_states:
            cleanup_reason = "BROKER_UNSAFE"
        cancelled_before_decision = (
            self.cancel_all_pending(output_root, cleanup_reason)
            if cleanup_reason is not None
            else []
        )
        cancel_unknown = [
            item
            for item in cancelled_before_decision
            if item.get("state") in {"CANCEL_UNKNOWN", "CANCEL_REJECTED"}
        ]
        if cancel_unknown:
            return {
                "state": "CANCEL_UNKNOWN_NO_SEND",
                "reason": "Pending REMOVE has no definitive broker outcome.",
                "broker_states": broker_states,
                "synced_deals": synced_deals,
                "cancelled": cancelled_before_decision,
            }
        if unsafe_states:
            return {
                "state": "BROKER_UNSAFE_NO_SEND",
                "reason": "Broker execution or protection evidence is not safe for a new order.",
                "broker_states": broker_states,
                "synced_deals": synced_deals,
                "cancelled": cancelled_before_decision,
            }
        persistent_cancel_controls = self._persistent_cancel_controls(output_root)
        if persistent_cancel_controls:
            return {
                "state": "CANCEL_UNKNOWN_NO_SEND",
                "reason": "A durable cancellation attempt has not reached a verified broker outcome.",
                "persistent_cancel_controls": persistent_cancel_controls,
                "cancel_controls_at_cycle_start": cancel_controls_at_cycle_start,
                "broker_states": broker_states,
                "synced_deals": synced_deals,
                "cancelled": cancelled_before_decision,
            }
        if unknown_states:
            return {
                "state": "UNKNOWN_NO_SEND",
                "reason": "One or more submitted intents lack definitive broker readback.",
                "broker_states": broker_states,
                "synced_deals": synced_deals,
                "cancelled": cancelled_before_decision,
            }
        if prefix.get("state") == "DATA_INVALID":
            return {
                "state": "DATA_INVALID_NO_SEND",
                "broker_states": broker_states,
                "synced_deals": synced_deals,
                "cancelled": cancelled_before_decision,
            }
        if prefix.get("state") not in {"VALID", "ALREADY_RECORDED"} or not prefix.get("path"):
            return {
                "state": "NO_EXECUTABLE_PREFIX",
                "prefix_state": prefix.get("state"),
                "cancelled": cancelled_before_decision,
            }
        record, prefix_raw = self._load_prefix_record(Path(prefix["path"]))
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
        # Build this before processing candidates.  If a prior candidate
        # becomes UNKNOWN/PARTIAL, a still-valid later candidate's pending
        # order must remain owned and must not be cancelled as stale.
        active_comments = {
            self._comment(str(item["leg_key"]), str(item["order_id"]))
            for item in candidates
        }
        new_entry_blocked = False
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
            if new_entry_blocked:
                results.append(
                    {
                        "state": "BLOCKED_BY_PRIOR_BROKER_UNCERTAINTY_NO_SEND",
                        "order_id": order_id,
                        "reason": "A prior candidate in this cycle returned UNKNOWN/PARTIAL.",
                    }
                )
                continue
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
            result = self._place_candidate(
                output_root,
                decision,
                str(runtime["legs"][leg_key]["epic"]),
                float(configs[leg_key].reward_r),
                prefix_record=record,
                prefix_raw=prefix_raw,
            )
            results.append(result)
            if result.get("state") in {
                "SEND_UNKNOWN_NO_SEND",
                "SEND_PARTIAL_NO_SEND",
            }:
                new_entry_blocked = True
        stale = []
        for order in self._mt5_collection("orders_get"):
            if self._is_owned_pending_order(order) and str(getattr(order, "comment", "")) not in active_comments:
                stale.append(self._remove_order(output_root, order, "NO_LONGER_ACTIVE_PREFIX"))
        return {
            "state": "RECONCILED",
            "known_time": now.isoformat(),
            "candidate_count": len(candidates),
            "pair_cap": pair_cap,
            "synced_deals": synced_deals,
            "broker_states": broker_states,
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
        self._record_health_checkpoint(output_root, report)
        path = output_root / "daily_health" / f"{trade_date}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        temp.replace(path)
        daily_log = output_root / "daily_health" / f"{trade_date}.jsonl"
        daily_temp = daily_log.with_name(f"{daily_log.name}.{uuid4().hex}.tmp")
        previous = daily_log.read_text(encoding="utf-8") if daily_log.exists() else ""
        daily_temp.write_text(previous + core.canonical_json(report) + "\n", encoding="utf-8")
        with daily_temp.open("rb") as handle:
            os.fsync(handle.fileno())
        self._replace_projection(daily_temp, daily_log)
        return {"state": "WRITTEN", "path": str(path)}

    def smoke_order(self, output_root: Path, lock: dict[str, Any]) -> dict[str, object]:
        self.bind_output_root(output_root)
        raise core.CriticalLiveError(
            "SMOKE is available only through the Super1 production order coordinator."
        )


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
    if core.runtime_config().get("execution") == "MT5_DEMO_ORDERS":
        raise core.CriticalLiveError(
            "base XM runner is read-only; broker writes require the canonical Super1 Mt5WritePort coordinator"
        )
    core.main()


if __name__ == "__main__":
    main()
