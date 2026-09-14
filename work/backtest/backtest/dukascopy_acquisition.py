"""Fail-closed, resumable Dukascopy acquisition policy and durable evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import secrets
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
        return f"pid:{pid}"


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

    def migrate_legacy_log(self, log_path: str | Path, *, now: datetime | None = None, transport_fixture: bool = False) -> dict[str, Any]:
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
        state = self._load()
        state["runs"].setdefault(str(run_id), {"provider_call_count": 0, "artifacts": {}, "started_at_utc": iso_utc(utc_now())})
        self._write(state)
        return state["runs"][str(run_id)]

    def before_request(self, host: str, *, now: datetime | None = None, run_id: str = "default", artifact_id: str = "unknown", transport_fixture: bool = False) -> dict[str, Any]:
        if str(host).lower() not in DEFAULT_HOST_ALLOWLIST:
            raise ValueError("provider host is outside the locked allowlist")
        host = str(host).lower()
        validate_clock(now=now, transport_fixture=transport_fixture)
        lock_path = self.store.path.with_name(f"{self.store.path.name}.state.lock")
        with ProviderProcessLock(lock_path):
            return self._before_request_locked(host, now=now, run_id=run_id, artifact_id=artifact_id, transport_fixture=transport_fixture)

    def _before_request_locked(self, host: str, *, now: datetime | None, run_id: str, artifact_id: str, transport_fixture: bool) -> dict[str, Any]:
        observed = now or utc_now()
        state = self._load()
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
        observed = validate_clock(now=now, transport_fixture=transport_fixture)
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
        observed = validate_clock(now=now, transport_fixture=transport_fixture)
        state = self._load()
        state["hosts"][host] = {"circuit": "CLOSED", "consecutive_429": 0, "last_success_at_utc": iso_utc(observed), "half_open_claimed": False}
        self._write(state)
        return state["hosts"][host]

    def record_terminal(self, host: str, code: str, *, now: datetime | None = None, transport_fixture: bool = False) -> dict[str, Any]:
        if str(host).lower() not in DEFAULT_HOST_ALLOWLIST:
            raise ValueError("provider host is outside the locked allowlist")
        observed = validate_clock(now=now, transport_fixture=transport_fixture)
        state = self._load()
        item = dict(state["hosts"].get(host, {"consecutive_429": 0}))
        item.update({"circuit": "TERMINAL", "terminal_error": code, "terminal_at_utc": iso_utc(observed), "half_open_claimed": False})
        state["hosts"][host] = item
        self._write(state)
        return item


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
