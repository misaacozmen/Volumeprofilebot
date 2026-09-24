"""Typed, mode-aware production environment loading with secret redaction."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import hashlib
import re
from typing import Mapping
import threading
from uuid import UUID


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlatformPaths:
    system_root: Path
    app_data: Path | None

    @classmethod
    def current(cls, environment: Mapping[str, str] | None = None) -> "PlatformPaths":
        env = os.environ if environment is None else environment
        root = Path(_get(env, "SystemRoot") or r"C:\Windows")
        app_data = _get(env, "APPDATA")
        return cls(root, Path(app_data) if app_data else None)


def environment_snapshot() -> dict[str, str]:
    """The only production escape hatch for passing the environment to validation."""
    return dict(os.environ)


def minimal_subprocess_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a non-secret platform-only environment for isolated workers."""
    result = worker_environment_subset(environment)
    result["PYTHONHASHSEED"] = "0"
    return result


def worker_environment_subset(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read only the central-schema worker keys from the supplied environment."""
    env = os.environ if environment is None else environment
    return {name: str(value) for name in WORKER_ENV_ALLOWLIST if (value := env.get(name))}


_ENVIRONMENT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "super1_environment_v1.schema.json"


def _load_environment_schema() -> dict[str, object]:
    try:
        payload = json.loads(_ENVIRONMENT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError("central environment schema is unavailable") from exc
    required = {"schema_version", "project_prefixes", "project_variables", "platform_variables", "ci_variables", "worker_allowed_variables"}
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema_version") != 1:
        raise SettingsError("central environment schema is not closed v1")
    return payload


_ENVIRONMENT_SCHEMA = _load_environment_schema()
SUPPORTED_ENV = frozenset(str(value) for value in _ENVIRONMENT_SCHEMA["project_variables"])
WORKER_ENV_ALLOWLIST = tuple(str(value) for value in _ENVIRONMENT_SCHEMA["worker_allowed_variables"])
SECRET_FIELDS = frozenset({"read_only_password", "password", "capital_api_key", "capital_api_password"})
_SID_PATTERN = re.compile(r"^S-\d-(?:\d+-)+\d+$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
PROJECT_ENV_PREFIXES = tuple(str(value) for value in _ENVIRONMENT_SCHEMA["project_prefixes"])


@dataclass(frozen=True, slots=True)
class EnvironmentField:
    key: str
    target: str
    value_type: str
    secret: bool = False
    source_policy: str = "ENV_ALLOWLIST"
    required_modes: frozenset[str] = frozenset()


ENVIRONMENT_SCHEMA = tuple(
    EnvironmentField(key, target, kind, secret)
    for key, target, kind, secret in (
        ("XM_MT5_TERMINAL_PATH", "terminal_path", "path", False),
        ("XM_MT5_SERVER", "server", "string", False),
        ("XM_MT5_ACCOUNT_LOGIN", "account_login", "integer", False),
        ("XM_MT5_SIGNED_SERVER_ASSERTION", "signed_server_assertion", "string", False),
        ("XM_MT5_READ_ONLY_PASSWORD", "read_only_password", "secret", True),
        ("XM_MT5_PASSWORD", "password", "secret", True),
        ("CAPITAL_IDENTIFIER", "capital_identifier", "secret", True),
        ("CAPITAL_API_KEY", "capital_api_key", "secret", True),
        ("CAPITAL_API_PASSWORD", "capital_api_password", "secret", True),
        ("SUPER1_INVOCATION_NONCE", "invocation_nonce", "uuid", False),
        ("SUPER1_RUNNER_SID", "runner_sid", "sid", False),
        ("SUPER1_LAUNCHER_SHA256", "launcher_sha256", "sha256", False),
        ("SUPER1_INVOCATION_STARTED_AT", "invocation_started_at", "timestamp", False),
        ("SUPER1_RUN_ID", "run_id", "string", False),
        ("SUPER1_EVIDENCE_MODE", "evidence_mode", "string", False),
        ("SUPER1_NEGATIVE_PHASE", "negative_phase", "string", False),
    )
)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    mode: str
    terminal_path: str = ""
    server: str = ""
    signed_server_assertion: str = ""
    terminal_sha256: str = ""
    read_only_password: str = ""
    password: str = ""
    capital_identifier: str = ""
    capital_api_key: str = ""
    capital_api_password: str = ""
    invocation_nonce: str = ""
    runner_sid: str = ""
    launcher_sha256: str = ""
    invocation_started_at: str = ""

    def __repr__(self) -> str:
        public = self.to_public_dict()
        return f"RuntimeSettings({public!r})"

    def to_public_dict(self) -> dict[str, str]:
        return {
            field.name: str(getattr(self, field.name))
            for field in fields(self)
            if field.name not in SECRET_FIELDS
        } | {field: "***" for field in SECRET_FIELDS if getattr(self, field)}

    def audit_payload(self) -> dict[str, str]:
        return self.to_public_dict()


def _get(environment: Mapping[str, str], name: str) -> str:
    return str(environment.get(name, "") or "").strip()


def validate_environment_keys(environment: Mapping[str, str]) -> None:
    """Reject unknown project variables while allowing ordinary platform env."""
    unknown = sorted(
        str(name) for name in environment
        if any(str(name).startswith(prefix) for prefix in PROJECT_ENV_PREFIXES)
        and str(name) not in SUPPORTED_ENV
    )
    if unknown:
        raise SettingsError(f"unknown project environment variable(s): {', '.join(unknown)}")


def _validate_invocation_fields(settings: RuntimeSettings, *, required: bool) -> None:
    fields_to_check = (
        ("SUPER1_INVOCATION_NONCE", settings.invocation_nonce),
        ("SUPER1_RUNNER_SID", settings.runner_sid),
        ("SUPER1_LAUNCHER_SHA256", settings.launcher_sha256),
        ("SUPER1_INVOCATION_STARTED_AT", settings.invocation_started_at),
    )
    missing = [name for name, value in fields_to_check if not value]
    if required and missing:
        raise SettingsError(f"protected invocation settings missing: {', '.join(missing)}")
    if settings.invocation_nonce:
        try:
            UUID(settings.invocation_nonce)
        except (ValueError, AttributeError, TypeError) as exc:
            raise SettingsError("SUPER1_INVOCATION_NONCE must be a UUID") from exc
    if settings.runner_sid and not _SID_PATTERN.fullmatch(settings.runner_sid):
        raise SettingsError("SUPER1_RUNNER_SID is not a canonical Windows SID")
    if settings.launcher_sha256 and not _SHA256_PATTERN.fullmatch(settings.launcher_sha256):
        raise SettingsError("SUPER1_LAUNCHER_SHA256 must be a SHA-256 digest")
    if settings.invocation_started_at:
        try:
            parsed = datetime.fromisoformat(settings.invocation_started_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise SettingsError("SUPER1_INVOCATION_STARTED_AT must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise SettingsError("SUPER1_INVOCATION_STARTED_AT must include a timezone")
        if parsed.astimezone(timezone.utc).timestamp() > datetime.now(timezone.utc).timestamp() + 5:
            raise SettingsError("SUPER1_INVOCATION_STARTED_AT cannot be materially in the future")


def environment_value(name: str, *, environment: Mapping[str, str] | None = None) -> str:
    """Read one allowlisted variable; unknown names are intentionally empty."""
    if name not in SUPPORTED_ENV:
        return ""
    return _get(os.environ if environment is None else environment, name)


def load_settings(
    config: Mapping[str, object],
    *,
    environment: Mapping[str, str] | None = None,
    enforce_required: bool = True,
) -> RuntimeSettings:
    env = os.environ if environment is None else environment
    validate_environment_keys(env)
    mode = str(config.get("account_mode") or config.get("environment") or "").upper()
    signed_terminal = str(config.get("terminal_path") or "").strip()
    signed_server = str(config.get("expected_server") or "").strip()
    env_terminal = _get(env, "XM_MT5_TERMINAL_PATH")
    env_server = _get(env, "XM_MT5_SERVER")
    if env_terminal and env_terminal != signed_terminal:
        raise SettingsError("XM_MT5_TERMINAL_PATH does not exactly match the signed terminal path")
    if env_server and env_server != signed_server:
        raise SettingsError("XM_MT5_SERVER does not exactly match the signed server")
    terminal_path = signed_terminal
    result = RuntimeSettings(
        mode=mode,
        terminal_path=terminal_path,
        server=signed_server,
        signed_server_assertion=_get(env, "XM_MT5_SIGNED_SERVER_ASSERTION") or signed_server,
        terminal_sha256=str(config.get("terminal_sha256") or "").strip(),
        read_only_password=_get(env, "XM_MT5_READ_ONLY_PASSWORD"),
        password=_get(env, "XM_MT5_PASSWORD"),
        capital_identifier=_get(env, "CAPITAL_IDENTIFIER"),
        capital_api_key=_get(env, "CAPITAL_API_KEY"),
        capital_api_password=_get(env, "CAPITAL_API_PASSWORD"),
        invocation_nonce=_get(env, "SUPER1_INVOCATION_NONCE"),
        runner_sid=_get(env, "SUPER1_RUNNER_SID"),
        launcher_sha256=_get(env, "SUPER1_LAUNCHER_SHA256"),
        invocation_started_at=_get(env, "SUPER1_INVOCATION_STARTED_AT"),
    )
    order_mode = mode in {"DEMO_ORDER", "XM_MT5_DEMO_ORDER", "MT5_DEMO_ORDERS"} or config.get("execution") == "MT5_DEMO_ORDERS"
    _validate_invocation_fields(result, required=order_mode and enforce_required)
    if order_mode and enforce_required:
        missing = [name for name, value in (("XM_MT5_TERMINAL_PATH", result.terminal_path), ("XM_MT5_SERVER", result.server), ("XM_MT5_PASSWORD", result.password or result.read_only_password)) if not value]
        if missing:
            raise SettingsError(f"demo-order settings missing: {', '.join(missing)}")
        terminal = Path(result.terminal_path)
        if not terminal.is_absolute() or terminal.name.casefold() != "terminal64.exe" or not terminal.is_file():
            raise SettingsError("signed terminal path must be an existing canonical terminal64.exe")
        canonical = str(terminal.resolve())
        if canonical != result.terminal_path:
            raise SettingsError("signed terminal path is not canonical")
        pinned = result.terminal_sha256
        if not pinned:
            raise SettingsError("pinned terminal SHA-256 is required")
        observed = hashlib.sha256(terminal.read_bytes()).hexdigest()
        if observed.casefold() != pinned.casefold():
            raise SettingsError("terminal SHA-256 does not match the signed pin")
    if result.signed_server_assertion != result.server:
        raise SettingsError("signed server assertion does not match the signed server")
    return result


def load_once(config: Mapping[str, object], *, environment: Mapping[str, str] | None = None) -> RuntimeSettings:
    settings = load_settings(config, environment=environment)
    fingerprint = hashlib.sha256(
        repr((dict(config), tuple(sorted((environment_snapshot() if environment is None else environment).items())))).encode()
    ).hexdigest()
    with _LOAD_LOCK:
        global _LOADED_FINGERPRINT, _LOADED_SETTINGS
        if _LOADED_FINGERPRINT is None:
            _LOADED_FINGERPRINT, _LOADED_SETTINGS = fingerprint, settings
        elif _LOADED_FINGERPRINT != fingerprint:
            raise SettingsError("runtime settings were already loaded from different inputs")
        return _LOADED_SETTINGS


_LOAD_LOCK = threading.Lock()
_LOADED_FINGERPRINT: str | None = None
_LOADED_SETTINGS: RuntimeSettings | None = None
