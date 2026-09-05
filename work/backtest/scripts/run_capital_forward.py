from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import date, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest.config import SymbolConfig
from backtest.engine_pipeline import EngineLeg, run_canonical_pair_pipeline, source_code_hash, stable_frame_hash
from backtest import manual_state as manual_state_module
from backtest.data_inspector import parse_timeframe_minutes
from backtest.gaps import find_gap_events
from backtest.integrity import DayDataIntegrity, assess_manual_state_day as baseline_assess_manual_state_day
from backtest.manual_state import ManualStateConfig
from backtest.state_audit import state_snapshot
from backtest.strategy import cluster_swings
from run_forward_shadow import (
    comparison_rows,
    decision_invariant_errors,
    engine_payload,
    frozen_config_hash,
    frozen_objects,
    object_hash,
)


TZ = "America/New_York"
UTC = "UTC"
PARENT_BASELINE = ROOT / "forward_shadow" / "baseline_lock.json"
RUNTIME_CONFIG = ROOT / "live_forward" / "capital_demo_config.json"
SCRIPT_PATH = Path(__file__).resolve()
HARNESS_PATHS = (SCRIPT_PATH,)
LEG_ORDER = ("nq", "spx")
REQUIRED_ENV = ("CAPITAL_IDENTIFIER", "CAPITAL_API_KEY", "CAPITAL_API_PASSWORD")
CREDENTIAL_PROVIDER: Any | None = None


class CriticalLiveError(RuntimeError):
    pass


class UnsafeStopError(CriticalLiveError):
    """Graceful shutdown could not prove broker-side safety."""

    pass


class TransientLiveError(RuntimeError):
    pass


class CapitalApiError(TransientLiveError):
    pass


FATAL_EXIT_STATUS = 78
_STOP_EVENT = threading.Event()


class OutputRootProcessLock:
    """Non-blocking, process-lifetime lock shared by every daemon for an output root."""

    def __init__(self, output_root: Path):
        self.path = output_root / "runtime" / "daemon.lock"
        self.handle: Any | None = None

    def acquire(self) -> "OutputRootProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise CriticalLiveError(
                f"Another forward daemon already owns output root {self.path.parent.parent}."
            ) from exc
        self.handle = handle
        handle.seek(0)
        handle.truncate()
        handle.write(
            canonical_json(
                {
                    "pid": os.getpid(),
                    "acquired_at": utc_now().isoformat(),
                    "output_root": str(self.path.parent.parent),
                }
            ).encode("utf-8")
        )
        handle.flush()
        os.fsync(handle.fileno())
        return self

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self) -> "OutputRootProcessLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def order_transport_present(runtime: dict[str, Any] | None = None) -> bool:
    effective = runtime or runtime_config()
    return effective.get("execution") == "MT5_DEMO_ORDERS"


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz=UTC)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def write_new_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        handle.write("\n")


def append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def runtime_config() -> dict[str, Any]:
    payload = read_json(RUNTIME_CONFIG)
    environment = payload.get("environment")
    if environment == "CAPITAL_DEMO_ONLY":
        expected_url = "https://demo-api-capital.backend-capital.com/api/v1"
        if payload.get("base_url") != expected_url:
            raise CriticalLiveError("Capital base URL is not the fixed demo endpoint.")
        if payload.get("feed") != "CAPITALCOM":
            raise CriticalLiveError("The live campaign feed must be CAPITALCOM.")
    elif environment in {"XM_MT5_DEMO_READ_ONLY", "XM_MT5_DEMO_ORDER"}:
        if payload.get("feed") != "XM_MT5":
            raise CriticalLiveError("The live campaign feed must be XM_MT5.")
        expected_mode = "DEMO_READ_ONLY" if environment.endswith("READ_ONLY") else "DEMO_ORDER"
        if payload.get("account_mode") != expected_mode:
            raise CriticalLiveError(f"XM account mode must remain {expected_mode}.")
    else:
        raise CriticalLiveError("Unsupported forward-shadow environment.")
    expected_execution = (
        "MT5_DEMO_ORDERS"
        if environment == "XM_MT5_DEMO_ORDER"
        else "SHADOW_ONLY_NO_ORDER_TRANSPORT"
    )
    if payload.get("execution") != expected_execution:
        raise CriticalLiveError(f"Execution must remain {expected_execution}.")
    return payload


def live_strategy_objects() -> tuple[dict[str, SymbolConfig], ManualStateConfig, dict[str, object]]:
    base_configs, state_config, frozen_payload = frozen_objects()
    runtime = runtime_config()
    configs: dict[str, SymbolConfig] = {}
    for key in LEG_ORDER:
        leg = runtime["legs"][key]
        if leg["timeframe"] != base_configs[key].timeframe:
            raise CriticalLiveError(f"{key}: timeframe differs from the frozen strategy.")
        configs[key] = replace(base_configs[key], symbol=leg["symbol"])
    live_payload = {
        "state": frozen_payload["state"],
        "legs": {key: asdict(configs[key]) for key in LEG_ORDER},
    }
    return configs, state_config, live_payload


def validate_parent_baseline() -> dict[str, Any]:
    lock = read_json(PARENT_BASELINE)
    manifest_path = ROOT / str(lock["baseline_manifest_path"])
    if not manifest_path.exists() or file_hash(manifest_path) != lock["baseline_manifest_sha256"]:
        raise CriticalLiveError("Parent reliability baseline changed or is missing.")
    if read_json(manifest_path) != lock["engine_manifest"]:
        raise CriticalLiveError("Parent reliability manifest content changed.")
    if source_code_hash() != lock["engine_manifest"]["code_hash"]:
        raise CriticalLiveError("Engine code hash differs from the approved parent baseline.")
    return lock


def harness_hash() -> str:
    digest = sha256()
    for path in (*HARNESS_PATHS, RUNTIME_CONFIG, PARENT_BASELINE):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def campaign_lock(output_root: Path) -> dict[str, Any]:
    parent = validate_parent_baseline()
    configs, _, payload = live_strategy_objects()
    runtime = runtime_config()
    path = output_root / "campaign_lock.json"
    expected = {
        "schema_version": 1,
        "campaign_id": None,
        "created_at": None,
        "parent_baseline_sha256": parent["baseline_manifest_sha256"],
        "engine_code_hash": parent["engine_manifest"]["code_hash"],
        "live_config_hash": frozen_config_hash(payload),
        "runtime_config_hash": file_hash(RUNTIME_CONFIG),
        "harness_hash": harness_hash(),
        "feed": runtime["feed"],
        "epics": {key: runtime["legs"][key]["epic"] for key in LEG_ORDER},
        "symbols": {key: configs[key].symbol for key in LEG_ORDER},
        "timeframes": {"nq": "3m", "spx": "5m", "htf": "15m"},
        "execution": runtime["execution"],
    }
    if runtime["feed"] == "XM_MT5":
        server = str(runtime.get("expected_server") or "").strip()
        supplied_server = os.environ.get("XM_MT5_SERVER", "").strip()
        if not server or (supplied_server and supplied_server != server):
            raise CriticalLiveError("XM broker identity must come from the signed runtime config.")
        expected["account_login"] = int(runtime["account_login"])
        expected["server"] = server
    if not path.exists():
        expected["campaign_id"] = str(uuid4())
        expected["created_at"] = utc_now().isoformat()
        write_new_json(path, expected)
        return expected
    current = read_json(path)
    if not isinstance(current.get("campaign_id"), str) or not current["campaign_id"].strip():
        raise CriticalLiveError("Existing campaign lock has no campaign_id; start a new clean state.")
    expected["campaign_id"] = current["campaign_id"]
    if {**current, "created_at": None} != expected:
        raise CriticalLiveError("Campaign code/config/selector lock changed; start a clean forward period.")
    return current


def credentials() -> dict[str, str] | None:
    if callable(CREDENTIAL_PROVIDER):
        provided = CREDENTIAL_PROVIDER()
        if provided is None:
            return None
        return {str(key): str(value) for key, value in provided.items()}
    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_ENV}
    if not all(values.values()):
        return None
    return values


