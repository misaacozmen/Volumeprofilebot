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
    "authorized_operator_sid",
    "machine_binding",
    "expected_account_login",
    "expected_server",
    "expected_company",
    "magic_number",
    "mode",
    "state",
    "revoked_at_utc",
    "revocation_reason",
    "invocation_nonce",
    "release_manifest_sha256",
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
    name = platform.node().strip()
    if not name:
        raise Super1RuntimeError("Machine binding is unavailable.")
    return name.upper()


def load_runtime_config(
    app_root: Path,
    config_path: Path | None = None,
    *,
    allow_unsigned_placeholder: bool = False,
) -> tuple[dict[str, Any], Path, str]:
    """Load a protected config, using the deployed alias only when requested.

    Source validation must bind the active versioned V4 bytes.  The signed
    release builder materializes the legacy deployed filename from those exact
    bytes for installers and the installed runtime guard.
    """
    path = Path(config_path) if config_path is not None else app_root / "live_forward" / "super1_xm_mt5_demo_config.json"
    if not path.is_absolute():
        path = app_root / path
    config = _read_json_object(path)
    required = {
        "schema_version", "environment", "account_mode", "account_login", "expected_server",
        "expected_company", "magic_number", "execution", "manual_required", "live_order_approval_required", "authorized_operator_sid", "rth_session_calendar",
        "terminal_path", "target", "isolation_required", "demo_order_execution_enabled",
        "real_money_live_enabled", "real_money_execution_allowed", "daily_manual_start_required",
        "unattended_execution_allowed",
    }
    if not required.issubset(config):
        raise Super1RuntimeError("Signed Super1 runtime config is incomplete.")
    account_login_valid = isinstance(config.get("account_login"), int) and config.get("account_login", 0) > 0
    unsigned_placeholder = (
        allow_unsigned_placeholder
        and config.get("status") == "UNSIGNED_VALIDATION_ONLY"
        and config.get("account_login") is None
    )
    if (
        config.get("environment") != "XM_MT5_DEMO_ORDER"
        or config.get("account_mode") != "DEMO_ORDER"
        or config.get("execution") != "MT5_DEMO_ORDERS"
        or config.get("manual_required") is not False
        or config.get("live_order_approval_required") is not True
        or config.get("terminal_path") != r"C:\Super1\mt5\terminal64.exe"
        or config.get("target") != "LOCAL_WINDOWS_PC"
        or config.get("isolation_required") is not True
        or config.get("demo_order_execution_enabled") is not True
        or config.get("real_money_live_enabled") is not False
        or config.get("real_money_execution_allowed") is not False
        or config.get("daily_manual_start_required") is not True
        or config.get("unattended_execution_allowed") is not False
        or not (account_login_valid or unsigned_placeholder)
        or not str(config.get("expected_server") or "")
        or not str(config.get("expected_company") or "")
        or not isinstance(config.get("magic_number"), int)
        or not re.fullmatch(r"S-\d-(?:\d+-)+\d+", str(config.get("authorized_operator_sid") or ""))
    ):
        raise Super1RuntimeError("Signed Super1 runtime config failed the demo-only identity gate.")
    return config, path, file_sha256(path)


def current_process_sid() -> str:
    """Return the SID proven by the current Windows process token."""
    return _current_user_sid()


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


def runtime_binding_expectations(root: Path, config: dict[str, Any]) -> dict[str, str]:
    """Derive every immutable binding from the installed, signed runtime."""
    root = root.resolve()
    app_root = root / "app"
    config_path = app_root / "live_forward" / "super1_xm_mt5_demo_config.json"
    manifest_path = app_root / "research_candidates" / "super1" / "super1_manifest.json"
    candidate_path = (app_root / str(config.get("candidate_path") or "")).resolve()
    try:
        candidate_path.relative_to(app_root.resolve())
    except ValueError as exc:
        raise Super1RuntimeError("Installed Super1 candidate path escapes the app root.") from exc
    if not config_path.is_file() or not manifest_path.is_file() or not candidate_path.is_file():
        raise Super1RuntimeError("Installed Super1 binding inputs are incomplete.")
    harness_paths = (
        app_root / "scripts" / "run_capital_forward.py",
        app_root / "live_forward" / "capital_demo_config.json",
        app_root / "forward_shadow" / "baseline_lock.json",
    )
    if any(not path.is_file() or path.is_symlink() for path in harness_paths):
        raise Super1RuntimeError("Installed Super1 harness inputs are incomplete.")
    harness_digest = hashlib.sha256()
    for path in harness_paths:
        harness_digest.update(path.name.encode("utf-8"))
        harness_digest.update(path.read_bytes())
    release_manifest_path = root / "super1-forward.manifest.json"
    if not release_manifest_path.is_file():
        raise Super1RuntimeError("Installed signed release manifest is missing.")
    try:
        release = _read_json_object(release_manifest_path)
    except Super1RuntimeError:
        raise
    return {
        "manifest_sha256": file_sha256(manifest_path),
        "config_sha256": file_sha256(config_path),
        "candidate_sha256": file_sha256(candidate_path),
        "harness_sha256": harness_digest.hexdigest(),
        "release_manifest_sha256": file_sha256(release_manifest_path),
        "release_id": str(release.get("release_id") or ""),
    }


