"""Fail-closed, resumable Dukascopy acquisition primitives.

The module owns policy and durable state.  The Node helper owns the locked
dukascopy-node primitives and returns structured HTTP evidence to this layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import os
import platform
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable, Mapping


STATE_RELATIVE = Path("outputs/reports/.dukascopy_acquisition_v5/state.json")
LEGACY_LOG_SHA256 = "895104f716a9f36042f2843fe560315d1dbd249cd47121801dc5f7dcc74a0688"
MIGRATION_COOLDOWN_UTC = "2026-09-15T15:33:39.3919524Z"
DEFAULT_HOST_ALLOWLIST = frozenset({"datafeed.dukascopy.com"})
RETRYABLE_STATUS = frozenset({408, 425, 500, 502, 503, 504})
RETRY_DELAYS_SECONDS = (5.0, 15.0, 45.0, 120.0)
RATE_LIMIT_BASES_SECONDS = (60.0, 120.0, 240.0, 480.0, 900.0)
SHA256_LENGTH = 64


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("UTC timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("UTC timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse delta-seconds or HTTP-date, returning a non-negative delay."""
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
    draw = (rng or (lambda: secrets.randbelow(1001) + 1))()
    fraction = max(1, min(1001, int(draw))) / 10000.0
    return base * fraction


def rate_limit_delay(
    consecutive_429: int,
    retry_after: str | None,
    *,
    now: datetime | None = None,
    rng: Callable[[], int] | None = None,
) -> tuple[float, float | None]:
    """Return selected delay and parsed header delay without sleeping."""
    parsed = parse_retry_after(retry_after, now=now)
    index = max(1, min(int(consecutive_429), len(RATE_LIMIT_BASES_SECONDS))) - 1
    base = max(float(parsed or 0.0), RATE_LIMIT_BASES_SECONDS[index])
    return base + _positive_jitter(base, rng), parsed


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def validate_provider_url(url: str, *, allowlist: set[str] | frozenset[str] = DEFAULT_HOST_ALLOWLIST) -> str:
    raw = str(url).strip()
    match = re.fullmatch(r"https://([^/?#]+)(?:[/?#].*)?", raw, flags=re.IGNORECASE)
    authority = match.group(1) if match else ""
    host = authority.lower()
    if not match or "#" in raw or "@" in authority or ":" in authority or host not in allowlist:
        raise ValueError("provider URL is outside the locked HTTPS allowlist")
    return raw


class AcquisitionDeferred(RuntimeError):
    def __init__(self, next_retry_at_utc: str, reason: str = "DEFERRED_RATE_LIMIT") -> None:
        super().__init__(reason)
        self.reason = reason
        self.next_retry_at_utc = next_retry_at_utc


class AtomicJsonStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("acquisition state must be a JSON object")
        return value

    def write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_name(self.path.name + ".part")
        payload = _canonical(value) + b"\n"
        with part.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, self.path)
        try:
            directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


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
    """Provider-wide O_EXCL lock with PID and process-start-token identity."""

    def __init__(self, path: str | Path, *, pid: int | None = None, token: str | None = None) -> None:
        self.path = Path(path)
        self.pid = int(pid or os.getpid())
        self.token = str(token or process_start_token(self.pid))
        self.held = False

    def _metadata(self) -> dict[str, Any]:
        return {"pid": self.pid, "process_start_token": self.token, "host": platform.node(), "acquired_at_utc": iso_utc(utc_now())}

    @staticmethod
    def _is_stale(value: Mapping[str, Any], *, process_alive: Callable[[int], bool] | None = None, token_for_pid: Callable[[int], str] | None = None) -> bool:
        try:
            pid = int(value["pid"])
            token = str(value["process_start_token"])
        except (KeyError, TypeError, ValueError):
            return True
        alive = process_alive(pid) if process_alive else _pid_alive(pid)
        if not alive:
            return True
        current = token_for_pid(pid) if token_for_pid else process_start_token(pid)
        return current != token

    def acquire(self, *, process_alive: Callable[[int], bool] | None = None, token_for_pid: Callable[[int], str] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = {}
                if not self._is_stale(existing, process_alive=process_alive, token_for_pid=token_for_pid):
                    raise RuntimeError("Dukascopy provider lock is held")
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self._metadata(), handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            self.held = True
            return
        raise RuntimeError("unable to acquire Dukascopy provider lock")

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False

    def __enter__(self) -> "ProviderProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class RateLimitController:
    def __init__(self, state_path: str | Path, *, rng: Callable[[], int] | None = None) -> None:
        self.store = AtomicJsonStore(state_path)
        self.rng = rng

    def _load(self) -> dict[str, Any]:
        state = self.store.read()
        state.setdefault("schema_version", 1)
        state.setdefault("hosts", {})
        return state

    def migrate_legacy_log(self, log_path: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
        digest = sha256(Path(log_path).read_bytes()).hexdigest()
        if digest != LEGACY_LOG_SHA256:
            raise ValueError("legacy rate-limit log hash mismatch")
        state = self._load()
        host = next(iter(DEFAULT_HOST_ALLOWLIST))
        state["hosts"][host] = {
            "circuit": "OPEN",
            "consecutive_429": 5,
            "next_retry_at_utc": MIGRATION_COOLDOWN_UTC,
            "migration_log_sha256": digest,
        }
        self.store.write(state)
        return state

    def before_request(self, host: str, *, now: datetime | None = None) -> dict[str, Any]:
        observed = now or utc_now()
        state = self._load()
        item = dict(state["hosts"].get(host, {"circuit": "CLOSED", "consecutive_429": 0}))
        retry_at = item.get("next_retry_at_utc")
        if item.get("circuit") == "OPEN" and retry_at:
            deadline = parse_utc(str(retry_at))
            if observed < deadline:
                raise AcquisitionDeferred(str(retry_at))
            item["circuit"] = "HALF_OPEN"
        if item.get("circuit") == "HALF_OPEN" and item.get("half_open_in_flight"):
            raise AcquisitionDeferred(str(retry_at or iso_utc(observed)))
        if item.get("circuit") == "HALF_OPEN":
            item["half_open_in_flight"] = True
        item["last_request_started_at_utc"] = iso_utc(observed)
        state["hosts"][host] = item
        self.store.write(state)
        return item

    def record_429(self, host: str, retry_after: str | None, *, now: datetime | None = None) -> dict[str, Any]:
        observed = now or utc_now()
        state = self._load()
        item = dict(state["hosts"].get(host, {"circuit": "CLOSED", "consecutive_429": 0}))
        count = int(item.get("consecutive_429", 0)) + 1
        delay, parsed = rate_limit_delay(count, retry_after, now=observed, rng=self.rng)
        if count >= 5:
            item["circuit"] = "OPEN"
            delay = 24 * 60 * 60
        else:
            item["circuit"] = "OPEN"
        next_retry = observed.timestamp() + delay
        item.update({
            "consecutive_429": count,
            "raw_retry_after": None if retry_after is None else str(retry_after),
            "parsed_retry_after_seconds": parsed,
            "selected_delay_seconds": delay,
            "next_retry_at_utc": iso_utc(datetime.fromtimestamp(next_retry, timezone.utc)),
            "half_open_in_flight": False,
        })
        state["hosts"][host] = item
        self.store.write(state)
        return item

    def record_success(self, host: str, *, now: datetime | None = None) -> dict[str, Any]:
        state = self._load()
        state["hosts"][host] = {
            "circuit": "CLOSED", "consecutive_429": 0,
            "last_success_at_utc": iso_utc(now or utc_now()),
        }
        self.store.write(state)
        return state["hosts"][host]


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

    @property
    def key(self) -> str:
        return "|".join((self.host, self.instrument, "BID", self.timeframe, self.utc_hour))


class HttpCas:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, body_sha256: str) -> Path:
        if len(body_sha256) != SHA256_LENGTH:
            raise ValueError("CAS body hash is invalid")
        return self.root / body_sha256[:2] / f"{body_sha256}.bi5"

    def put(self, body: bytes) -> tuple[str, Path]:
        digest = sha256_bytes(body)
        target = self.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_bytes(target.read_bytes()) != digest:
                raise ValueError("CAS collision or tamper detected")
            return digest, target
        part = target.with_name(target.name + ".part")
        with part.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, target)
        return digest, target


def checkpoint_key(host: str, instrument: str, timeframe: str, utc_hour: str) -> str:
    return ArtifactCheckpoint(host, instrument, timeframe, utc_hour).key


def retry_delay_for_attempt(attempt: int, *, rng: Callable[[], int] | None = None) -> float:
    if not 1 <= int(attempt) <= len(RETRY_DELAYS_SECONDS):
        raise ValueError("artifact retry attempt must be 1..4")
    base = RETRY_DELAYS_SECONDS[int(attempt) - 1]
    return base + _positive_jitter(base, rng)


def migration_checkpoint(root: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    path = Path(root) / STATE_RELATIVE
    controller = RateLimitController(path)
    legacy = Path(root) / "outputs/reports/reacquire_invalid_sessions_v4.log"
    if legacy.is_file():
        return controller.migrate_legacy_log(legacy, now=now)
    state = controller._load()
    state["migration_blocker"] = "PRIVATE_LEGACY_LOG_MISSING"
    controller.store.write(state)
    return state