class CapitalDemoClient:
    def __init__(self, config: dict[str, Any], secrets: dict[str, str]):
        self.base_url = str(config["base_url"]).rstrip("/")
        self.identifier = secrets["CAPITAL_IDENTIFIER"]
        self.api_key = secrets["CAPITAL_API_KEY"]
        self.api_password = secrets["CAPITAL_API_PASSWORD"]
        self.cst = ""
        self.security_token = ""

    def login(self) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}/session",
            data=json.dumps(
                {
                    "identifier": self.identifier,
                    "password": self.api_password,
                    "encryptedPassword": False,
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-CAP-API-KEY": self.api_key},
        )
        status, headers, payload = self._open(request)
        if status != 200:
            raise CapitalApiError(f"Capital demo login returned HTTP {status}.")
        self.cst = headers.get("CST", "")
        self.security_token = headers.get("X-SECURITY-TOKEN", "")
        if not self.cst or not self.security_token:
            raise CapitalApiError("Capital demo login did not return session tokens.")
        return payload

    def get(self, endpoint: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        if not self.cst:
            self.login()
        query = "" if not params else "?" + urlencode(params)
        request = Request(
            f"{self.base_url}/{endpoint.lstrip('/')}{query}",
            method="GET",
            headers={"CST": self.cst, "X-SECURITY-TOKEN": self.security_token},
        )
        try:
            status, _, payload = self._open(request)
        except CapitalApiError as exc:
            if "HTTP 401" not in str(exc):
                raise
            self.login()
            request = Request(
                f"{self.base_url}/{endpoint.lstrip('/')}{query}",
                method="GET",
                headers={"CST": self.cst, "X-SECURITY-TOKEN": self.security_token},
            )
            status, _, payload = self._open(request)
        if status != 200:
            raise CapitalApiError(f"Capital demo GET {endpoint} returned HTTP {status}.")
        return payload

    def prices(
        self,
        epic: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> tuple[pd.Timestamp, list[dict[str, Any]]]:
        payload = self.get(
            f"prices/{epic}",
            {
                "resolution": "MINUTE",
                "max": 1000,
                "from": start.tz_convert(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "to": end.tz_convert(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            },
        )
        return utc_now(), list(payload.get("prices") or [])

    def market(self, epic: str) -> dict[str, Any]:
        return self.get(f"markets/{epic}")

    @staticmethod
    def _open(request: Request) -> tuple[int, Any, dict[str, Any]]:
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return int(response.status), response.headers, payload
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise CapitalApiError(f"Capital API HTTP {exc.code}: {body[:300]}") from exc
        except (URLError, TimeoutError) as exc:
            raise CapitalApiError(f"Capital API connection error: {exc}") from exc


class BarStore:
    def __init__(self, output_root: Path):
        self.output_root = output_root
        self.db_path = output_root / "cache" / "capital_bars.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS minute_bars (
                epic TEXT NOT NULL,
                bar_time_utc TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                first_known_time_utc TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                PRIMARY KEY (epic, bar_time_utc)
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS conflicts (
                epic TEXT NOT NULL,
                bar_time_utc TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL,
                original_hash TEXT NOT NULL,
                observed_hash TEXT NOT NULL
            )
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def ingest(
        self,
        epic: str,
        known_time: pd.Timestamp,
        prices: Iterable[dict[str, Any]],
    ) -> dict[str, int]:
        inserted = duplicate = conflict = rejected = 0
        for item in prices:
            row = normalize_price(item)
            if row is None:
                rejected += 1
                continue
            payload_hash = object_hash(row)
            key = (epic, row["bar_time_utc"])
            current = self.db.execute(
                "SELECT payload_hash FROM minute_bars WHERE epic=? AND bar_time_utc=?",
                key,
            ).fetchone()
            if current is None:
                self.db.execute(
                    """
                    INSERT INTO minute_bars
                    (epic, bar_time_utc, open, high, low, close, volume, first_known_time_utc, payload_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        epic,
                        row["bar_time_utc"],
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["volume"],
                        known_time.isoformat(),
                        payload_hash,
                    ),
                )
                inserted += 1
            elif current[0] == payload_hash:
                duplicate += 1
            else:
                self.db.execute(
                    "INSERT INTO conflicts VALUES (?, ?, ?, ?, ?)",
                    (epic, row["bar_time_utc"], known_time.isoformat(), current[0], payload_hash),
                )
                conflict += 1
        self.db.commit()
        return {"inserted": inserted, "duplicates": duplicate, "conflicts": conflict, "rejected": rejected}

    def minute_frame(
        self,
        epic: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        knowledge_asof: pd.Timestamp,
    ) -> pd.DataFrame:
        """Return only rows known by the explicit causal knowledge cutoff."""
        knowledge_utc = pd.Timestamp(knowledge_asof).tz_convert(UTC)
        rows = self.db.execute(
            """
            SELECT bar_time_utc, open, high, low, close, volume, first_known_time_utc
            FROM minute_bars
            WHERE epic=? AND bar_time_utc>=? AND bar_time_utc<?
              AND first_known_time_utc<=?
            ORDER BY bar_time_utc
            """,
            (
                epic,
                start.tz_convert(UTC).isoformat(),
                end.tz_convert(UTC).isoformat(),
                knowledge_utc.isoformat(),
            ),
        ).fetchall()
        frame = pd.DataFrame(
            rows,
            columns=["time", "open", "high", "low", "close", "volume", "known_time"],
        )
        if frame.empty:
            return frame
        frame["time"] = pd.to_datetime(frame["time"], utc=True).dt.tz_convert(TZ)
        frame["known_time"] = pd.to_datetime(frame["known_time"], utc=True)
        if frame["known_time"].max() > knowledge_utc:
            raise CriticalLiveError("minute_frame returned data newer than knowledge_asof.")
        return frame

    def conflict_count(self, epic: str, start: pd.Timestamp, end: pd.Timestamp) -> int:
        row = self.db.execute(
            """
            SELECT COUNT(*) FROM conflicts
            WHERE epic=? AND bar_time_utc>=? AND bar_time_utc<?
            """,
            (epic, start.tz_convert(UTC).isoformat(), end.tz_convert(UTC).isoformat()),
        ).fetchone()
        return int(row[0])

    def future_known_summary(
        self, epic: str, start: pd.Timestamp, end: pd.Timestamp, knowledge_asof: pd.Timestamp
    ) -> dict[str, object]:
        row = self.db.execute(
            """
            SELECT COUNT(*), MAX(first_known_time_utc)
            FROM minute_bars
            WHERE epic=? AND bar_time_utc>=? AND bar_time_utc<?
              AND first_known_time_utc>?
            """,
            (
                epic,
                start.tz_convert(UTC).isoformat(),
                end.tz_convert(UTC).isoformat(),
                pd.Timestamp(knowledge_asof).tz_convert(UTC).isoformat(),
            ),
        ).fetchone()
        return {
            "excluded_future_known_count": int(row[0] or 0),
            "excluded_future_known_max": None if row[1] is None else str(row[1]),
        }

    def latest_time(self, epic: str) -> pd.Timestamp | None:
        row = self.db.execute(
            "SELECT MAX(bar_time_utc) FROM minute_bars WHERE epic=?",
            (epic,),
        ).fetchone()
        if not row or not row[0]:
            return None
        return pd.Timestamp(row[0])


def normalize_price(item: dict[str, Any]) -> dict[str, object] | None:
    timestamp = item.get("snapshotTimeUTC")
    try:
        bar_time = pd.Timestamp(timestamp, tz=UTC) if timestamp else pd.NaT
        values = {
            "open": float(item["openPrice"]["bid"]),
            "high": float(item["highPrice"]["bid"]),
            "low": float(item["lowPrice"]["bid"]),
            "close": float(item["closePrice"]["bid"]),
            "volume": float(item["lastTradedVolume"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if pd.isna(bar_time):
        return None
    if (
        values["volume"] < 0
        or values["high"] < values["low"]
        or not values["low"] <= values["open"] <= values["high"]
        or not values["low"] <= values["close"] <= values["high"]
    ):
        return None
    return {"bar_time_utc": bar_time.isoformat(), **values}


def aggregate_minutes(frame: pd.DataFrame, minutes: int, as_of: pd.Timestamp) -> pd.DataFrame:
    columns = ["time", "open", "high", "low", "close", "volume", "known_time", "source_minute_count"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    closed = frame[(frame["time"] + pd.Timedelta(minutes=1)) <= as_of.tz_convert(TZ)].copy()
    if closed.empty:
        return pd.DataFrame(columns=columns)
    closed["bucket"] = closed["time"].dt.floor(f"{minutes}min")
    grouped = closed.groupby("bucket", sort=True)
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        known_time=("known_time", "max"),
        source_minute_count=("time", "nunique"),
    )
    result = result.reset_index()
    result = result.rename(columns={"bucket": "time"})
    result = result[(result["time"] + pd.Timedelta(minutes=minutes)) <= as_of.tz_convert(TZ)]
    result["date"] = result["time"].dt.date
    return result


def scheduled_closed_minute(timestamp: pd.Timestamp, runtime: dict[str, Any]) -> bool:
    schedule = runtime.get("market_schedule_ny")
    if not schedule:
        return False
    local = timestamp.tz_convert(str(schedule.get("timezone") or TZ))
    clock = local.strftime("%H:%M")
    weekday = local.weekday()
    weekly_close = str(schedule["weekly_close_friday"])
    weekly_open = str(schedule["weekly_open_sunday"])
    if weekday == 5:
        return True
    if weekday == 4 and clock >= weekly_close:
        return True
    if weekday == 6 and clock < weekly_open:
        return True
    for start, end in schedule.get("daily_breaks", []):
        if start <= clock < end:
            return True
    for closure in schedule.get("planned_closures", []):
        if pd.Timestamp(closure["start"]).tz_convert(TZ) <= local < pd.Timestamp(closure["end"]).tz_convert(TZ):
            return True
    return False


def scheduled_closed_bucket(timestamp: pd.Timestamp, minutes: int, runtime: dict[str, Any]) -> bool:
    return all(
        scheduled_closed_minute(timestamp + pd.Timedelta(minutes=offset), runtime)
        for offset in range(minutes)
    )


def xm_assess_manual_state_day(
    frame: pd.DataFrame,
    trade_date: date,
    timeframe: str,
    profile_start: pd.Timestamp,
    trade_end: pd.Timestamp,
) -> DayDataIntegrity:
    result = baseline_assess_manual_state_day(frame, trade_date, timeframe, profile_start, trade_end)
    if result.valid or runtime_config().get("feed") != "XM_MT5":
        return result
    minutes = int(parse_timeframe_minutes(timeframe) or 5)
    relevant = frame[(frame["time"] >= profile_start) & (frame["time"] <= trade_end)].copy()
    gaps = {event.time.isoformat(): event for event in find_gap_events(relevant, expected_minutes=minutes)}
    retained = []
    for issue in result.issues:
        event = gaps.get(issue.time)
        if issue.code != "MISSING_BARS" or event is None:
            retained.append(issue)
            continue
        missing = pd.date_range(
            event.prev_time + pd.Timedelta(minutes=minutes),
            event.time - pd.Timedelta(minutes=minutes),
            freq=f"{minutes}min",
        )
        if not all(scheduled_closed_bucket(timestamp, minutes, runtime_config()) for timestamp in missing):
            retained.append(issue)
    return DayDataIntegrity(result.trade_date, not retained, tuple(retained))


def install_xm_scheduled_gap_integrity() -> None:
    manual_state_module.assess_manual_state_day = xm_assess_manual_state_day


def causal_swing_touch_evidence(
    frame: pd.DataFrame,
    trade_date: date,
    config: SymbolConfig,
) -> dict[str, int]:
    trade_start = pd.Timestamp(f"{trade_date} {config.trade_window_start}", tz=TZ)
    history = frame[frame["time"] < trade_start].tail(config.swing_lookback_candles).reset_index(drop=True)
    if len(history) < 5:
        return {}
    output: dict[str, int] = {}
    for side, price_field, comparator in (
        ("high", "high", "max"),
        ("low", "low", "min"),
    ):
        swings: list[tuple[pd.Timestamp, float]] = []
        for index in range(2, len(history) - 2):
            candle = history.iloc[index]
            left = history.iloc[index - 2:index]
            right = history.iloc[index + 1:index + 3]
            price = float(candle[price_field])
            if comparator == "max":
                selected = price > float(left[price_field].max()) and price >= float(right[price_field].max())
            else:
                selected = price < float(left[price_field].min()) and price <= float(right[price_field].min())
            if selected:
                swings.append((pd.Timestamp(candle.time), price))
        clustered = cluster_swings(swings, config.equal_swing_tolerance)
        if config.swing_liquidity_mode == "strong_only":
            clustered = [item for item in clustered if item[2] >= config.strong_swing_min_touches]
        for number, (_, _, touches) in enumerate(clustered[-5:], start=1):
            output[f"swing_{side}_{number}"] = int(touches)
    return output


def timestamp_ranges(values: list[pd.Timestamp], minutes: int) -> list[dict[str, object]]:
    if not values:
        return []
    ranges: list[dict[str, object]] = []
    first = previous = values[0]
    step = pd.Timedelta(minutes=minutes)
    for current in values[1:]:
        if current - previous != step:
            ranges.append(
                {
                    "first": first.isoformat(),
                    "last": previous.isoformat(),
                    "bar_count": int((previous - first) / step) + 1,
                }
            )
            first = current
        previous = current
    ranges.append(
        {
            "first": first.isoformat(),
            "last": previous.isoformat(),
            "bar_count": int((previous - first) / step) + 1,
        }
    )
    return ranges


def frame_gate(
    store: BarStore,
    epic: str,
    frame: pd.DataFrame,
    trade_date: date,
    cutoff: pd.Timestamp,
    minutes: int,
) -> dict[str, Any]:
    runtime = runtime_config()
    profile_start = pd.Timestamp(trade_date, tz=TZ) - pd.Timedelta(hours=6)
    issues: list[dict[str, object]] = []
    relevant = frame[(frame["time"] >= profile_start) & (frame["time"] < cutoff)].copy()
    expected = pd.date_range(profile_start, cutoff, freq=f"{minutes}min", inclusive="left")
    scheduled_closed = [
        item for item in expected if scheduled_closed_bucket(item, minutes, runtime)
    ]
    expected_open = [item for item in expected if item not in set(scheduled_closed)]
    actual = set(relevant["time"]) if not relevant.empty else set()
    missing = [item for item in expected_open if item not in actual]
    incomplete_source = (
        relevant[relevant["source_minute_count"] < minutes]
        if "source_minute_count" in relevant.columns
        else pd.DataFrame(columns=[*relevant.columns, "source_minute_count"])
    )
    tolerated_incomplete_source = incomplete_source.iloc[0:0]
    if not incomplete_source.empty:
        issues.append(
            {
                "code": "INCOMPLETE_SOURCE_MINUTES",
                "count": int(len(incomplete_source)),
                "first": pd.Timestamp(incomplete_source.iloc[0]["time"]).isoformat(),
                "last": pd.Timestamp(incomplete_source.iloc[-1]["time"]).isoformat(),
            }
        )
    tolerated_no_tick: list[pd.Timestamp] = []
    if missing:
        issues.append(
            {
                "code": "MISSING_BARS",
                "count": len(missing),
                "first": missing[0].isoformat(),
                "last": missing[-1].isoformat(),
            }
        )
    schedule_drift = [item for item in scheduled_closed if item in actual]
    if schedule_drift:
        issues.append(
            {
                "code": "SESSION_SCHEDULE_DRIFT",
                "count": len(schedule_drift),
                "first": schedule_drift[0].isoformat(),
                "last": schedule_drift[-1].isoformat(),
            }
        )
    conflicts = store.conflict_count(epic, profile_start, cutoff)
    if conflicts:
        issues.append({"code": "CONFLICTING_DUPLICATE_OHLC", "count": conflicts})
    if relevant.empty:
        issues.append({"code": "MISSING_PROFILE_DATA"})
    premarket_start = pd.Timestamp(f"{trade_date} 09:15", tz=TZ)
    premarket_end = pd.Timestamp(f"{trade_date} 09:30", tz=TZ)
    if relevant[(relevant["time"] >= premarket_start) & (relevant["time"] < premarket_end)].empty:
        issues.append({"code": "MISSING_PREMARKET_DATA"})
    return {
        "state": "VALID" if not issues else "DATA_INVALID",
        "feed": runtime["feed"],
        "epic": epic,
        "timeframe": f"{minutes}m",
        "cutoff": cutoff.isoformat(),
        "closed_bar_only": True,
        "issues": issues,
        "tolerated_no_tick_bar_count": len(tolerated_no_tick),
        "tolerated_no_tick_ranges": timestamp_ranges(tolerated_no_tick, minutes),
        "tolerated_incomplete_source_bar_count": int(len(tolerated_incomplete_source)),
        "tolerated_incomplete_source_ranges": timestamp_ranges(
            list(tolerated_incomplete_source["time"]), minutes
        ),
        "scheduled_closed_bar_count": len(scheduled_closed),
        "scheduled_closed_ranges": timestamp_ranges(scheduled_closed, minutes),
        "schedule_evidence": runtime.get("market_schedule_ny", {}).get("evidence"),
        "data_known_at": (
            None
            if relevant.empty or "known_time" not in relevant.columns
            else pd.Timestamp(relevant["known_time"].max()).isoformat()
        ),
        "data_hash": stable_frame_hash(relevant[["time", "open", "high", "low", "close", "volume"]]),
    }


def closed_cutoff(now: pd.Timestamp, minutes: int, delay_seconds: int) -> pd.Timestamp:
    safe = now.tz_convert(TZ) - pd.Timedelta(seconds=delay_seconds)
    return safe.floor(f"{minutes}min")


def campaign_session_eligible(now: pd.Timestamp, lock: dict[str, Any]) -> bool:
    trade_date = now.tz_convert(TZ).date()
    profile_start = pd.Timestamp(trade_date, tz=TZ) - pd.Timedelta(hours=6)
    return pd.Timestamp(lock["created_at"]).tz_convert(TZ) <= profile_start


def manual_file(output_root: Path, trade_date: date) -> Path:
    return output_root / "manual" / f"{trade_date}.json"


def record_manual(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    campaign_lock(output_root)
    trade_date = date.fromisoformat(args.date)
    path = manual_file(output_root, trade_date)
    if path.exists():
        raise CriticalLiveError(f"Manual record is append-only and already exists: {path}")
    legs = {}
    for key in LEG_ORDER:
        final = getattr(args, f"{key}_final")
        direction = getattr(args, f"{key}_direction")
        legs[key] = {
            "final": None if final == "UNKNOWN" else final,
            "direction": None if direction == "UNKNOWN" else direction,
            "explanation": getattr(args, f"{key}_explanation") or None,
        }
    payload: dict[str, object] = {
        "schema_version": 1,
        "date": str(trade_date),
        "recorded_at": utc_now().isoformat(),
        "author": args.author,
        "blind_to_engine_result": True,
        "legs": legs,
    }
    payload["record_hash"] = object_hash(payload)
    write_new_json(path, payload)
    print(f"SEALED {path}")


def load_manual(output_root: Path, trade_date: date) -> dict[str, Any] | None:
    path = manual_file(output_root, trade_date)
    if not path.exists():
        return None
    payload = read_json(path)
    claimed = payload.pop("record_hash", None)
    if object_hash(payload) != claimed:
        raise CriticalLiveError("Manual decision seal is invalid.")
    payload["record_hash"] = claimed
    return payload


def fetch_window(
    client: CapitalDemoClient,
    store: BarStore,
    output_root: Path,
    epic: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, object]:
    totals = {"inserted": 0, "duplicates": 0, "conflicts": 0, "rejected": 0}
    cursor = start.tz_convert(UTC)
    end_utc = end.tz_convert(UTC)
    provider_observations: list[pd.Timestamp] = []
    while cursor < end_utc:
        touch_health(output_root, "FETCHING_MARKET_DATA")
        chunk_end = min(cursor + pd.Timedelta(hours=12), end_utc)
        known_time, prices = client.prices(epic, cursor, chunk_end)
        # Keep the provider's real observation time.  The requested endpoint
        # remains the independent market-data-as-of/fetch-scope boundary.
        known_time = pd.Timestamp(known_time).tz_convert(UTC)
        provider_observations.append(known_time)
        result = store.ingest(epic, known_time, prices)
        for key, value in result.items():
            totals[key] += value
        append_jsonl(
            output_root / "raw" / known_time.strftime("%Y-%m-%d") / "fetches.jsonl",
            {
                "known_time": known_time.isoformat(),
                "provider_observation_at": known_time.isoformat(),
                "market_data_asof": end_utc.isoformat(),
                "epic": epic,
                "from": cursor.isoformat(),
                "to": chunk_end.isoformat(),
                "received": len(prices),
                "verified_no_tick_minutes": sum(bool(item.get("verifiedNoTick")) for item in prices),
                "ingest": result,
                "payload_hash": object_hash(prices),
            },
        )
        touch_health(output_root, "FETCHING_MARKET_DATA")
        cursor = chunk_end
    latest = store.latest_time(epic)
    return {
        **totals,
        "latest_bar_utc": None if latest is None else latest.isoformat(),
        "market_data_asof": end_utc.isoformat(),
        "fetch_scope_end": end_utc.isoformat(),
        "provider_observation_at": (
            None
            if not provider_observations
            else max(provider_observations).isoformat()
        ),
        "data_known_at": (
            None
            if not provider_observations
            else max(provider_observations).isoformat()
        ),
    }


def build_frames(
    store: BarStore,
    runtime: dict[str, Any],
    market_data_asof: pd.Timestamp,
    knowledge_asof: pd.Timestamp,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.Timestamp]]:
    start = market_data_asof.tz_convert(TZ).normalize() - pd.Timedelta(days=int(runtime["history_days"]))
    end = market_data_asof.tz_convert(TZ) + pd.Timedelta(minutes=1)
    frames: dict[str, pd.DataFrame] = {}
    cutoffs: dict[str, pd.Timestamp] = {}
    for key in LEG_ORDER:
        minutes = int(str(runtime["legs"][key]["timeframe"]).removesuffix("m"))
        epic = runtime["legs"][key]["epic"]
        minute = store.minute_frame(epic, start, end, knowledge_asof)
        cutoff = closed_cutoff(market_data_asof, minutes, int(runtime["closed_bar_delay_seconds"]))
        frames[key] = aggregate_minutes(minute, minutes, cutoff)
        cutoffs[key] = cutoff
    return frames, cutoffs


def snapshot_path(output_root: Path, trade_date: date, cutoffs: dict[str, pd.Timestamp]) -> Path:
    key = "_".join(cutoffs[leg].strftime("%H%M") for leg in LEG_ORDER)
    return output_root / "prefix" / str(trade_date) / f"{key}.json"


def invalid_payload(
    trade_date: date,
    gates: dict[str, dict[str, Any]],
    known_time: pd.Timestamp,
) -> dict[str, object]:
    days = []
    events = []
    for sequence, key in enumerate(LEG_ORDER, 1):
        reasons = [str(issue["code"]) for issue in gates[key]["issues"]]
        days.append(
            {
                "leg_key": key,
                "date": str(trade_date),
                "data_state": "INVALID",
                "data_reasons": reasons,
                "vah": None,
                "val": None,
                "premarket_context": None,
                "liquidity": None,
                "controlling_array_id": None,
                "no_trade_reason": "DATA_INVALID:" + "|".join(reasons),
            }
        )
        events.append(
            {
                "sequence": sequence,
                "leg_key": key,
                "event_type": "DATA_GATE",
                "bar_time": gates[key]["cutoff"],
                "known_time": known_time.isoformat(),
                "entity_id": "",
                "state": "DATA_INVALID",
                "direction": "",
                "reason": "|".join(reasons),
                "details": {"closed_bar_only": True, "issues": gates[key]["issues"]},
            }
        )
    return {"decisions": [], "events": events, "lifecycle": {}, "days": days}


def run_prefix(
    output_root: Path,
    store: BarStore,
    market_data_asof: pd.Timestamp,
    knowledge_asof: pd.Timestamp,
) -> dict[str, Any]:
    runtime = runtime_config()
    configs, state_config, _ = live_strategy_objects()
    trade_date = market_data_asof.tz_convert(TZ).date()
    if pd.Timestamp(trade_date).weekday() >= 5:
        return {"state": "OUTSIDE_TRADE_WINDOW", "reason": "WEEKEND"}
    market_data_utc = pd.Timestamp(market_data_asof).tz_convert(UTC)
    observed_knowledge_asof = pd.Timestamp(knowledge_asof).tz_convert(UTC)
    decision_produced_at = pd.Timestamp(utc_now()).tz_convert(UTC)
    if not market_data_utc <= observed_knowledge_asof <= decision_produced_at:
        raise CriticalLiveError(
            "Prefix causality violation: market_data_asof <= knowledge_asof <= decision_produced_at is required."
        )
    frames, cutoffs = build_frames(store, runtime, market_data_asof, observed_knowledge_asof)
    cutoffs = {
        key: min(cutoffs[key], pd.Timestamp(f"{trade_date} {configs[key].trade_window_end}", tz=TZ))
        for key in LEG_ORDER
    }
    path = snapshot_path(output_root, trade_date, cutoffs)
    if path.exists():
        return {"state": "ALREADY_RECORDED", "path": str(path)}

    gates: dict[str, dict[str, Any]] = {}
    active = False
    prefix_configs: dict[str, SymbolConfig] = {}
    for key in LEG_ORDER:
        minutes = int(runtime["legs"][key]["timeframe"].removesuffix("m"))
        start = pd.Timestamp(f"{trade_date} {configs[key].trade_window_start}", tz=TZ)
        frozen_end = pd.Timestamp(f"{trade_date} {configs[key].trade_window_end}", tz=TZ)
        cutoff = min(cutoffs[key], frozen_end)
        prefix_configs[key] = replace(configs[key], trade_window_end=cutoff.strftime("%H:%M"))
        gates[key] = frame_gate(
            store,
            runtime["legs"][key]["epic"],
            frames[key],
            trade_date,
            cutoff,
            minutes,
        )
        active = active or cutoff > start

    if not active:
        return {"state": "OUTSIDE_TRADE_WINDOW"}

    valid = all(gate["state"] == "VALID" for gate in gates.values())
    snapshots: dict[str, object] = {}
    payload: dict[str, object] = invalid_payload(trade_date, gates, utc_now())
    deterministic = True
    invariant_errors: list[str] = []
    if valid:
        legs = [EngineLeg(key, frames[key], prefix_configs[key]) for key in LEG_ORDER]
        first = run_canonical_pair_pipeline(legs, [trade_date], state_config=state_config)
        second = run_canonical_pair_pipeline(legs, [trade_date], state_config=state_config)
        first_payload = engine_payload(first, prefix_configs)
        second_payload = engine_payload(second, prefix_configs)
        deterministic = object_hash(first_payload) == object_hash(second_payload)
        payload = first_payload
        invariant_errors = decision_invariant_errors(payload["decisions"], payload["events"])
        snapshots = {
            key: state_snapshot(first.leg_results[key].days[0], cutoffs[key])
            for key in LEG_ORDER
        }
        graph_features = {
            key: {
                "swing_touches": causal_swing_touch_evidence(
                    frames[key], trade_date, prefix_configs[key]
                )
            }
            for key in LEG_ORDER
        }
    else:
        graph_features = {}
    state = "CRITICAL_STOP" if (not deterministic or invariant_errors) else ("VALID" if valid else "DATA_INVALID")
    decision_produced_at_text = decision_produced_at.isoformat()
    record = {
        "schema_version": 1,
        "market_data_asof": market_data_asof.isoformat(),
        "knowledge_asof": observed_knowledge_asof.isoformat(),
        "recorded_at": decision_produced_at_text,
        "decision_produced_at": decision_produced_at_text,
        "date": str(trade_date),
        "state": state,
        "cutoffs": {key: value.isoformat() for key, value in cutoffs.items()},
        "data_gates": gates,
        "closed_bar_only": True,
        "order_transport_present": order_transport_present(runtime),
        "deterministic_rerun": deterministic,
        "invariant_errors": invariant_errors,
        "data_known_at": {
            key: gates[key].get("data_known_at") for key in LEG_ORDER
        },
        "snapshots": snapshots,
        "graph_features": graph_features,
        "payload": payload,
        "hashes": {
            "code": source_code_hash(),
            "config": campaign_lock(output_root)["live_config_hash"],
            "data": {key: gates[key]["data_hash"] for key in LEG_ORDER},
            "result": object_hash(payload),
        },
    }
    write_new_json(path, record)
    if state == "CRITICAL_STOP":
        raise CriticalLiveError(f"Critical prefix audit failure: {path}")
    return {"state": state, "path": str(path)}


def session_attempt_dir(output_root: Path, trade_date: date) -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
    return output_root / "sessions" / str(trade_date) / f"{stamp}_{uuid4().hex[:8]}"


def snapshots_equal(expected: object, observed: object) -> bool:
    return object_hash(expected) == object_hash(observed)


def finalize_session(
    output_root: Path,
    store: BarStore,
    *,
    market_data_asof: pd.Timestamp,
    knowledge_asof: pd.Timestamp,
    finalization_asof: pd.Timestamp,
    fetches: dict[str, dict[str, object]],
) -> dict[str, Any]:
    runtime = runtime_config()
    configs, state_config, _ = live_strategy_objects()
    market_data_utc = pd.Timestamp(market_data_asof).tz_convert(UTC)
    knowledge_utc = pd.Timestamp(knowledge_asof).tz_convert(UTC)
    finalization_utc = pd.Timestamp(finalization_asof).tz_convert(UTC)
    decision_produced_at = pd.Timestamp(utc_now()).tz_convert(UTC)
    trade_date = finalization_asof.tz_convert(TZ).date()
    if pd.Timestamp(trade_date).weekday() >= 5:
        return {"state": "NON_TRADING_DAY", "reason": "WEEKEND"}
    marker = output_root / "finalized" / f"{trade_date}.json"
    if marker.exists():
        return {"state": "ALREADY_FINALIZED", "path": str(marker)}
    final_ready = max(
        pd.Timestamp(f"{trade_date} {configs[key].trade_window_end}", tz=TZ)
        + pd.Timedelta(minutes=int(runtime["legs"][key]["timeframe"].removesuffix("m")))
        for key in LEG_ORDER
    )
    if finalization_asof.tz_convert(TZ) < final_ready + pd.Timedelta(seconds=int(runtime["closed_bar_delay_seconds"])):
        return {"state": "SESSION_OPEN"}

    shared_asof = market_data_asof
    # Final data must cover the frozen window end, and the shared observation
    # must have made that last bucket usable under the closed-bar delay.
    required_cutoffs = {
        key: pd.Timestamp(f"{trade_date} {configs[key].trade_window_end}", tz=TZ)
        for key in LEG_ORDER
    }
    closed_cutoffs = {
        key: min(
            closed_cutoff(
                shared_asof,
                int(runtime["legs"][key]["timeframe"].removesuffix("m")),
                int(runtime["closed_bar_delay_seconds"]),
            ),
            required_cutoffs[key],
        )
        for key in LEG_ORDER
    }
    missing_closed = {
        key: closed_cutoffs[key].isoformat()
        for key in LEG_ORDER
        if closed_cutoffs[key] < required_cutoffs[key]
    }
    if fetches is not None:
        fetch_scope_ends = {
            key: (
                None
                if not isinstance(fetches.get(key), dict)
                else fetches[key].get("fetch_scope_end")
            )
            for key in LEG_ORDER
        }
        metadata_missing = {
            key: [field for field in ("provider_observation_at", "data_known_at")
                  if not isinstance(fetches.get(key), dict) or fetches[key].get(field) is None]
            for key in LEG_ORDER
        }
        metadata_missing = {key: fields for key, fields in metadata_missing.items() if fields}
        metadata_before_market: dict[str, dict[str, str]] = {}
        for key in LEG_ORDER:
            item = fetches.get(key)
            if not isinstance(item, dict):
                continue
            for field in ("provider_observation_at", "data_known_at"):
                value = item.get(field)
                if value is not None and pd.Timestamp(value).tz_convert(UTC) < market_data_utc:
                    metadata_before_market.setdefault(key, {})[field] = str(value)
        missing_scope = {
            key: None if value is None else str(value)
            for key, value in fetch_scope_ends.items()
            if value is None or pd.Timestamp(value) < required_cutoffs[key].tz_convert(UTC)
        }
        future_metadata: dict[str, dict[str, str]] = {}
        for key in LEG_ORDER:
            item = fetches.get(key)
            if not isinstance(item, dict):
                continue
            for field in ("provider_observation_at", "data_known_at"):
                value = item.get(field)
                if value is not None and pd.Timestamp(value).tz_convert(UTC) > knowledge_utc:
                    future_metadata.setdefault(key, {})[field] = str(value)
        if future_metadata:
            future_values = [value for fields in future_metadata.values() for value in fields.values()]
            return {
                "state": "WAIT_FOR_KNOWLEDGE",
                "knowledge_asof": knowledge_utc.isoformat(),
                "excluded_future_known_count": len(future_values),
                "excluded_future_known_max": max(future_values),
                "excluded_future_known_by_leg": future_metadata,
            }
        if metadata_missing or metadata_before_market or missing_scope or missing_closed or not market_data_utc <= knowledge_utc <= finalization_utc <= decision_produced_at:
            return {
                "state": "WAIT_FOR_FINAL_DATA",
                "market_data_asof": shared_asof.isoformat(),
                "required_cutoffs": {
                    key: value.isoformat() for key, value in required_cutoffs.items()
                },
                "fetch_scope_ends": {
                    key: None if value is None else str(value)
                    for key, value in fetch_scope_ends.items()
                },
                "missing_scope": missing_scope,
                "missing_metadata": metadata_missing,
                "metadata_before_market": metadata_before_market,
                "closed_cutoffs": {
                    key: value.isoformat() for key, value in closed_cutoffs.items()
                },
                "missing_closed_cutoff": missing_closed,
            }
    else:
        metadata_missing = {
            key: ["provider_observation_at", "data_known_at"] for key in LEG_ORDER
        }
    if fetches is None or metadata_missing:
        return {
            "state": "WAIT_FOR_FINAL_DATA",
            "market_data_asof": shared_asof.isoformat(),
            "knowledge_asof": knowledge_utc.isoformat(),
            "finalization_asof": finalization_utc.isoformat(),
            "required_cutoffs": {
                key: value.isoformat() for key, value in required_cutoffs.items()
            },
            "closed_cutoffs": {
                key: value.isoformat() for key, value in closed_cutoffs.items()
            },
            "missing_scope": {},
            "missing_metadata": metadata_missing,
            "missing_closed_cutoff": missing_closed,
        }

    future_known = {}
    for key in LEG_ORDER:
        profile_start = pd.Timestamp(trade_date, tz=TZ) - pd.Timedelta(hours=6)
        future_known[key] = store.future_known_summary(
            runtime["legs"][key]["epic"], profile_start, required_cutoffs[key], knowledge_utc
        )
    excluded_future_known_count = sum(
        int(item["excluded_future_known_count"]) for item in future_known.values()
    )
    if excluded_future_known_count:
        return {
            "state": "WAIT_FOR_KNOWLEDGE",
            "knowledge_asof": knowledge_utc.isoformat(),
            "excluded_future_known_count": excluded_future_known_count,
            "excluded_future_known_max": max(
                (item["excluded_future_known_max"] for item in future_known.values() if item["excluded_future_known_max"]),
                default=None,
            ),
            "excluded_future_known_by_leg": future_known,
        }

    frames, _ = build_frames(store, runtime, shared_asof, knowledge_utc)
    gates: dict[str, dict[str, Any]] = {}
    for key in LEG_ORDER:
        minutes = int(runtime["legs"][key]["timeframe"].removesuffix("m"))
        cutoff = required_cutoffs[key]
        gates[key] = frame_gate(
            store,
            runtime["legs"][key]["epic"],
            frames[key],
            trade_date,
            cutoff,
            minutes,
        )
    valid = all(gate["state"] == "VALID" for gate in gates.values())
    payload: dict[str, object] = invalid_payload(trade_date, gates, finalization_utc)
    deterministic = True
    invariant_errors: list[str] = []
    prefix_violations: list[dict[str, object]] = []
    if valid:
        legs = [EngineLeg(key, frames[key], configs[key]) for key in LEG_ORDER]
        first = run_canonical_pair_pipeline(legs, [trade_date], state_config=state_config)
        second = run_canonical_pair_pipeline(legs, [trade_date], state_config=state_config)
        payload = engine_payload(first, configs)
        deterministic = object_hash(payload) == object_hash(engine_payload(second, configs))
        invariant_errors = decision_invariant_errors(payload["decisions"], payload["events"])
        for prefix_file in sorted((output_root / "prefix" / str(trade_date)).glob("*.json")):
            prefix = read_json(prefix_file)
            if prefix["state"] != "VALID":
                continue
            for key in LEG_ORDER:
                cutoff = pd.Timestamp(prefix["cutoffs"][key])
                expected = state_snapshot(first.leg_results[key].days[0], cutoff)
                observed = prefix["snapshots"].get(key)
                if not snapshots_equal(expected, observed):
                    prefix_violations.append(
                        {
                            "prefix_file": str(prefix_file),
                            "leg_key": key,
                            "cutoff": cutoff.isoformat(),
                            "expected_hash": object_hash(expected),
                            "observed_hash": object_hash(observed),
                        }
                    )
    manual = load_manual(output_root, trade_date)
    comparisons = [] if manual is None else comparison_rows(payload["decisions"], manual)
    critical = (not deterministic) or bool(invariant_errors) or bool(prefix_violations)
    manual_required = bool(runtime.get("manual_required", True))
    state = (
        "CRITICAL_STOP"
        if critical
        else (
            "DATA_INVALID"
            if not valid
            else ("MANUAL_MISSING" if manual_required and manual is None else "VALID")
        )
    )
    attempt = session_attempt_dir(output_root, trade_date)
    attempt.mkdir(parents=True, exist_ok=False)
    write_new_json(attempt / "session.json", {
        "schema_version": 1,
        "date": str(trade_date),
        "completed_at": decision_produced_at.isoformat(),
        "market_data_asof": shared_asof.isoformat(),
        "knowledge_asof": knowledge_utc.isoformat(),
        "finalization_asof": finalization_utc.isoformat(),
        "decision_produced_at": decision_produced_at.isoformat(),
        "state": state,
        "execution": runtime["execution"],
        "order_transport_present": order_transport_present(runtime),
        "data_valid": valid,
        "data_gates": gates,
        "deterministic_rerun": deterministic,
        "prefix_violation_count": len(prefix_violations),
        "invariant_error_count": len(invariant_errors),
        "trace_explainable_pct": 100.0 if not invariant_errors else 0.0,
        "decision_count": len(payload["decisions"]),
        "hashes": {
            "code": source_code_hash(),
            "config": campaign_lock(output_root)["live_config_hash"],
            "data": {key: gates[key]["data_hash"] for key in LEG_ORDER},
            "result": object_hash(payload),
            "manual": None if manual is None else manual["record_hash"],
        },
    })
    write_new_json(attempt / "data_gate.json", gates)
    write_new_json(attempt / "lifecycle.json", payload["lifecycle"])
    write_new_json(attempt / "day_summary.json", payload["days"])
    write_new_json(attempt / "causality_checks.json", {
        "deterministic_rerun": deterministic,
        "prefix_violations": prefix_violations,
        "invariant_errors": invariant_errors,
    })
    write_new_json(attempt / "manual_engine_alignment.json", comparisons)
    for name, rows in (("chronological_trace.jsonl", payload["events"]), ("decisions.jsonl", payload["decisions"])):
        path = attempt / name
        path.touch(exist_ok=False)
        for row in rows:
            append_jsonl(path, row)
    marker.parent.mkdir(parents=True, exist_ok=True)
    write_new_json(marker, {"state": state, "attempt": str(attempt), "result_hash": object_hash(payload)})
    if critical:
        raise CriticalLiveError(f"Critical session audit failure: {attempt}")
    return {"state": state, "path": str(attempt)}


def health_path(output_root: Path) -> Path:
    return output_root / "health.json"


def fatal_latch_path(output_root: Path) -> Path:
    return output_root / "fatal_latch.json"


def broker_recovery_path(output_root: Path) -> Path:
    return output_root / "runtime" / "broker_recovery_required.json"


def write_broker_recovery(
    output_root: Path,
    reason: str,
    error: str,
    **details: object,
) -> Path:
    path = broker_recovery_path(output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    first_detected_at = utc_now().isoformat()
    if path.exists():
        try:
            first_detected_at = str(read_json(path).get("first_detected_at") or first_detected_at)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    payload = {
        "schema_version": 1,
        "first_detected_at": first_detected_at,
        "updated_at": utc_now().isoformat(),
        "state": "UNKNOWN_NO_SEND",
        "reason": reason,
        "error": error,
        **details,
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def write_fatal_latch(output_root: Path, state: str, error: str, **details: object) -> Path:
    path = fatal_latch_path(output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "latched_at": utc_now().isoformat(),
        "state": state,
        "error": error,
        **details,
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def assert_no_fatal_latch(output_root: Path) -> None:
    path = fatal_latch_path(output_root)
    if path.exists():
        latch = read_json(path)
        raise CriticalLiveError(
            f"Persistent fatal latch {latch.get('state', 'UNKNOWN')} is set at {path}; "
            "inspect broker state and clear it explicitly before restart."
        )


def write_health(output_root: Path, state: str, **details: object) -> None:
    path = health_path(output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if "lease_id" not in details:
        lease_path = Path(output_root).resolve().parent / "control" / "session-lease.json"
        try:
            lease = read_json(lease_path)
            if lease.get("state") == "ACTIVE":
                details["lease_id"] = str(lease.get("lease_id") or "")
                details["campaign_id"] = str(lease.get("campaign_id") or "")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(
            {"updated_at": utc_now().isoformat(), "state": state, **details},
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def stop_request_path(output_root: Path) -> Path:
    return Path(output_root).resolve().parent / "control" / "stop-request.json"


def read_stop_request(output_root: Path) -> dict[str, Any] | None:
    path = stop_request_path(output_root)
    if not path.exists():
        return None
    request = read_json(path)
    if not isinstance(request, dict) or not request.get("request_id") or not request.get("reason"):
        raise UnsafeStopError("Stop request is malformed; refusing to continue or send.")
    return request


def touch_health(output_root: Path, phase: str) -> None:
    path = health_path(output_root)
    if not path.exists():
        return
    payload = read_json(path)
    payload["updated_at"] = utc_now().isoformat()
    payload["heartbeat_phase"] = phase
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def verify_markets(client: CapitalDemoClient, runtime: dict[str, Any]) -> dict[str, object]:
    verified: dict[str, object] = {}
    expectations = {"nq": ("NASDAQ", "INDICES"), "spx": ("S&P", "INDICES")}
    for key in LEG_ORDER:
        epic = runtime["legs"][key]["epic"]
        payload = client.market(epic)
        instrument = payload.get("instrument") or {}
        name = str(instrument.get("name") or instrument.get("symbol") or "").upper()
        expected_name, expected_type = expectations[key]
        if expected_name not in name or str(instrument.get("type")) != expected_type:
            raise CriticalLiveError(f"{key}: epic {epic} failed the fixed instrument selector.")
        verified[key] = {
            "epic": epic,
            "name": instrument.get("name"),
            "type": instrument.get("type"),
            "streaming": instrument.get("streamingPricesAvailable"),
        }
    return verified


def run_once(output_root: Path, client: CapitalDemoClient, store: BarStore) -> dict[str, object]:
    runtime = runtime_config()
    lock = campaign_lock(output_root)
    cycle_started_at = utc_now()
    history_start = cycle_started_at - pd.Timedelta(days=int(runtime["history_days"]))
    fetches = {}
    for key in LEG_ORDER:
        epic = runtime["legs"][key]["epic"]
        latest = store.latest_time(epic)
        start = history_start if latest is None else max(history_start, latest - pd.Timedelta(minutes=10))
        fetches[key] = fetch_window(client, store, output_root, epic, start, cycle_started_at)
    evaluation_now = utc_now()
    if not campaign_session_eligible(evaluation_now, lock):
        return {
            "fetches": fetches,
            "preflight": {"state": "CAMPAIGN_WARMUP"},
            "prefix": {"state": "CAMPAIGN_WARMUP"},
            "execution": {"state": "CAMPAIGN_WARMUP"},
            "final": {"state": "CAMPAIGN_WARMUP"},
            "daily_report": {"state": "NOT_AVAILABLE"},
            "timing": {
                "cycle_started_at": cycle_started_at.isoformat(),
                "evaluation_at": evaluation_now.isoformat(),
            },
        }
    preflight: dict[str, object] = {"state": "NOT_APPLICABLE"}
    preflight_order_transport = getattr(client, "preflight_order_transport", None)
    if callable(preflight_order_transport):
        preflight = preflight_order_transport(output_root, evaluation_now)
    prefix = run_prefix(output_root, store, cycle_started_at, evaluation_now)
    execution: dict[str, object] = {"state": "NO_ORDER_TRANSPORT"}
    reconcile = getattr(client, "reconcile_orders", None)
    preflight_ready = str(preflight.get("state")) in {"NOT_APPLICABLE", "PASS", "ALREADY_PASSED"}
    send_guard_at: pd.Timestamp | None = None
    if callable(reconcile) and preflight_ready:
        send_guard_at = utc_now()
        prefix_record = None
        if prefix.get("path") and Path(str(prefix["path"])).is_file():
            prefix_record = read_json(Path(str(prefix["path"])))
            prefix_decision_at = pd.Timestamp(prefix_record["decision_produced_at"]).tz_convert(UTC)
            if prefix_decision_at > send_guard_at.tz_convert(UTC):
                raise CriticalLiveError("Prefix decision was produced after the send guard.")
        execution = reconcile(output_root, prefix, send_guard_at, lock)
        if isinstance(execution, dict):
            execution["decision_produced_at"] = prefix_record.get("decision_produced_at") if prefix_record else None
            execution["send_guard_at"] = send_guard_at.isoformat()
    elif callable(reconcile):
        send_guard_at = utc_now()
        execution = {
            "state": "PREFLIGHT_PENDING",
            "preflight_state": preflight.get("state"),
        }
    if str(execution.get("state")) == "UNKNOWN_NO_SEND":
        raise TransientLiveError(
            "Broker state is UNKNOWN_NO_SEND; verified pending-order recovery is required: "
            f"{execution.get('reason') or 'broker readback unavailable'}"
        )
    finalization_at = utc_now()
    final = finalize_session(
        output_root,
        store,
        market_data_asof=cycle_started_at,
        knowledge_asof=evaluation_now,
        finalization_asof=finalization_at,
        fetches=fetches,
    )
    daily_report: dict[str, object] = {"state": "NOT_AVAILABLE"}
    report = getattr(client, "daily_health_report", None)
    if str(execution.get("state")) == "UNKNOWN_NO_SEND":
        daily_report = {
            "state": "UNKNOWN_NO_SEND",
            "reason": execution.get("reason"),
        }
    elif callable(report):
        daily_report = report(output_root, finalization_at, prefix, final, lock)
    return {
        "fetches": fetches,
        "preflight": preflight,
        "prefix": prefix,
        "execution": execution,
        "final": final,
        "daily_report": daily_report,
        "timing": {
            "cycle_started_at": cycle_started_at.isoformat(),
            "market_data_asof": cycle_started_at.isoformat(),
            "evaluation_at": evaluation_now.isoformat(),
            "send_guard_at": None if send_guard_at is None else send_guard_at.isoformat(),
            "finalization_at": finalization_at.isoformat(),
        },
    }


def _install_shutdown_handlers() -> dict[int, Any]:
    _STOP_EVENT.clear()
    previous: dict[int, Any] = {}

    def request_stop(signum: int, frame: object) -> None:
        del frame
        _STOP_EVENT.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    return previous


def _restore_shutdown_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _cancel_for_stop(
    output_root: Path,
    client: CapitalDemoClient | None,
    reason: str,
    *,
    require_readback: bool = False,
) -> list[dict[str, object]]:
    details = _graceful_stop(
        output_root,
        client,
        reason,
        require_readback=require_readback,
    )
    return list(details.get("cancelled", []))


def _raise_unsafe_cancel_failure(output_root: Path, reason: str, exc: Exception) -> None:
    latch = write_fatal_latch(
        output_root,
        "UNSAFE_OPEN_ORDERS",
        str(exc),
        shutdown_reason=reason,
        error_type=type(exc).__name__,
    )
    write_health(
        output_root,
        "UNSAFE_OPEN_ORDERS",
        error=str(exc),
        fatal_latch=str(latch),
        order_transport_present=True,
    )
    raise CriticalLiveError(
        f"Pending-order cancellation/readback could not be verified; fatal latch: {latch}"
    ) from exc


def _raise_unsafe_stop_failure(output_root: Path, reason: str, exc: Exception) -> None:
    latch = write_fatal_latch(
        output_root,
        "UNSAFE_STOP_NO_SEND",
        str(exc),
        shutdown_reason=reason,
        error_type=type(exc).__name__,
    )
    write_health(
        output_root,
        "UNSAFE_STOP_NO_SEND",
        error=str(exc),
        fatal_latch=str(latch),
        order_transport_present=True,
    )
    raise UnsafeStopError(
        f"Safe stop could not be proven; fatal latch: {latch}"
    ) from exc


def _graceful_stop(
    output_root: Path,
    client: CapitalDemoClient | None,
    reason: str,
    *,
    require_readback: bool,
) -> dict[str, object]:
    emergency = getattr(client, "cancel_all_pending", None)
    if not callable(emergency):
        if require_readback:
            _raise_unsafe_stop_failure(
                output_root,
                reason,
                CriticalLiveError("Order transport client cannot reconcile pending orders."),
            )
        return {"cancelled": [], "safe_stop": "PASS", "owned_pending": 0, "open_positions": 0}
    try:
        cancelled = list(emergency(output_root, reason))
        reconcile = getattr(client, "stop_reconciliation", None)
        if not callable(reconcile):
            raise CriticalLiveError("Order transport client cannot reconcile final positions.")
        details = dict(reconcile(output_root, reason))
        details["cancelled"] = cancelled
        if str(details.get("safe_stop")) != "PASS":
            raise CriticalLiveError(f"Broker exposure remained unsafe: {details}")
        return details
    except Exception as exc:
        _raise_unsafe_stop_failure(output_root, reason, exc)
        raise AssertionError("unreachable")


def _cancel_for_recovery(
    output_root: Path,
    client: CapitalDemoClient | None,
    reason: str,
    *,
    transport: bool,
    trigger_error: Exception | None = None,
) -> dict[str, object]:
    if not transport:
        return {
            "state": "NOT_APPLICABLE",
            "broker_state": "NO_ORDER_TRANSPORT",
            "recovery_required": False,
            "cancelled": [],
        }
    trigger = "" if trigger_error is None else str(trigger_error)
    marker = write_broker_recovery(
        output_root,
        reason,
        trigger or "Pending-order state requires verified broker readback.",
        trigger_error_type=None if trigger_error is None else type(trigger_error).__name__,
    )
    write_health(
        output_root,
        "RETRYING",
        broker_state="UNKNOWN_NO_SEND",
        recovery_required=True,
        recovery_marker=str(marker),
        recovery_reason=reason,
        order_transport_present=True,
    )
    emergency = getattr(client, "cancel_all_pending", None)
    if not callable(emergency):
        error = "Order transport client is unavailable for pending-order readback."
        write_broker_recovery(output_root, reason, error, error_type="ClientUnavailable")
        return {
            "state": "UNKNOWN_NO_SEND",
            "broker_state": "UNKNOWN_NO_SEND",
            "recovery_required": True,
            "error": error,
            "cancelled": [],
        }
    try:
        cancelled = list(emergency(output_root, reason))
    except CriticalLiveError as exc:
        _raise_unsafe_cancel_failure(output_root, reason, exc)
    except (TransientLiveError, OSError, TimeoutError, sqlite3.OperationalError) as exc:
        write_broker_recovery(
            output_root,
            reason,
            str(exc),
            error_type=type(exc).__name__,
            trigger_error=trigger or None,
        )
        return {
            "state": "UNKNOWN_NO_SEND",
            "broker_state": "UNKNOWN_NO_SEND",
            "recovery_required": True,
            "error": str(exc),
            "cancelled": [],
        }
    except Exception as exc:
        _raise_unsafe_cancel_failure(output_root, reason, exc)
    append_jsonl(
        output_root / "broker_recovery" / f"{utc_now().strftime('%Y-%m-%d')}.jsonl",
        {
            "recorded_at": utc_now().isoformat(),
            "state": "VERIFIED_SAFE",
            "reason": reason,
            "cancelled": cancelled,
        },
    )
    marker.unlink(missing_ok=True)
    return {
        "state": "VERIFIED_SAFE",
        "broker_state": "VERIFIED_NO_PENDING",
        "recovery_required": False,
        "cancelled": cancelled,
    }


def _daemon_locked(args: argparse.Namespace, output_root: Path) -> None:
    output_root = Path(args.output_root).resolve()
    campaign_lock(output_root)
    assert_no_fatal_latch(output_root)
    runtime = runtime_config()
    transport = order_transport_present(runtime)
    previous_handlers = _install_shutdown_handlers()
    active_client: CapitalDemoClient | None = None
    try:
        while not _STOP_EVENT.is_set():
            stop_request = read_stop_request(output_root)
            secrets = credentials()
            if secrets is None:
                if stop_request is not None:
                    write_health(
                        output_root,
                        "UNSAFE_STOP_NO_SEND",
                        reason=stop_request.get("reason"),
                        error="Credentials unavailable for broker-side stop reconciliation.",
                        order_transport_present=transport,
                    )
                    _STOP_EVENT.wait(5)
                    continue
                recovery_required = transport and broker_recovery_path(output_root).exists()
                write_health(
                    output_root,
                    "RETRYING" if recovery_required else "WAITING_CREDENTIALS",
                    missing=[name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()],
                    broker_state="UNKNOWN_NO_SEND" if recovery_required else "NOT_CONNECTED",
                    recovery_required=recovery_required,
                    order_transport_present=transport,
                )
                _STOP_EVENT.wait(30)
                continue
            store = BarStore(output_root)
            active_client = None
            try:
                active_client = CapitalDemoClient(runtime, secrets)
                stop_request = read_stop_request(output_root)
                if stop_request is not None:
                    stop_details = _graceful_stop(
                        output_root,
                        active_client,
                        str(stop_request["reason"]),
                        require_readback=transport,
                    )
                    write_health(
                        output_root,
                        "STOPPED",
                        reason=stop_request["reason"],
                        request_id=stop_request["request_id"],
                        lease_id=stop_request.get("lease_id"),
                        **stop_details,
                        order_transport_present=transport,
                    )
                    return
                if transport and broker_recovery_path(output_root).exists():
                    recovery = _cancel_for_recovery(
                        output_root,
                        active_client,
                        "TECHNICAL_RECOVERY",
                        transport=True,
                    )
                    if recovery["state"] != "VERIFIED_SAFE":
                        write_health(
                            output_root,
                            "RETRYING",
                            error=recovery.get("error"),
                            broker_state="UNKNOWN_NO_SEND",
                            recovery_required=True,
                            recovery=recovery,
                            order_transport_present=True,
                        )
                        _STOP_EVENT.wait(30)
                        continue
                verified = verify_markets(active_client, runtime)
                write_health(
                    output_root,
                    "RUNNING",
                    markets=verified,
                    last_cycle={"execution": {"state": "FETCHING_MARKET_DATA"}},
                    order_transport_present=transport,
                )
                result = run_once(output_root, active_client, store)
                write_health(
                    output_root,
                    "RUNNING",
                    markets=verified,
                    last_cycle=result,
                    order_transport_present=transport,
                )
                _STOP_EVENT.wait(int(runtime["poll_seconds"]))
                if _STOP_EVENT.is_set():
                    cancelled = _cancel_for_stop(
                        output_root,
                        active_client,
                        "SIGNAL_SHUTDOWN",
                        require_readback=transport,
                    )
                    write_health(
                        output_root,
                        "STOPPED",
                        reason="SIGNAL_SHUTDOWN",
                        cancelled=cancelled,
                        order_transport_present=transport,
                    )
            except CriticalLiveError as exc:
                _cancel_for_stop(
                    output_root,
                    active_client,
                    "CRITICAL_STOP",
                    require_readback=transport,
                )
                latch = write_fatal_latch(
                    output_root,
                    "CRITICAL_STOP",
                    str(exc),
                    error_type=type(exc).__name__,
                )
                write_health(
                    output_root,
                    "CRITICAL_STOP",
                    error=str(exc),
                    fatal_latch=str(latch),
                    order_transport_present=transport,
                )
                raise
            except (TransientLiveError, OSError, TimeoutError, sqlite3.OperationalError) as exc:
                recovery = _cancel_for_recovery(
                    output_root,
                    active_client,
                    "TECHNICAL_FAILURE",
                    transport=transport,
                    trigger_error=exc,
                )
                append_jsonl(
                    output_root / "technical_failures" / f"{utc_now().strftime('%Y-%m-%d')}.jsonl",
                    {
                        "recorded_at": utc_now().isoformat(),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                write_health(
                    output_root,
                    "RETRYING",
                    error=str(exc),
                    broker_state=recovery["broker_state"],
                    recovery_required=recovery["recovery_required"],
                    recovery=recovery,
                    order_transport_present=transport,
                )
                _STOP_EVENT.wait(30)
            except Exception as exc:
                _cancel_for_stop(
                    output_root,
                    active_client,
                    "UNCLASSIFIED_FAILURE",
                    require_readback=transport,
                )
                latch = write_fatal_latch(
                    output_root,
                    "UNCLASSIFIED_FAILURE",
                    str(exc),
                    error_type=type(exc).__name__,
                )
                write_health(
                    output_root,
                    "CRITICAL_STOP",
                    error=str(exc),
                    fatal_latch=str(latch),
                    order_transport_present=transport,
                )
                raise CriticalLiveError(
                    f"Unclassified daemon failure was latched: {type(exc).__name__}: {exc}"
                ) from exc
            finally:
                close_client = getattr(active_client, "close", None)
                if callable(close_client):
                    close_client()
                active_client = None
                store.close()
        if _STOP_EVENT.is_set() and transport:
            shutdown_secrets = credentials()
            if shutdown_secrets is None:
                latch = write_fatal_latch(
                    output_root,
                    "UNSAFE_OPEN_ORDERS",
                    "Credentials unavailable during shutdown broker readback.",
                    shutdown_reason="SIGNAL_SHUTDOWN",
                )
                write_health(
                    output_root,
                    "UNSAFE_OPEN_ORDERS",
                    error="Credentials unavailable during shutdown broker readback.",
                    fatal_latch=str(latch),
                    order_transport_present=transport,
                )
                raise CriticalLiveError(f"Shutdown broker readback was impossible; fatal latch: {latch}")
            shutdown_client = CapitalDemoClient(runtime, shutdown_secrets)
            try:
                cancelled = _cancel_for_stop(
                    output_root,
                    shutdown_client,
                    "SIGNAL_SHUTDOWN_VERIFY",
                    require_readback=True,
                )
                write_health(
                    output_root,
                    "STOPPED",
                    reason="SIGNAL_SHUTDOWN",
                    cancelled=cancelled,
                    order_transport_present=transport,
                )
            finally:
                close_shutdown_client = getattr(shutdown_client, "close", None)
                if callable(close_shutdown_client):
                    close_shutdown_client()
    finally:
        _restore_shutdown_handlers(previous_handlers)


def daemon(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    with OutputRootProcessLock(output_root):
        _daemon_locked(args, output_root)


def doctor(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    lock = campaign_lock(output_root)
    secrets = credentials()
    if secrets is None:
        raise CriticalLiveError(f"Missing credentials: {', '.join(REQUIRED_ENV)}")
    client = CapitalDemoClient(runtime_config(), secrets)
    try:
        markets = verify_markets(client, runtime_config())
        permission = getattr(client, "order_permission_status", None)
        order_permission = permission() if callable(permission) else {"state": "NOT_APPLICABLE"}
        state = "READY" if order_permission.get("state") in {"READY", "NOT_APPLICABLE"} else "NOT_READY"
        print(
            json.dumps(
                {
                    "state": state,
                    "campaign": lock,
                    "markets": markets,
                    "order_permission": order_permission,
                },
                indent=2,
                default=str,
            )
        )
    finally:
        close_client = getattr(client, "close", None)
        if callable(close_client):
            close_client()


def status(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    campaign_lock(output_root)
    path = health_path(output_root)
    if not path.exists():
        print(json.dumps({"state": "NOT_STARTED"}))
        return
    print(path.read_text(encoding="utf-8"), end="")


def smoke_order(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    lock = campaign_lock(output_root)
    if not args.confirm_demo:
        raise CriticalLiveError("Smoke order requires --confirm-demo.")
    secrets = credentials()
    if secrets is None:
        raise CriticalLiveError(f"Missing credentials: {', '.join(REQUIRED_ENV)}")
    client = CapitalDemoClient(runtime_config(), secrets)
    try:
        verify_markets(client, runtime_config())
        smoke = getattr(client, "smoke_order", None)
        if not callable(smoke):
            raise CriticalLiveError("The configured transport has no smoke-order implementation.")
        result = smoke(output_root, lock)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    finally:
        close_client = getattr(client, "close", None)
        if callable(close_client):
            close_client()


def daily_health(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    campaign_lock(output_root)
    report_date = args.date or str(utc_now().tz_convert(TZ).date())
    path = output_root / "daily_health" / f"{report_date}.json"
    if not path.exists():
        print(json.dumps({"state": "NOT_AVAILABLE", "date": report_date}))
        return
    print(path.read_text(encoding="utf-8"), end="")


def initialize(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    lock = campaign_lock(output_root)
    initialize_ledger = getattr(CapitalDemoClient, "_initialize_order_db", None)
    if callable(initialize_ledger):
        # The ledger schema is created without constructing a broker session or
        # sending any order.  This makes a fresh campaign readiness-checkable.
        initialize_ledger(CapitalDemoClient.__new__(CapitalDemoClient), output_root)
    write_health(
        output_root,
        "INITIALIZED",
        campaign_created_at=lock["created_at"],
        order_transport_present=order_transport_present(),
    )
    print(json.dumps(lock, indent=2, default=str))


def clear_fatal_latch(args: argparse.Namespace) -> None:
    output_root = Path(args.output_root).resolve()
    if not args.confirm:
        raise CriticalLiveError("Clearing the fatal latch requires --confirm after broker inspection.")
    path = fatal_latch_path(output_root)
    if path.exists():
        cleared = path.with_name(f"fatal_latch.cleared.{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json")
        path.replace(cleared)
        print(json.dumps({"state": "CLEARED", "archived_latch": str(cleared)}))
    else:
        print(json.dumps({"state": "NOT_SET"}))


def parser() -> argparse.ArgumentParser:
    runtime = runtime_config()
    provider = "XM MT5" if runtime["feed"] == "XM_MT5" else "Capital demo"
    output_name = "xm_mt5_forward" if runtime["feed"] == "XM_MT5" else "capital_forward"
    root = argparse.ArgumentParser(description=f"{provider} closed-bar causal forward shadow daemon.")
    root.add_argument("--output-root", default=str(ROOT / "outputs" / output_name / "nq3m_spx5m"))
    root.add_argument(
        "--credential-stdin",
        action="store_true",
        help="Read a transient broker password from stdin; never use an environment variable for it.",
    )
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("daemon")
    sub.add_parser("doctor")
    sub.add_parser("status")
    clear_latch = sub.add_parser("clear-fatal-latch")
    clear_latch.add_argument("--confirm", action="store_true")
    smoke = sub.add_parser("smoke-order")
    smoke.add_argument("--confirm-demo", action="store_true")
    report = sub.add_parser("daily-health")
    report.add_argument("--date")
    manual = sub.add_parser("manual")
    manual.add_argument("--date", required=True)
    manual.add_argument("--author", required=True)
    for key in LEG_ORDER:
        manual.add_argument(f"--{key}-final", choices=["TAKE", "SKIP", "UNKNOWN"], required=True)
        manual.add_argument(f"--{key}-direction", choices=["LONG", "SHORT", "UNKNOWN"], required=True)
        manual.add_argument(f"--{key}-explanation")
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "init":
            initialize(args)
        elif args.command == "daemon":
            daemon(args)
        elif args.command == "doctor":
            doctor(args)
        elif args.command == "status":
            status(args)
        elif args.command == "clear-fatal-latch":
            clear_fatal_latch(args)
        elif args.command == "smoke-order":
            smoke_order(args)
        elif args.command == "daily-health":
            daily_health(args)
        elif args.command == "manual":
            record_manual(args)
    except CriticalLiveError as exc:
        if args.command == "daemon":
            print(str(exc), file=sys.stderr)
            raise SystemExit(FATAL_EXIT_STATUS) from exc
        raise


if __name__ == "__main__":
    main()
