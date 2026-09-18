"""Observed, owner-authorized MT5 DEMO read-only acceptance probe.

This file is intentionally separate from the pinned historical probe.  It uses
the real MetaTrader5 module, blocks every non-allowlisted API before reaching
the module, and records call inputs/results without recording account identity.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
from typing import Any


ALLOWED_OPERATIONS = (
    "initialize",
    "account_info",
    "positions_get",
    "orders_get",
    "history_deals_get",
    "shutdown",
)
FORBIDDEN_OPERATIONS = frozenset(
    {
        "login",
        "order_send",
        "order_check",
        "symbol_select",
        "market_book_add",
        "market_book_release",
        "copy_ticks_from",
        "copy_ticks_range",
        "copy_rates_from",
        "copy_rates_from_pos",
        "copy_rates_range",
    }
)
_PROVENANCE_PATTERNS = {
    "run_id": re.compile(r"[0-9a-f]{64}\Z"),
    "nonce": re.compile(r"[0-9a-f]{64}\Z"),
    "source_commit": re.compile(r"[0-9a-f]{40}\Z"),
    "source_tree_oid": re.compile(r"[0-9a-f]{40}\Z"),
}


class ReadOnlyViolation(RuntimeError):
    """Raised when code attempts to access a non-allowlisted MT5 operation."""


def _argument_metadata(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    def describe(value: Any) -> dict[str, Any]:
        if isinstance(value, datetime):
            return {"type": "datetime", "value_utc": value.astimezone(timezone.utc).isoformat()}
        return {"type": type(value).__name__}

    return {
        "args": [describe(value) for value in args],
        "kwargs": sorted(kwargs),
    }


class ObservedMt5:
    """A strict proxy whose forbidden accesses never reach the real module."""

    def __init__(self, module: Any) -> None:
        self._module = module
        self.events: list[dict[str, Any]] = []
        self.forbidden_attempts: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name not in ALLOWED_OPERATIONS:
            self.forbidden_attempts.append(name)
            self.events.append(
                {
                    "operation": name,
                    "phase": "access",
                    "success": False,
                    "reached_real_module": False,
                    "error_type": "ReadOnlyViolation",
                }
            )
            raise ReadOnlyViolation(f"MT5 operation is not allowlisted: {name}")

        operation = getattr(self._module, name, None)
        if not callable(operation):
            self.events.append(
                {
                    "operation": name,
                    "phase": "access",
                    "success": False,
                    "reached_real_module": True,
                    "error_type": "MissingOperation",
                }
            )
            raise ReadOnlyViolation(f"MT5 operation is unavailable: {name}")

        def invoke(*args: Any, **kwargs: Any) -> Any:
            event: dict[str, Any] = {
                "operation": name,
                "phase": "call",
                "inputs": _argument_metadata(args, kwargs),
                "reached_real_module": True,
            }
            try:
                result = operation(*args, **kwargs)
            except Exception as exc:
                event.update(
                    {
                        "success": False,
                        "result_type": None,
                        "error_type": type(exc).__name__,
                    }
                )
                self.events.append(event)
                raise
            event.update(
                {
                    "success": True,
                    "result_type": type(result).__name__,
                    "result_is_none": result is None,
                }
            )
            self.events.append(event)
            return result

        return invoke


def _rows(value: Any, operation: str) -> tuple[Any, ...]:
    if value is None or isinstance(value, (str, bytes, dict)):
        raise ReadOnlyViolation(f"{operation} returned no valid iterable result")
    try:
        return tuple(value)
    except TypeError as exc:
        raise ReadOnlyViolation(f"{operation} returned no valid iterable result") from exc


def _account_identity_hmac(account: Any, key: bytes) -> tuple[str, bool]:
    if account is None:
        raise ReadOnlyViolation("MT5 account identity is missing")
    login = getattr(account, "login", None)
    server = getattr(account, "server", None)
    company = getattr(account, "company", None)
    if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
        raise ReadOnlyViolation("MT5 account login is invalid")
    if not isinstance(server, str) or not server.strip():
        raise ReadOnlyViolation("MT5 account server is invalid")
    if not isinstance(company, str) or not company.strip():
        raise ReadOnlyViolation("MT5 account company is invalid")
    values = {
        "login": str(login),
        "server": server,
        "company": company,
    }
    material = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest(), True


def _validate_provenance(*, run_id: str, nonce: str, source_commit: str, source_tree_oid: str) -> None:
    values = {
        "run_id": run_id,
        "nonce": nonce,
        "source_commit": source_commit,
        "source_tree_oid": source_tree_oid,
    }
    for name, value in values.items():
        pattern = _PROVENANCE_PATTERNS[name]
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ValueError(f"invalid provenance field: {name}")


def run(*, key_path: Path, run_id: str, nonce: str, source_commit: str, source_tree_oid: str) -> dict[str, Any]:
    _validate_provenance(
        run_id=run_id,
        nonce=nonce,
        source_commit=source_commit,
        source_tree_oid=source_tree_oid,
    )
    key = key_path.resolve().read_bytes()
    if len(key) != 32:
        raise ValueError("owner HMAC key must be 32 bytes")

    import MetaTrader5 as real_mt5  # noqa: PLC0415 - the real broker module is required.

    observed = ObservedMt5(real_mt5)
    started = datetime.now(timezone.utc)
    initialize_attempted = False
    shutdown_attempted = False
    shutdown_success = False
    status = "BLOCKED_EXTERNAL_ACCEPTANCE"
    error: dict[str, str] | None = None
    account_identity_hmac: str | None = None
    account_trade_mode = None
    counts: dict[str, int | None] = {
        "open_positions": None,
        "pending_orders": None,
        "history_deals": None,
    }

    try:
        initialize_attempted = True
        initialized = bool(observed.initialize())
        if not initialized:
            raise ReadOnlyViolation("MT5 initialize returned false")

        account = observed.account_info()
        demo_constant = getattr(real_mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
        account_mode = getattr(account, "trade_mode", None) if account is not None else None
        if isinstance(demo_constant, bool) or not isinstance(demo_constant, int):
            raise ReadOnlyViolation("MT5 DEMO constant is unavailable")
        if isinstance(account_mode, bool) or not isinstance(account_mode, int):
            raise ReadOnlyViolation("MT5 account trade mode is invalid")
        account_trade_mode = "DEMO" if account_mode == demo_constant else "NON_DEMO"
        if account_trade_mode != "DEMO":
            raise ReadOnlyViolation("MT5 account is not DEMO")
        account_identity_hmac, _ = _account_identity_hmac(account, key)

        positions = _rows(observed.positions_get(), "positions_get")
        orders = _rows(observed.orders_get(), "orders_get")
        now = datetime.now(timezone.utc)
        history = _rows(observed.history_deals_get(now - timedelta(days=1), now), "history_deals_get")
        counts = {
            "open_positions": len(positions),
            "pending_orders": len(orders),
            "history_deals": len(history),
        }
    except Exception as exc:
        error = {"type": type(exc).__name__}
    finally:
        if initialize_attempted:
            shutdown_attempted = True
            try:
                shutdown_success = bool(observed.shutdown())
                if not shutdown_success and error is None:
                    error = {"type": "ShutdownFailure"}
            except Exception as exc:
                shutdown_success = False
                if error is None:
                    error = {"type": type(exc).__name__}

    operations = [event["operation"] for event in observed.events if event.get("phase") == "call"]
    expected = ["initialize", "account_info", "positions_get", "orders_get", "history_deals_get", "shutdown"]
    write_attempts = [name for name in observed.forbidden_attempts if name in FORBIDDEN_OPERATIONS]
    order_send_attempts = [name for name in observed.forbidden_attempts if name == "order_send"]
    if error is None and operations == expected and shutdown_success and not observed.forbidden_attempts:
        status = "PASS_EXTERNAL"

    finished = datetime.now(timezone.utc)
    return {
        "schema_version": 2,
        "acceptance_scope": "ITEM22_DEMO_READ_ONLY_OWNER_DECLARATION_V1",
        "status": status,
        "run_id": run_id,
        "nonce": nonce,
        "source_commit": source_commit,
        "source_tree_oid": source_tree_oid,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "account_trade_mode": account_trade_mode,
        "account_identity_hmac_sha256": account_identity_hmac,
        "operations": operations,
        "allowed_operations": list(ALLOWED_OPERATIONS),
        "forbidden_attempts": list(observed.forbidden_attempts),
        "forbidden_calls": len(observed.forbidden_attempts),
        "write_operations": len(write_attempts),
        "order_send": len(order_send_attempts),
        "counts": counts,
        "events": observed.events,
        "initialize_attempted": initialize_attempted,
        "shutdown_attempted": shutdown_attempted,
        "shutdown_success": shutdown_success,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-hmac-key", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree-oid", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    payload = run(
        key_path=args.owner_hmac_key,
        run_id=args.run_id,
        nonce=args.nonce,
        source_commit=args.source_commit,
        source_tree_oid=args.source_tree_oid,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    print(json.dumps({"status": payload["status"], "operations": payload["operations"], "forbidden_calls": payload["forbidden_calls"], "shutdown_success": payload["shutdown_success"]}, sort_keys=True))
    return 0 if payload["status"] == "PASS_EXTERNAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