def _validate_lease_payload(
    root: Path,
    lease: dict[str, Any],
    config: dict[str, Any],
    manifest_sha256: str | None,
    *,
    for_order: bool,
    principal_sid: str | None,
    principal_role: str,
    now: datetime,
    expected_bindings: dict[str, str] | None,
) -> dict[str, Any]:
    if set(lease) - LEASE_FIELDS or int(lease.get("schema_version", 0)) != 1:
        raise Super1RuntimeError("Session lease schema is invalid.")
    try:
        uuid.UUID(str(lease["lease_id"]))
        uuid.UUID(str(lease["invocation_nonce"]))
    except (KeyError, ValueError, AttributeError, TypeError) as exc:
        raise Super1RuntimeError("Session lease id or invocation nonce is invalid.") from exc
    if lease.get("state") not in ALLOWED_LEASE_STATES:
        raise Super1RuntimeError("Session lease state is not allowlisted.")
    if lease.get("state") != "ACTIVE":
        raise Super1RuntimeError("Session lease is revoked.")
    if not str(lease.get("campaign_id") or "").strip() or not str(lease.get("release_id") or "").strip():
        raise Super1RuntimeError("Session lease campaign/release binding is missing.")
    if str(lease.get("trade_date_ny")) != trade_date_ny(now):
        raise Super1RuntimeError("Session lease is bound to another New York trade date.")
    issued = parse_utc(lease.get("issued_at_utc"), "issued_at_utc")
    not_before = parse_utc(lease.get("not_before_utc"), "not_before_utc")
    order_not_before = parse_utc(
        lease.get("order_not_before_utc", lease.get("not_before_utc")),
        "order_not_before_utc",
    )
    expires = parse_utc(lease.get("expires_at_utc"), "expires_at_utc")
    if not issued <= not_before <= order_not_before < expires or now < not_before or now >= expires:
        raise Super1RuntimeError("Session lease is outside its validity interval.")
    if any(
        not isinstance(lease.get(field), str) or not re.fullmatch(r"[a-f0-9]{64}", lease[field], re.I)
        for field in (
            "app_manifest_sha256",
            "config_sha256",
            "candidate_sha256",
            "harness_sha256",
            "release_manifest_sha256",
        )
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
    if not re.fullmatch(r"S-\d-(?:\d+-)+\d+", runner_sid):
        raise Super1RuntimeError("Session lease Runner SID is invalid.")
    if for_order and now < order_not_before:
        raise Super1RuntimeError("Session lease is not yet order-enabled.")
    operator_sid = str(lease.get("authorized_operator_sid") or "")
    if not re.fullmatch(r"S-\d-(?:\d+-)+\d+", operator_sid) or operator_sid == runner_sid:
        raise Super1RuntimeError("Session lease authorized operator SID is missing or equals Runner SID.")
    if principal_role == "runner":
        if os.name == "nt" and runner_sid != _current_user_sid():
            raise Super1RuntimeError("Session lease is bound to another Runner SID.")
    elif principal_role == "operator":
        if principal_sid != operator_sid:
            raise Super1RuntimeError("Current interactive SID is not the authorized operator SID.")
    else:
        raise Super1RuntimeError("Unknown runtime principal role.")
    if manifest_sha256 is not None and str(lease.get("app_manifest_sha256")) != manifest_sha256:
        raise Super1RuntimeError("Session lease release manifest binding differs.")
    if expected_bindings:
        for field in (
            "manifest_sha256",
            "config_sha256",
            "candidate_sha256",
            "harness_sha256",
            "release_manifest_sha256",
            "release_id",
        ):
            if str(lease.get("app_manifest_sha256" if field == "manifest_sha256" else field)) != str(expected_bindings.get(field)):
                raise Super1RuntimeError(f"Session lease {field} binding differs from installed signed runtime.")
    return lease


def load_lease(
    root: Path,
    config: dict[str, Any],
    manifest_sha256: str | None = None,
    *,
    for_order: bool = True,
    principal_sid: str | None = None,
    principal_role: str = "runner",
    expected_bindings: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = _lease_path(root)
    lease = _read_json_object(path)
    return _validate_lease_payload(
        root,
        lease,
        config,
        manifest_sha256,
        for_order=for_order,
        principal_sid=principal_sid,
        principal_role=principal_role,
        now=utc_now() if now is None else now,
        expected_bindings=expected_bindings,
    )


def read_lease_state(
    root: Path,
    *,
    expected_bindings: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a safe watchdog classification without importing MT5 or credentials."""
    path = _lease_path(root)
    if not path.is_file():
        return {"state": "MISSING", "lease": None, "lease_id": "", "detail": "lease file is missing"}
    try:
        lease = _read_json_object(path)
        lease_id = str(lease.get("lease_id") or "")
        if lease.get("state") == "REVOKED":
            return {"state": "REVOKED", "lease": lease, "lease_id": lease_id, "detail": "lease is revoked"}
        observed = utc_now() if now is None else now
        try:
            expires = parse_utc(lease.get("expires_at_utc"), "expires_at_utc")
            if expires <= observed:
                return {"state": "EXPIRED", "lease": lease, "lease_id": lease_id, "detail": "lease expired"}
        except Super1RuntimeError:
            pass
        config, _, _ = load_runtime_config(root / "app")
        manifest, _, manifest_hash = load_runtime_manifest(root / "app")
        del manifest
        validated = load_lease(
            root,
            config,
            manifest_hash,
            for_order=False,
            expected_bindings=expected_bindings or runtime_binding_expectations(root, config),
            now=observed,
        )
        return {"state": "ACTIVE", "lease": validated, "lease_id": lease_id, "detail": "lease is valid"}
    except Super1RuntimeError as exc:
        return {"state": "INVALID", "lease": lease if "lease" in locals() else None, "lease_id": lease_id if "lease_id" in locals() else "", "detail": str(exc)}


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
