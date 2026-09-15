"""Fail-closed, resumable Dukascopy acquisition policy and durable evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

STATE_RELATIVE = Path("outputs/reports/.dukascopy_acquisition_v5/state.json")
EVENTS_RELATIVE = Path("outputs/reports/.dukascopy_acquisition_v5/http_events.jsonl")
LEGACY_LOG_SHA256 = "895104f716a9f36042f2843fe560315d1dbd249cd47121801dc5f7dcc74a0688"
MIGRATION_COOLDOWN_UTC = "2026-09-15T15:33:39.3919524Z"
DEFAULT_HOST_ALLOWLIST = frozenset({"datafeed.dukascopy.com"})
RETRYABLE_STATUS = frozenset({408, 425, 500, 502, 503, 504})
RETRY_DELAYS_SECONDS = (5.0, 15.0, 45.0, 120.0)
RATE_LIMIT_BASES_SECONDS = (60.0, 120.0, 240.0, 480.0, 900.0)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STATE_SCHEMA_VERSION = 2
REQUEST_METHOD_CONTRACT_VERSION = "DUKASCOPY_HTTP_REQUEST_V5"
MIN_PROVIDER_SPACING_SECONDS = 2.0
UNKNOWN_ATTEMPT_COOLDOWN_SECONDS = 24 * 60 * 60
ACQUISITION_LEASE_SECONDS = 15 * 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("UTC timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("UTC timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        seconds = (target.astimezone(timezone.utc) - (now or utc_now())).total_seconds()
    if seconds < 0:
        return 0.0
    if not seconds < float("inf"):
        return None
    return seconds


def _positive_jitter(base: float, rng: Callable[[], int] | None = None) -> float:
    if base <= 0:
        return 0.0
    draw = int((rng or (lambda: secrets.randbelow(1000) + 1))())
    return base * max(1, min(1000, draw)) / 10000.0


def rate_limit_delay(consecutive_429: int, retry_after: str | None, *, now: datetime | None = None, rng: Callable[[], int] | None = None) -> tuple[float, float | None]:
    parsed = parse_retry_after(retry_after, now=now)
    index = max(1, min(int(consecutive_429), len(RATE_LIMIT_BASES_SECONDS))) - 1
    base = max(float(parsed or 0.0), RATE_LIMIT_BASES_SECONDS[index])
    return base + _positive_jitter(base, rng), parsed


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def require_sha256(value: object, label: str = "sha256") -> str:
    text = str(value or "")
    if not SHA256_RE.fullmatch(text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def validate_provider_url(url: str, *, allowlist: set[str] | frozenset[str] = DEFAULT_HOST_ALLOWLIST) -> str:
    raw = str(url).strip()
    match = re.fullmatch(r"https://([^/?#]+)(?:[/?#].*)?", raw, flags=re.IGNORECASE)
    authority = match.group(1) if match else ""
    host = authority.lower()
    if not match or "#" in raw or "@" in authority or ":" in authority or host not in allowlist:
        raise ValueError("provider URL is outside the locked HTTPS allowlist")
    return raw


def validate_clock(*, now: datetime | None, transport_fixture: bool) -> datetime:
    if now is not None and not transport_fixture:
        raise ValueError("injected clock is allowed only for transport fixtures")
    return parse_utc(now) if now is not None else utc_now()


class AcquisitionDeferred(RuntimeError):
    def __init__(self, next_retry_at_utc: str, reason: str = "DEFERRED_RATE_LIMIT") -> None:
        super().__init__(reason)
        self.reason = reason
        self.next_retry_at_utc = next_retry_at_utc


class AcquisitionTerminalError(RuntimeError):
    pass


class ProviderResponseError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, error_code: str = "PROVIDER_RESPONSE_INVALID", headers: Mapping[str, Any] | None = None, endpoint: str | None = None, request_url_sha256: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.headers = dict(headers or {})
        self.endpoint = endpoint
        self.request_url_sha256 = request_url_sha256


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class AtomicJsonStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("durable acquisition state must be a JSON object")
        return value

    def write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_name(f"{self.path.name}.part.{os.getpid()}.{uuid4().hex}")
        try:
            with part.open("wb") as handle:
                handle.write(_canonical(value) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part, self.path)
            _fsync_directory(self.path.parent)
        finally:
            part.unlink(missing_ok=True)


def process_start_token(pid: int | None = None) -> str:
    pid = int(pid or os.getpid())
    try:
        import psutil  # type: ignore
        return str(psutil.Process(pid).create_time())
    except Exception:
        if os.name != "nt":
            stat = Path(f"/proc/{pid}").stat()
            return f"{stat.st_ctime_ns}:{stat.st_mtime_ns}"
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            raise OSError(error, f"cannot open process {pid}")
        try:
            creation_time = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(creation_time), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                error = ctypes.get_last_error()
                raise OSError(error, f"cannot read process start time {pid}")
            value = (int(creation_time.dwHighDateTime) << 32) | int(creation_time.dwLowDateTime)
            return f"FILETIME:{value:016x}"
        finally:
            kernel32.CloseHandle(handle)


class ProviderProcessLock:
    """Descriptor lock held for the full provider process lifetime."""

    def __init__(self, path: str | Path, *, pid: int | None = None, token: str | None = None) -> None:
        self.path = Path(path)
        self.pid = int(pid or os.getpid())
        self.token = str(token or process_start_token(self.pid))
        self._handle: Any | None = None
        self.held = False

    def _metadata(self) -> dict[str, Any]:
        return {"pid": self.pid, "process_start_token": self.token, "host": platform.node(), "acquired_at_utc": iso_utc(utc_now())}

    def acquire(self, **_: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(_canonical(self._metadata()) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError("Dukascopy provider lock is held") from exc
        self._handle = handle
        self.held = True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
            self.held = False

    def __enter__(self) -> "ProviderProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class HttpEventLog:
    ALLOWED_FIELDS = frozenset({"event_id", "event_type", "previous_event_sha256", "run_id", "artifact_id", "host", "started_at_utc", "finished_at_utc", "status", "retry_after", "date", "content_type", "content_length", "etag", "endpoint", "body_sha256", "body_byte_count", "error_code", "request_url_sha256", "provider_call_count", "terminal"})

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with ProviderProcessLock(lock_path):
            clean = {key: event[key] for key in event if key in self.ALLOWED_FIELDS and key != "event_sha256"}
            clean.setdefault("event_id", uuid4().hex)
            previous = ""
            if self.path.is_file() and self.path.read_text(encoding="utf-8").splitlines():
                previous = require_sha256(json.loads(self.path.read_text(encoding="utf-8").splitlines()[-1]).get("event_sha256"), "previous event SHA")
            clean["previous_event_sha256"] = previous
            clean["event_sha256"] = sha256_bytes(_canonical(clean))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as handle:
                handle.write(_canonical(clean) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            return clean


def _default_state() -> dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "migration": {"completed": False, "source_sha256": None, "completed_at_utc": None}, "hosts": {}, "provider_call_count": 0, "runs": {}, "request_events": []}


def _strict_state(value: Mapping[str, Any]) -> dict[str, Any]:
    state = dict(value)
    version = int(state.get("schema_version", 1))
    if version not in {1, STATE_SCHEMA_VERSION}:
        raise ValueError("unsupported acquisition state schema")
    if version == 1:
        migration = state.get("migration") if isinstance(state.get("migration"), dict) else {}
        state = {**_default_state(), "hosts": state.get("hosts", {}), "runs": state.get("runs", {}), "request_events": state.get("request_events", []), "provider_call_count": int(state.get("provider_call_count", 0)), "migration": {"completed": bool(migration.get("completed", False)), "source_sha256": migration.get("source_sha256") or state.get("migration_log_sha256"), "completed_at_utc": migration.get("completed_at_utc")}}
    if not isinstance(state.get("hosts"), dict) or not isinstance(state.get("runs"), dict) or not isinstance(state.get("request_events"), list):
        raise ValueError("acquisition state containers are invalid")
    state["schema_version"] = STATE_SCHEMA_VERSION
    state["provider_call_count"] = max(0, int(state.get("provider_call_count", 0)))
    return state


class RateLimitController:
    def __init__(self, state_path: str | Path, *, rng: Callable[[], int] | None = None, event_path: str | Path | None = None) -> None:
        self.store = AtomicJsonStore(state_path)
        self.rng = rng
        self.events = HttpEventLog(event_path or Path(state_path).with_name("http_events.jsonl"))

    def _load(self) -> dict[str, Any]:
        return _strict_state(self.store.read() or _default_state())

    def _write(self, state: Mapping[str, Any]) -> None:
        self.store.write(_strict_state(state))

    def _state_lock_path(self) -> Path:
        return self.store.path.with_name(f"{self.store.path.name}.state.lock")

    def migrate_legacy_log(self, log_path: str | Path, *, now: datetime | None = None, transport_fixture: bool = False) -> dict[str, Any]:
        with ProviderProcessLock(self._state_lock_path()):
            state = self._load()
            migration = state["migration"]
            if migration.get("completed"):
                return state
            digest = sha256(Path(log_path).read_bytes()).hexdigest()
            if digest != LEGACY_LOG_SHA256:
                raise ValueError("legacy rate-limit log hash mismatch")
            observed = validate_clock(now=now, transport_fixture=transport_fixture)
            host = next(iter(DEFAULT_HOST_ALLOWLIST))
            existing = dict(state["hosts"].get(host, {}))
            existing_deadline = existing.get("next_retry_at_utc")
            migration_deadline = parse_utc(MIGRATION_COOLDOWN_UTC)
            if existing_deadline and parse_utc(str(existing_deadline)) > migration_deadline:
                deadline_text = str(existing_deadline)
            else:
                deadline_text = MIGRATION_COOLDOWN_UTC
            existing.update({"circuit": "OPEN", "consecutive_429": max(5, int(existing.get("consecutive_429", 0))), "next_retry_at_utc": deadline_text, "migration_log_sha256": digest})
            state["hosts"][host] = existing
            state["migration"] = {"completed": True, "source_sha256": digest, "completed_at_utc": iso_utc(observed)}
            self._write(state)
            return state

    def _recover_unknown_attempts(self, state: dict[str, Any], observed: datetime) -> None:
        for event in state["request_events"]:
            if event.get("event_type") != "REQUEST_STARTED" or event.get("terminal"):
                continue
            started = parse_utc(str(event["started_at_utc"]))
            host = str(event["host"])
            item = dict(state["hosts"].get(host, {}))
            deadline = started.timestamp() + UNKNOWN_ATTEMPT_COOLDOWN_SECONDS
            current = parse_utc(str(item["next_retry_at_utc"])).timestamp() if item.get("next_retry_at_utc") else 0
            item.update({"circuit": "OPEN", "unknown_attempt": True, "next_retry_at_utc": iso_utc(datetime.fromtimestamp(max(deadline, current), timezone.utc))})
            event.update({"terminal": True, "event_type": "UNKNOWN_ATTEMPT", "finished_at_utc": iso_utc(observed)})
            state["hosts"][host] = item

    def start_run(self, run_id: str) -> dict[str, Any]:
        run_id = str(run_id).strip()
        if not run_id:
            raise ValueError("acquisition run_id is required")
        with ProviderProcessLock(self._state_lock_path()):
            state = self._load()
            active = str(state.get("active_run_id") or "")
            if active and active != run_id:
                raise ValueError("acquisition state is bound to one run_id")
            state["active_run_id"] = run_id
            state["run_identity"] = {"pid": os.getpid(), "process_start_token": process_start_token(), "host": platform.node()}
            state["runs"].setdefault(run_id, {"run_id": run_id, "provider_call_count": 0, "artifacts": {}, "started_at_utc": iso_utc(utc_now())})
            self._write(state)
            return state["runs"][run_id]

    def before_request(self, host: str, *, now: datetime | None = None, run_id: str = "default", artifact_id: str = "unknown", transport_fixture: bool = False) -> dict[str, Any]:
        if str(host).lower() not in DEFAULT_HOST_ALLOWLIST:
            raise ValueError("provider host is outside the locked allowlist")
        host = str(host).lower()
        validate_clock(now=now, transport_fixture=transport_fixture)
        with ProviderProcessLock(self._state_lock_path()):
            return self._before_request_locked(host, now=now, run_id=run_id, artifact_id=artifact_id, transport_fixture=transport_fixture)

    def _before_request_locked(self, host: str, *, now: datetime | None, run_id: str, artifact_id: str, transport_fixture: bool) -> dict[str, Any]:
        observed = now or utc_now()
        state = self._load()
        active_run_id = str(state.get("active_run_id") or "")
        if active_run_id and active_run_id != str(run_id):
            raise ValueError("acquisition request uses a foreign run_id")
        state["active_run_id"] = str(run_id)
        self._recover_unknown_attempts(state, observed)
        item = dict(state["hosts"].get(host, {"circuit": "CLOSED", "consecutive_429": 0}))
        retry_at = item.get("next_retry_at_utc")
        if item.get("circuit") == "TERMINAL":
            raise AcquisitionTerminalError(str(item.get("terminal_error", "terminal provider error")))
        if item.get("circuit") == "OPEN" and retry_at:
            deadline = parse_utc(str(retry_at))
            if observed < deadline:
                self._write(state)
                raise AcquisitionDeferred(str(retry_at), "DEFERRED_RATE_LIMIT")
            item["circuit"] = "HALF_OPEN"
        if item.get("circuit") == "HALF_OPEN" and item.get("half_open_claimed"):
            raise AcquisitionDeferred(str(retry_at or iso_utc(observed)), "HALF_OPEN_ALREADY_CLAIMED")
        last = item.get("last_provider_start_at_utc")
        if last:
            next_allowed = parse_utc(str(last)).timestamp() + MIN_PROVIDER_SPACING_SECONDS
            if observed.timestamp() < next_allowed:
                deadline = iso_utc(datetime.fromtimestamp(next_allowed, timezone.utc))
                self._write(state)
                raise AcquisitionDeferred(deadline, "DEFERRED_PROVIDER_SPACING")
        half_open = item.get("circuit") == "HALF_OPEN"
        if half_open:
            item["half_open_claimed"] = True
        started = iso_utc(observed)
        call_count = int(state.get("provider_call_count", 0)) + 1
        state["provider_call_count"] = call_count
        run = state["runs"].setdefault(str(run_id), {"provider_call_count": 0, "artifacts": {}, "started_at_utc": started})
        run["provider_call_count"] = int(run.get("provider_call_count", 0)) + 1
        artifact = run.setdefault("artifacts", {}).setdefault(str(artifact_id), {"provider_call_count": 0})
        artifact["provider_call_count"] = int(artifact.get("provider_call_count", 0)) + 1
        artifact["status"] = "REQUEST_STARTED"
        event = {"event_id": uuid4().hex, "event_type": "REQUEST_STARTED", "terminal": False, "host": host, "run_id": str(run_id), "artifact_id": str(artifact_id), "started_at_utc": started, "provider_call_count": call_count}
        state["request_events"].append(event)
        item["last_provider_start_at_utc"] = started
        item["circuit"] = "HALF_OPEN" if half_open else item.get("circuit", "CLOSED")
        state["hosts"][host] = item
        self._write(state)
        self.events.append(event)
        return {"event_id": event["event_id"], "provider_call_count": call_count, "half_open": half_open, "run_id": run_id, "artifact_id": artifact_id}

    def finish_request(self, request: Mapping[str, Any], *, status: int | None = None, error_code: str | None = None, body_sha256: str | None = None, body_byte_count: int | None = None, headers: Mapping[str, Any] | None = None, endpoint: str | None = None, request_url_sha256: str | None = None, finished_at: datetime | None = None, transport_fixture: bool = False) -> dict[str, Any]:
        with ProviderProcessLock(self._state_lock_path()):
            state = self._load()
            event = next((item for item in state["request_events"] if item.get("event_id") == request.get("event_id")), None)
            if event is None or event.get("terminal"):
                raise ValueError("request event is missing or already terminal")
            if body_sha256 is not None:
                body_sha256 = require_sha256(body_sha256, "body_sha256")
            if request_url_sha256 is not None:
                request_url_sha256 = require_sha256(request_url_sha256, "request_url_sha256")
            if endpoint is not None:
                endpoint = validate_provider_url(endpoint)
            finished = validate_clock(now=finished_at, transport_fixture=transport_fixture)
            headers = headers or {}
            event.update({"terminal": True, "event_type": "RESPONSE_RECEIVED" if status is not None else "REQUEST_FAILED", "finished_at_utc": iso_utc(finished), "status": status, "error_code": error_code, "body_sha256": body_sha256, "body_byte_count": body_byte_count, "retry_after": headers.get("retry-after"), "date": headers.get("date"), "content_type": headers.get("content-type"), "content_length": headers.get("content-length"), "etag": headers.get("etag"), "endpoint": endpoint, "request_url_sha256": request_url_sha256})
            run = state["runs"].get(str(event["run_id"]), {})
            artifact = run.get("artifacts", {}).get(str(event["artifact_id"]), {})
            artifact.update({"status": event["event_type"], "status_code": status})
            self._write(state)
            self.events.append(event)
            return event

    def record_429(self, host: str, retry_after: str | None, *, now: datetime | None = None, transport_fixture: bool = False) -> dict[str, Any]:
        if str(host).lower() not in DEFAULT_HOST_ALLOWLIST:
            raise ValueError("provider host is outside the locked allowlist")
        host = str(host).lower()
        observed = validate_clock(now=now, transport_fixture=transport_fixture)
        with ProviderProcessLock(self._state_lock_path()):
            state = self._load()
            item = dict(state["hosts"].get(host, {"circuit": "CLOSED", "consecutive_429": 0}))
            count = int(item.get("consecutive_429", 0)) + 1
            delay, parsed = rate_limit_delay(count, retry_after, now=observed, rng=self.rng)
            delay = max(delay, UNKNOWN_ATTEMPT_COOLDOWN_SECONDS) if count >= 5 else delay
            item.update({"circuit": "OPEN", "consecutive_429": count, "raw_retry_after": None if retry_after is None else str(retry_after), "parsed_retry_after_seconds": parsed, "selected_delay_seconds": delay, "next_retry_at_utc": iso_utc(datetime.fromtimestamp(observed.timestamp() + delay, timezone.utc)), "half_open_claimed": False, "unknown_attempt": False})
            state["hosts"][host] = item
            self._write(state)
            return item

    def record_success(self, host: str, *, now: datetime | None = None, transport_fixture: bool = False) -> dict[str, Any]:
        if str(host).lower() not in DEFAULT_HOST_ALLOWLIST:
            raise ValueError("provider host is outside the locked allowlist")
        host = str(host).lower()
        observed = validate_clock(now=now, transport_fixture=transport_fixture)
        with ProviderProcessLock(self._state_lock_path()):
            state = self._load()
            item = dict(state["hosts"].get(host, {}))
            item.update({"circuit": "CLOSED", "consecutive_429": 0, "last_success_at_utc": iso_utc(observed), "half_open_claimed": False, "unknown_attempt": False})
            state["hosts"][host] = item
            self._write(state)
            return item

    def record_terminal(self, host: str, code: str, *, now: datetime | None = None, transport_fixture: bool = False) -> dict[str, Any]:
        if str(host).lower() not in DEFAULT_HOST_ALLOWLIST:
            raise ValueError("provider host is outside the locked allowlist")
        host = str(host).lower()
        observed = validate_clock(now=now, transport_fixture=transport_fixture)
        with ProviderProcessLock(self._state_lock_path()):
            state = self._load()
            item = dict(state["hosts"].get(host, {"consecutive_429": 0}))
            item.update({"circuit": "TERMINAL", "terminal_error": code, "terminal_at_utc": iso_utc(observed), "half_open_claimed": False})
            state["hosts"][host] = item
            self._write(state)
            return item

    def record_transient_failure(self, host: str, code: str, *, now: datetime | None = None, transport_fixture: bool = False) -> dict[str, Any]:
        if str(host).lower() not in DEFAULT_HOST_ALLOWLIST:
            raise ValueError("provider host is outside the locked allowlist")
        host = str(host).lower()
        observed = validate_clock(now=now, transport_fixture=transport_fixture)
        with ProviderProcessLock(self._state_lock_path()):
            state = self._load()
            item = dict(state["hosts"].get(host, {"consecutive_429": 0, "transient_failure_count": 0}))
            count = int(item.get("transient_failure_count", 0)) + 1
            if count >= len(RETRY_DELAYS_SECONDS):
                item.update({"circuit": "TERMINAL", "terminal_error": code, "terminal_at_utc": iso_utc(observed), "half_open_claimed": False, "transient_failure_count": count})
            else:
                delay = retry_delay_for_attempt(count, rng=self.rng)
                item.update({"circuit": "OPEN", "transient_failure_count": count, "transient_failure_code": code, "selected_delay_seconds": delay, "next_retry_at_utc": iso_utc(datetime.fromtimestamp(observed.timestamp() + delay, timezone.utc)), "half_open_claimed": False, "unknown_attempt": False})
            state["hosts"][host] = item
            self._write(state)
            return item

    def record_http_status(self, status: int) -> dict[str, Any]:
        status_code = int(status)
        if status_code < 100 or status_code > 599:
            raise ValueError("HTTP status is invalid")
        with ProviderProcessLock(self._state_lock_path()):
            state = self._load()
            counts = dict(state.get("http_status_counts", {}))
            key = str(status_code)
            counts[key] = int(counts.get(key, 0)) + 1
            state["http_status_counts"] = counts
            self._write(state)
            return state


@dataclass(frozen=True, slots=True)
class ArtifactCheckpoint:
    host: str
    instrument: str
    timeframe: str
    utc_hour: str
    status: str
    body_sha256: str | None = None
    body_bytes: int | None = None
    acquired_at_utc: str | None = None
    decode_ok: bool = False
    range_ok: bool = False
    url_sha256: str | None = None

    @property
    def key(self) -> str:
        return "|".join((self.host, self.instrument, "BID", self.timeframe, self.utc_hour))


class HttpCas:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, body_sha256: str) -> Path:
        digest = require_sha256(body_sha256, "CAS body hash")
        return self.root / digest[:2] / f"{digest}.bi5"

    def put(self, body: bytes) -> tuple[str, Path]:
        digest = sha256_bytes(body)
        target = self.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_bytes(target.read_bytes()) != digest:
                raise ValueError("CAS collision or tamper detected")
            return digest, target
        part = target.with_name(f"{target.name}.part.{os.getpid()}.{uuid4().hex}")
        try:
            with part.open("wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part, target)
            _fsync_directory(target.parent)
        finally:
            part.unlink(missing_ok=True)
        return digest, target


class AcquisitionRunLedger:
    """SQLite-WAL source of truth for resumable acquisition progress."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None, check_same_thread=False)
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        if int(self.connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise ValueError("acquisition ledger requires SQLite foreign keys")
        journal_mode = None
        deadline = time.monotonic() + 30.0
        while journal_mode is None:
            try:
                journal_mode = str(self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        if journal_mode != "wal":
            raise ValueError("acquisition ledger requires SQLite WAL")
        self._lock = threading.RLock()
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS acquisition_runs (
                run_id TEXT PRIMARY KEY,
                started_at_utc TEXT NOT NULL,
                source_commit TEXT NOT NULL,
                source_tree_sha256 TEXT NOT NULL,
                process_identity_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS acquisition_artifacts (
                run_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                state TEXT NOT NULL,
                body_sha256 TEXT,
                body_bytes INTEGER,
                fixture_path TEXT NOT NULL,
                request_url TEXT,
                request_url_sha256 TEXT,
                expected_body_sha256 TEXT,
                expected_body_bytes INTEGER,
                lease_pid INTEGER,
                lease_process_start_token TEXT,
                lease_expires_at_utc TEXT,
                updated_at_utc TEXT NOT NULL,
                PRIMARY KEY (run_id, artifact_id),
                FOREIGN KEY (run_id) REFERENCES acquisition_runs(run_id)
            );
            """
        )
        columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(acquisition_artifacts)")}
        for name, definition in (
            ("request_url", "TEXT"),
            ("request_url_sha256", "TEXT"),
            ("expected_body_sha256", "TEXT"),
            ("expected_body_bytes", "INTEGER"),
            ("lease_pid", "INTEGER"),
            ("lease_process_start_token", "TEXT"),
            ("lease_expires_at_utc", "TEXT"),
        ):
            if name not in columns:
                self.connection.execute(f'ALTER TABLE acquisition_artifacts ADD COLUMN "{name}" {definition}')

    def _transaction(self):
        class Transaction:
            def __init__(self, owner: "AcquisitionRunLedger") -> None:
                self.owner = owner

            def __enter__(self):
                self.owner._lock.acquire()
                self.owner.connection.execute("BEGIN IMMEDIATE")
                return self.owner.connection

            def __exit__(self, exc_type, exc, tb):
                try:
                    self.owner.connection.rollback() if exc_type else self.owner.connection.commit()
                finally:
                    self.owner._lock.release()

        return Transaction(self)

    def start_run(self, run_id: str, *, source_commit: str, source_tree_sha256: str, repository_root: str | Path | None = None) -> dict[str, Any]:
        run_id = str(run_id).strip()
        if not run_id or not re.fullmatch(r"[0-9a-f]{40}", str(source_commit)) or not re.fullmatch(r"[0-9a-f]{40}", str(source_tree_sha256)):
            raise ValueError("acquisition run identity is incomplete")
        if repository_root is not None:
            from scripts.git_provenance import validate_production_source_binding
            validate_production_source_binding(Path(repository_root).resolve(), str(source_commit), str(source_tree_sha256))
        identity = _canonical({"pid": os.getpid(), "process_start_token": process_start_token(), "host": platform.node()}).decode("utf-8")
        with self._transaction() as connection:
            existing = connection.execute("SELECT source_commit, source_tree_sha256 FROM acquisition_runs WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None and tuple(existing) != (source_commit, source_tree_sha256):
                raise ValueError("acquisition run identity differs from the persisted source")
            connection.execute(
                "INSERT OR IGNORE INTO acquisition_runs(run_id,started_at_utc,source_commit,source_tree_sha256,process_identity_json) VALUES(?,?,?,?,?)",
                (run_id, iso_utc(utc_now()), source_commit, source_tree_sha256, identity),
            )
            row = connection.execute("SELECT run_id,started_at_utc,source_commit,source_tree_sha256,process_identity_json FROM acquisition_runs WHERE run_id=?", (run_id,)).fetchone()
        assert row is not None
        return {"run_id": row[0], "started_at_utc": row[1], "source_commit": row[2], "source_tree_sha256": row[3], "process_identity": json.loads(row[4])}

    def reserve_artifact(
        self,
        run_id: str,
        artifact_id: str,
        fixture_path: str,
        *,
        request_url: str | None = None,
        request_url_sha256: str | None = None,
        expected_body_sha256: str | None = None,
        expected_body_bytes: int | None = None,
    ) -> str:
        run_id, artifact_id, fixture_path = str(run_id).strip(), str(artifact_id).strip(), str(fixture_path).strip()
        if not run_id or not artifact_id or not fixture_path:
            raise ValueError("acquisition artifact identity is incomplete")
        if request_url is not None:
            request_url = str(request_url).strip()
            if request_url.startswith("https://"):
                request_url = validate_provider_url(request_url)
            elif not request_url.startswith("fixture://"):
                raise ValueError("acquisition request URL is outside the explicit mode contract")
        if request_url_sha256 is not None:
            request_url_sha256 = require_sha256(request_url_sha256, "request URL SHA-256")
        if request_url is not None and request_url_sha256 is None:
            request_url_sha256 = sha256_bytes(request_url.encode("utf-8"))
        if expected_body_sha256 is not None:
            expected_body_sha256 = require_sha256(expected_body_sha256, "expected body SHA-256")
        if expected_body_bytes is not None and (isinstance(expected_body_bytes, bool) or int(expected_body_bytes) < 0):
            raise ValueError("expected body byte count is invalid")
        pid = os.getpid()
        process_token = process_start_token(pid)
        now = utc_now()
        expires = iso_utc(now + timedelta(seconds=ACQUISITION_LEASE_SECONDS))
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM acquisition_runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise ValueError("acquisition artifact references an unknown run")
            existing = connection.execute(
                "SELECT state,request_url,request_url_sha256,expected_body_sha256,expected_body_bytes,lease_pid,lease_process_start_token,lease_expires_at_utc FROM acquisition_artifacts WHERE run_id=? AND artifact_id=?",
                (run_id, artifact_id),
            ).fetchone()
            if existing is not None:
                state = str(existing[0])
                if state == "COMMITTED":
                    persisted = tuple(existing[1:5])
                    requested = (request_url, request_url_sha256, expected_body_sha256, expected_body_bytes)
                    if any(value is not None for value in persisted) and persisted != requested:
                        raise ValueError("acquisition artifact identity differs from the persisted request")
                    return state
                if state != "IN_PROGRESS":
                    raise ValueError("acquisition artifact has an invalid persisted state")
                lease_pid = existing[5]
                lease_token = str(existing[6] or "")
                lease_expires = existing[7]
                lease_active = False
                if lease_pid is not None and lease_token and lease_expires:
                    try:
                        lease_active = parse_utc(str(lease_expires)) > now and process_start_token(int(lease_pid)) == lease_token
                    except (OSError, ValueError, TypeError):
                        lease_active = False
                if lease_active and (int(lease_pid) != pid or lease_token != process_token):
                    raise ValueError("acquisition artifact lease is held by another live process")
                connection.execute(
                    "UPDATE acquisition_artifacts SET fixture_path=?,request_url=?,request_url_sha256=?,expected_body_sha256=?,expected_body_bytes=?,lease_pid=?,lease_process_start_token=?,lease_expires_at_utc=?,updated_at_utc=? WHERE run_id=? AND artifact_id=? AND state='IN_PROGRESS'",
                    (fixture_path, request_url, request_url_sha256, expected_body_sha256, expected_body_bytes, pid, process_token, expires, iso_utc(now), run_id, artifact_id),
                )
                return state
            connection.execute(
                "INSERT INTO acquisition_artifacts(run_id,artifact_id,state,fixture_path,request_url,request_url_sha256,expected_body_sha256,expected_body_bytes,lease_pid,lease_process_start_token,lease_expires_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, artifact_id, "IN_PROGRESS", fixture_path, request_url, request_url_sha256, expected_body_sha256, expected_body_bytes, pid, process_token, expires, iso_utc(now)),
            )
        return "IN_PROGRESS"

    def complete_artifact(self, run_id: str, artifact_id: str, *, body_sha256: str, body_bytes: int) -> None:
        require_sha256(body_sha256, "acquisition body hash")
        if int(body_bytes) < 0:
            raise ValueError("acquisition body byte count is invalid")
        with self._transaction() as connection:
            updated = connection.execute(
                "UPDATE acquisition_artifacts SET state='COMMITTED',body_sha256=?,body_bytes=?,expected_body_sha256=COALESCE(expected_body_sha256,?),expected_body_bytes=COALESCE(expected_body_bytes,?),lease_pid=NULL,lease_process_start_token=NULL,lease_expires_at_utc=NULL,updated_at_utc=? WHERE run_id=? AND artifact_id=? AND state='IN_PROGRESS'",
                (body_sha256, int(body_bytes), body_sha256, int(body_bytes), iso_utc(utc_now()), str(run_id), str(artifact_id)),
            ).rowcount
            if updated == 0:
                existing = connection.execute("SELECT state,body_sha256,body_bytes FROM acquisition_artifacts WHERE run_id=? AND artifact_id=?", (str(run_id), str(artifact_id))).fetchone()
                if existing is not None and existing[0] == "COMMITTED" and existing[1] == body_sha256 and int(existing[2]) == int(body_bytes):
                    return
                raise ValueError("acquisition artifact is not an in-progress row")

    def artifact(self, run_id: str, artifact_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT run_id,artifact_id,state,body_sha256,body_bytes,fixture_path,request_url,request_url_sha256,expected_body_sha256,expected_body_bytes FROM acquisition_artifacts WHERE run_id=? AND artifact_id=?",
            (str(run_id), str(artifact_id)),
        ).fetchone()
        if row is None:
            return None
        return {
            "run_id": str(row[0]), "artifact_id": str(row[1]), "state": str(row[2]),
            "body_sha256": row[3], "body_bytes": row[4], "fixture_path": str(row[5]),
            "request_url": row[6], "request_url_sha256": row[7],
            "expected_body_sha256": row[8], "expected_body_bytes": row[9],
        }

    def close(self) -> None:
        self.connection.close()


class AcquisitionCoordinator:
    """Single SQLite-WAL coordinator with explicit fixture/provider modes."""

    def __init__(self, *, fixture_root: str | Path | None, cas_root: str | Path, ledger_path: str | Path, provider: Callable[..., Any] | None = None, mode: str = "fixture", rate_controller: RateLimitController | None = None, host: str = "datafeed.dukascopy.com") -> None:
        self.mode = str(mode).strip().lower()
        if self.mode not in {"fixture", "provider"}:
            raise ValueError("acquisition mode must be fixture or provider")
        if self.mode == "fixture":
            if provider is not None:
                raise ValueError("provider calls are forbidden in fixture mode")
            if fixture_root is None:
                raise ValueError("fixture root is required in fixture mode")
            self.fixture_root = Path(fixture_root).resolve()
            if not self.fixture_root.is_dir():
                raise ValueError("fixture root is missing")
        else:
            if fixture_root is not None:
                raise ValueError("fixture root is forbidden in provider mode")
            if provider is None:
                raise ValueError("provider callback is required in provider mode")
            if rate_controller is None:
                raise ValueError("provider mode requires the persisted rate controller")
            self.fixture_root = None
        self.provider = provider
        self.rate_controller = rate_controller
        self.host = str(host)
        self.cas = HttpCas(cas_root)
        self.ledger = AcquisitionRunLedger(ledger_path)

    def _fixture_bytes(self, fixture_path: str, *, expected_sha256: str, expected_bytes: int) -> bytes:
        relative = Path(str(fixture_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("fixture path escapes fixture root")
        assert self.fixture_root is not None
        path = (self.fixture_root / relative).resolve()
        try:
            path.relative_to(self.fixture_root)
        except ValueError as exc:
            raise ValueError("fixture path escapes fixture root") from exc
        if not path.is_file():
            raise ValueError("fixture file is missing")
        body = path.read_bytes()
        if len(body) != int(expected_bytes) or sha256_bytes(body) != require_sha256(expected_sha256, "fixture body hash"):
            raise ValueError("fixture bytes do not match the declared CAS identity")
        return body

    def _verify_committed(self, run_id: str, artifact_id: str) -> dict[str, Any]:
        record = self.ledger.artifact(run_id, artifact_id)
        if record is None or record["state"] != "COMMITTED":
            raise ValueError("persisted artifact is not committed")
        body_sha256 = require_sha256(record.get("body_sha256"), "committed artifact body hash")
        body_bytes = record.get("body_bytes")
        if isinstance(body_bytes, bool) or not isinstance(body_bytes, int) or body_bytes < 0:
            raise ValueError("committed artifact byte count is invalid")
        cas_path = self.cas.path_for(body_sha256)
        if not cas_path.is_file() or cas_path.stat().st_size != body_bytes or sha256_bytes(cas_path.read_bytes()) != body_sha256:
            raise ValueError("committed artifact CAS evidence is invalid")
        if not record.get("request_url") or record.get("request_url_sha256") != sha256_bytes(str(record["request_url"]).encode("utf-8")):
            raise ValueError("committed artifact URL binding is invalid")
        if record.get("expected_body_sha256") not in {None, body_sha256} or record.get("expected_body_bytes") not in {None, body_bytes}:
            raise ValueError("committed artifact does not match the persisted expected body identity")
        request_url = record.get("request_url")
        request_url_sha256 = record.get("request_url_sha256")
        if request_url is not None and request_url_sha256 != sha256_bytes(str(request_url).encode("utf-8")):
            raise ValueError("committed artifact URL binding is invalid")
        return {"run_id": run_id, "artifact_id": artifact_id, "state": "COMMITTED", "body_sha256": body_sha256, "body_bytes": body_bytes, "cas_path": str(cas_path), "request_url": request_url, "request_url_sha256": request_url_sha256}

    def _validate_provider_response(self, item: Mapping[str, Any], response: Any) -> tuple[bytes, dict[str, Any]]:
        request_url = validate_provider_url(str(item.get("url") or ""))
        request_url_sha256 = sha256_bytes(request_url.encode("utf-8"))
        if not isinstance(response, Mapping):
            raise ProviderResponseError("provider response evidence is not an object")
        raw_status = response.get("status")
        if isinstance(raw_status, bool):
            raise ProviderResponseError("provider response status is invalid")
        try:
            status = int(raw_status)
        except (TypeError, ValueError) as exc:
            raise ProviderResponseError("provider response status is invalid") from exc
        if response.get("url") != request_url:
            raise ProviderResponseError("provider response URL binding differs", status=status, error_code="PROVIDER_URL_MISMATCH", endpoint=request_url, request_url_sha256=request_url_sha256)
        headers = response.get("headers")
        if not isinstance(headers, Mapping):
            raise ProviderResponseError("provider response headers are invalid", status=status, error_code="PROVIDER_HEADERS_INVALID", endpoint=request_url, request_url_sha256=request_url_sha256)
        if status != 200:
            raise ProviderResponseError(f"provider HTTP status {status}", status=status, error_code=f"HTTP_{status}", headers=headers, endpoint=request_url, request_url_sha256=request_url_sha256)
        body = response.get("body")
        if not isinstance(body, bytes) or not body:
            raise ProviderResponseError("provider response body is empty or unavailable", status=status, error_code="PROVIDER_BODY_INVALID")
        body_sha256 = sha256_bytes(body)
        body_bytes = len(body)
        if response.get("body_sha256") != body_sha256 or response.get("body_byte_count") != body_bytes:
            raise ProviderResponseError("provider response body hash or size is not bound", status=status, error_code="PROVIDER_BODY_BINDING_MISMATCH")
        if response.get("buffer_sha256") is not None and response.get("buffer_sha256") != body_sha256:
            raise ProviderResponseError("provider response buffer hash is not bound", status=status, error_code="PROVIDER_BODY_BINDING_MISMATCH")
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                if int(str(content_length)) != body_bytes:
                    raise ProviderResponseError("provider response content-length differs", status=status, error_code="PROVIDER_SIZE_MISMATCH")
            except ValueError as exc:
                raise ProviderResponseError("provider response content-length is invalid", status=status, error_code="PROVIDER_SIZE_INVALID") from exc
        provenance = response.get("provenance")
        if not isinstance(provenance, Mapping) or provenance.get("provider") != "Dukascopy" or provenance.get("status") != 200 or provenance.get("url_sha256") != request_url_sha256 or provenance.get("body_sha256") != body_sha256 or provenance.get("body_byte_count") != body_bytes:
            raise ProviderResponseError("provider response provenance is incomplete or mismatched", status=status, error_code="PROVIDER_PROVENANCE_MISMATCH")
        expected_sha = item.get("sha256")
        expected_bytes = item.get("bytes")
        if expected_sha not in (None, "") and require_sha256(expected_sha, "expected provider body hash") != body_sha256:
            raise ProviderResponseError("provider response differs from the declared body hash", status=status, error_code="PROVIDER_BODY_BINDING_MISMATCH")
        if expected_bytes not in (None, ""):
            if isinstance(expected_bytes, bool) or int(expected_bytes) != body_bytes:
                raise ProviderResponseError("provider response differs from the declared body size", status=status, error_code="PROVIDER_SIZE_MISMATCH")
        return body, {"status": status, "headers": dict(headers), "endpoint": request_url, "request_url_sha256": request_url_sha256, "body_sha256": body_sha256, "body_byte_count": body_bytes}

    def run(self, *, run_id: str, source_commit: str, source_tree_sha256: str, artifacts: list[Mapping[str, Any]], repository_root: str | Path | None = None) -> list[dict[str, Any]]:
        self.ledger.start_run(run_id, source_commit=source_commit, source_tree_sha256=source_tree_sha256, repository_root=repository_root)
        output: list[dict[str, Any]] = []
        for item in artifacts:
            artifact_id = str(item.get("artifact_id") or "").strip()
            fixture_path = str(item.get("fixture_path") or "").strip()
            request_url = str(item.get("url") or f"fixture://{fixture_path}")
            if self.mode == "provider" and not item.get("url"):
                raise ValueError("provider artifact URL is required")
            expected_sha256 = str(item.get("sha256") or "") or None
            expected_bytes_raw = item.get("bytes")
            expected_bytes = None if expected_bytes_raw in (None, "") else int(expected_bytes_raw)
            if self.mode == "fixture" and (expected_sha256 is None or expected_bytes is None):
                raise ValueError("fixture artifact body identity is required")
            state = self.ledger.reserve_artifact(run_id, artifact_id, fixture_path or "provider", request_url=request_url, expected_body_sha256=expected_sha256, expected_body_bytes=expected_bytes)
            if state == "COMMITTED":
                output.append(self._verify_committed(run_id, artifact_id))
                continue
            if self.mode == "fixture":
                assert expected_sha256 is not None and expected_bytes is not None
                body = self._fixture_bytes(fixture_path, expected_sha256=expected_sha256, expected_bytes=expected_bytes)
            else:
                assert self.provider is not None
                assert self.rate_controller is not None
                request = self.rate_controller.before_request(self.host, run_id=run_id, artifact_id=artifact_id)
                try:
                    response = self.provider(item)
                    body, response_meta = self._validate_provider_response(item, response)
                    self.rate_controller.finish_request(request, status=200, body_sha256=response_meta["body_sha256"], body_byte_count=response_meta["body_byte_count"], headers=response_meta["headers"], endpoint=response_meta["endpoint"], request_url_sha256=response_meta["request_url_sha256"])
                    self.rate_controller.record_success(self.host)
                except ProviderResponseError as exc:
                    status = exc.status
                    self.rate_controller.finish_request(request, status=status, error_code=exc.error_code, headers=exc.headers, endpoint=exc.endpoint or request_url, request_url_sha256=exc.request_url_sha256 or sha256_bytes(request_url.encode("utf-8")))
                    if status is not None and 100 <= status <= 599:
                        self.rate_controller.record_http_status(status)
                    if status == 429:
                        item_state = self.rate_controller.record_429(self.host, exc.headers.get("retry-after"))
                        raise AcquisitionDeferred(str(item_state["next_retry_at_utc"]), "DEFERRED_RATE_LIMIT") from exc
                    if status in {404, 410}:
                        self.rate_controller.record_terminal(self.host, "SOURCE_ARTIFACT_MISSING")
                        raise AcquisitionTerminalError("SOURCE_ARTIFACT_MISSING") from exc
                    if status is not None and 500 <= status <= 599:
                        item_state = self.rate_controller.record_transient_failure(self.host, f"HTTP_{status}")
                        if item_state.get("circuit") == "TERMINAL":
                            raise AcquisitionTerminalError(f"HTTP_{status}_RETRY_EXHAUSTED") from exc
                        raise AcquisitionDeferred(str(item_state["next_retry_at_utc"]), "DEFERRED_PROVIDER_FAILURE") from exc
                    self.rate_controller.record_terminal(self.host, exc.error_code)
                    raise AcquisitionTerminalError(str(exc)) from exc
                except Exception as exc:
                    error_code = "TRANSPORT_TIMEOUT" if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutExpired" else "PROVIDER_PROCESS_CRASH" if isinstance(exc, (BrokenPipeError, ConnectionError, RuntimeError)) else "PROVIDER_ERROR"
                    self.rate_controller.finish_request(request, error_code=error_code, endpoint=request_url, request_url_sha256=sha256_bytes(request_url.encode("utf-8")))
                    self.rate_controller.record_transient_failure(self.host, error_code)
                    raise ProviderResponseError("provider callback failed", error_code=error_code) from exc
            if self.mode == "provider":
                assert isinstance(body, bytes) and body
            body_sha256, cas_path = self.cas.put(body)
            if not cas_path.is_file() or sha256_bytes(cas_path.read_bytes()) != body_sha256:
                raise ValueError("CAS readback verification failed")
            self.ledger.complete_artifact(run_id, artifact_id, body_sha256=body_sha256, body_bytes=len(body))
            output.append({"run_id": run_id, "artifact_id": artifact_id, "state": "COMMITTED", "body_sha256": body_sha256, "body_bytes": len(body), "cas_path": str(cas_path)})
        return output

    def close(self) -> None:
        self.ledger.close()


def checkpoint_key(host: str, instrument: str, timeframe: str, utc_hour: str) -> str:
    return ArtifactCheckpoint(host, instrument, timeframe, utc_hour, "PENDING").key


def retry_delay_for_attempt(attempt: int, *, rng: Callable[[], int] | None = None) -> float:
    if not 1 <= int(attempt) <= len(RETRY_DELAYS_SECONDS):
        raise ValueError("artifact retry attempt must be 1..4")
    base = RETRY_DELAYS_SECONDS[int(attempt) - 1]
    return base + _positive_jitter(base, rng)


def migration_checkpoint(root: str | Path, *, now: datetime | None = None, transport_fixture: bool = False) -> dict[str, Any]:
    path = Path(root) / STATE_RELATIVE
    controller = RateLimitController(path)
    legacy = Path(root) / "outputs/reports/reacquire_invalid_sessions_v4.log"
    if legacy.is_file():
        return controller.migrate_legacy_log(legacy, now=now, transport_fixture=transport_fixture)
    state = controller._load()
    state["migration_blocker"] = "PRIVATE_LEGACY_LOG_MISSING"
    controller._write(state)
    return state
