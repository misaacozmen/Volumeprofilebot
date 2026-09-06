from __future__ import annotations

"""Fail-closed local runtime guards shared by Super1 order paths and tests."""

from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import threading
import uuid
from zoneinfo import ZoneInfo
from typing import Any, Iterator


ORDER_MUTEX_NAME = r"Global\Super1OrderTransport"
LEASE_FILE_NAME = "session-lease.json"
ALLOWED_LEASE_STATES = {"ACTIVE", "REVOKED"}
LEASE_FIELDS = {
    "schema_version",
    "lease_id",
    "campaign_id",
    "trade_date_ny",
    "issued_at_utc",
    "not_before_utc",
    "order_not_before_utc",
    "expires_at_utc",
    "release_id",
    "app_manifest_sha256",
    "config_sha256",
    "candidate_sha256",
    "harness_sha256",
    "runner_sid",
    "machine_binding",
    "expected_account_login",
    "expected_server",
    "expected_company",
    "magic_number",
    "mode",
    "state",
    "revoked_at_utc",
    "revocation_reason",
}


class Super1RuntimeError(RuntimeError):
    """A runtime safety gate failed; callers must not send."""


class AccountBindingMismatchError(Super1RuntimeError):
    code = "ACCOUNT_BINDING_MISMATCH_NO_SEND"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class UnknownNoSendError(Super1RuntimeError):
    code = "UNKNOWN_NO_SEND"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def trade_date_ny(now: datetime | None = None) -> str:
    """Derive the lease day from the same injectable UTC clock used by gates."""
    observed = utc_now() if now is None else now
    if observed.tzinfo is None:
        raise Super1RuntimeError("Runtime clock must be timezone-aware.")
    return observed.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise Super1RuntimeError(f"Lease {field} is missing.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Super1RuntimeError(f"Lease {field} is invalid.") from exc
    if parsed.tzinfo is None:
        raise Super1RuntimeError(f"Lease {field} must be timezone-aware.")
    return parsed.astimezone(timezone.utc)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise Super1RuntimeError(f"Protected runtime file is missing or reparse-backed: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Super1RuntimeError(f"Protected runtime JSON is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise Super1RuntimeError(f"Protected runtime JSON is not an object: {path.name}")
    return value


def _current_user_sid() -> str:
    if os.name != "nt":
        return ""
    try:
        import subprocess

        output = subprocess.check_output(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Super1RuntimeError("Could not resolve the current Runner SID.") from exc
    match = re.search(r'"(S-\d-(?:\d+-)+\d+)"', output)
    if not match:
        raise Super1RuntimeError("Could not prove the current Runner SID.")
    return match.group(1)


def machine_binding() -> str:
    name = os.environ.get("COMPUTERNAME", "").strip() or platform.node()
    if not name:
        raise Super1RuntimeError("Machine binding is unavailable.")
    return name.upper()


def load_runtime_config(app_root: Path) -> tuple[dict[str, Any], Path, str]:
    path = app_root / "live_forward" / "super1_xm_mt5_demo_config.json"
    config = _read_json_object(path)
    required = {
        "schema_version", "environment", "account_mode", "account_login", "expected_server",
        "expected_company", "magic_number", "execution", "manual_required", "rth_session_calendar",
        "terminal_path", "target", "isolation_required", "demo_order_execution_enabled",
        "real_money_live_enabled", "real_money_execution_allowed", "daily_manual_start_required",
        "unattended_execution_allowed",
    }
    if not required.issubset(config):
        raise Super1RuntimeError("Signed Super1 runtime config is incomplete.")
    if (
        config.get("environment") != "XM_MT5_DEMO_ORDER"
        or config.get("account_mode") != "DEMO_ORDER"
        or config.get("execution") != "MT5_DEMO_ORDERS"
        or config.get("manual_required") is not False
        or config.get("terminal_path") != r"C:\Super1\mt5\terminal64.exe"
        or config.get("target") != "LOCAL_WINDOWS_PC"
        or config.get("isolation_required") is not True
        or config.get("demo_order_execution_enabled") is not True
        or config.get("real_money_live_enabled") is not False
        or config.get("real_money_execution_allowed") is not False
        or config.get("daily_manual_start_required") is not True
        or config.get("unattended_execution_allowed") is not False
        or not isinstance(config.get("account_login"), int)
        or config.get("account_login", 0) <= 0
        or not str(config.get("expected_server") or "")
        or not str(config.get("expected_company") or "")
        or not isinstance(config.get("magic_number"), int)
    ):
        raise Super1RuntimeError("Signed Super1 runtime config failed the demo-only identity gate.")
    return config, path, file_sha256(path)


def load_runtime_manifest(app_root: Path) -> tuple[dict[str, Any], Path, str]:
    path = app_root / "research_candidates" / "super1" / "super1_manifest.json"
    manifest = _read_json_object(path)
    deployment = manifest.get("deployment")
    if not isinstance(deployment, dict):
        raise Super1RuntimeError("Super1 release manifest has no deployment contract.")
    required = {
        "target": "LOCAL_WINDOWS_PC",
        "isolation_required": True,
        "demo_order_execution_enabled": True,
        "real_money_live_enabled": False,
        "real_money_execution_allowed": False,
        "existing_campaign_must_remain_untouched": True,
        "daily_manual_start_required": True,
        "unattended_execution_allowed": False,
    }
    if any(deployment.get(key) != value for key, value in required.items()):
        raise Super1RuntimeError("Super1 release manifest is not local-demo/manual-only.")
    return manifest, path, file_sha256(path)


def _lease_path(root: Path) -> Path:
    return root / "control" / LEASE_FILE_NAME


def load_lease(root: Path, config: dict[str, Any], manifest_sha256: str | None = None, *, for_order: bool = True) -> dict[str, Any]:
    path = _lease_path(root)
    lease = _read_json_object(path)
    if set(lease) - LEASE_FIELDS or int(lease.get("schema_version", 0)) != 1:
        raise Super1RuntimeError("Session lease schema is invalid.")
    try:
        uuid.UUID(str(lease["lease_id"]))
    except (KeyError, ValueError, AttributeError) as exc:
        raise Super1RuntimeError("Session lease id is invalid.") from exc
    if lease.get("state") not in ALLOWED_LEASE_STATES:
        raise Super1RuntimeError("Session lease state is not allowlisted.")
    if lease.get("state") != "ACTIVE":
        raise Super1RuntimeError("Session lease is revoked.")
    now = utc_now()
    if not str(lease.get("campaign_id") or "").strip() or not str(lease.get("release_id") or "").strip():
        raise Super1RuntimeError("Session lease campaign/release binding is missing.")
    expected_trade_date = trade_date_ny(now)
    if str(lease.get("trade_date_ny")) != expected_trade_date:
        raise Super1RuntimeError("Session lease is bound to another New York trade date.")
    not_before = parse_utc(lease.get("not_before_utc"), "not_before_utc")
    expires = parse_utc(lease.get("expires_at_utc"), "expires_at_utc")
    if not_before >= expires or now < not_before or now >= expires:
        raise Super1RuntimeError("Session lease is outside its validity interval.")
    if any(
        not isinstance(lease.get(field), str) or not re.fullmatch(r"[a-f0-9]{64}", lease[field], re.I)
        for field in ("app_manifest_sha256", "config_sha256", "candidate_sha256", "harness_sha256")
    ):
        raise Super1RuntimeError("Session lease hash binding is invalid.")
    if lease.get("mode") != "DEMO_ORDER":
        raise Super1RuntimeError("Session lease mode is not DEMO_ORDER.")
    expected = {
        "expected_account_login": config.get("account_login"),
        "expected_server": config.get("expected_server"),
        "expected_company": config.get("expected_company"),
        "magic_number": config.get("magic_number"),
    }
    if any(lease.get(key) != value for key, value in expected.items()):
        raise AccountBindingMismatchError("Session lease identity differs from signed config.")
    if str(lease.get("machine_binding")) != machine_binding():
        raise Super1RuntimeError("Session lease is bound to another machine.")
    runner_sid = str(lease.get("runner_sid") or "")
    if os.name == "nt" and runner_sid != _current_user_sid():
        raise Super1RuntimeError("Session lease is bound to another Runner SID.")
    order_not_before = parse_utc(
        lease.get("order_not_before_utc", lease.get("not_before_utc")),
        "order_not_before_utc",
    )
    if for_order and now < order_not_before:
        raise Super1RuntimeError("Session lease is not yet order-enabled.")
    if manifest_sha256 is not None and str(lease.get("app_manifest_sha256")) != manifest_sha256:
        raise Super1RuntimeError("Session lease release manifest binding differs.")
    return lease


_fallback_locks: dict[str, threading.RLock] = {}
_fallback_locks_guard = threading.Lock()


@contextmanager
def order_mutex(timeout_seconds: float = 30.0) -> Iterator[None]:
    """Hold the same named transport mutex used by PowerShell stop/start."""
    if os.name != "nt":
        with _fallback_locks_guard:
            lock = _fallback_locks.setdefault(ORDER_MUTEX_NAME, threading.RLock())
        if not lock.acquire(timeout=timeout_seconds):
            raise UnknownNoSendError("Could not acquire the order transport mutex.")
        try:
            yield
        finally:
            lock.release()
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    wait = kernel32.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait.restype = ctypes.c_uint32
    release = kernel32.ReleaseMutex
    release.argtypes = [ctypes.c_void_p]
    release.restype = ctypes.c_bool
    close = kernel32.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_bool
    handle = create_mutex(None, False, ORDER_MUTEX_NAME)
    if not handle:
        raise UnknownNoSendError("Could not create the order transport mutex.")
    try:
        result = wait(handle, max(1, int(timeout_seconds * 1000)))
        if result not in (0x00000000, 0x00000080):
            raise UnknownNoSendError("Could not acquire the order transport mutex.")
        yield
        if not release(handle):
            raise UnknownNoSendError("Could not release the order transport mutex.")
    finally:
        close(handle)


def assert_account_binding(mt5: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Require every terminal/account permission and exact signed identity."""
    account = mt5.account_info()
    terminal = mt5.terminal_info()
    if account is None or terminal is None:
        raise UnknownNoSendError("MT5 identity or terminal state is unavailable.")
    checks = {
        "terminal_connected": bool(getattr(terminal, "connected", False)),
        "terminal_trade_allowed": bool(getattr(terminal, "trade_allowed", False)),
        "api_trading_enabled": not bool(getattr(terminal, "tradeapi_disabled", True)),
        "account_trade_allowed": bool(getattr(account, "trade_allowed", False)),
        "account_expert_allowed": bool(getattr(account, "trade_expert", False)),
        "demo_trade_mode": int(getattr(account, "trade_mode", -1))
        == int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)),
        "login": int(getattr(account, "login", -1)) == int(config["account_login"]),
        "server": str(getattr(account, "server", "")) == str(config["expected_server"]),
        "company": str(getattr(account, "company", "")) == str(config["expected_company"]),
    }
    if not all(checks.values()):
        raise AccountBindingMismatchError(f"Broker binding or permission gate failed: {checks}")
    return {
        "login": int(account.login),
        "server": str(account.server),
        "company": str(account.company),
        "trade_mode": int(account.trade_mode),
        "checks": checks,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
