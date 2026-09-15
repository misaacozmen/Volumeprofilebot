"""Pinned source for the owner-controlled, credentialless MT5 read probe."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable


PROBE_SCHEMA_VERSION = 1
READ_NAMES = ("account_info", "positions_get", "orders_get", "history_deals_get")


def execute(module: Any, *, now: datetime, symbol: str = "") -> tuple[Any, list[str]]:
    """Run the fixed read sequence through a lifecycle/read-only proxy."""
    calls: list[str] = []
    initialized = False

    def lifecycle(name: str) -> Any:
        operation = getattr(module, name, None)
        if not callable(operation):
            raise RuntimeError(f"MT5 lifecycle operation unavailable: {name}")
        calls.append(name)
        return operation()

    def read(name: str, *args: Any) -> Any:
        if name not in READ_NAMES and name != "symbol_info":
            raise RuntimeError(f"MT5 operation is not allowlisted: {name}")
        operation: Callable[..., Any] | None = getattr(module, name, None)
        if not callable(operation):
            raise RuntimeError(f"MT5 read operation unavailable: {name}")
        calls.append(name)
        return operation(*args)

    try:
        initialized = bool(lifecycle("initialize"))
        if not initialized:
            raise RuntimeError("credentialless MT5 initialize failed")
        account = read("account_info")
        positions = read("positions_get")
        orders = read("orders_get")
        history = read("history_deals_get", now - timedelta(days=1), now)
        if symbol.strip() and read("symbol_info", symbol.strip()) is None:
            raise RuntimeError("MT5 symbol metadata is unavailable")
        demo = getattr(module, "ACCOUNT_TRADE_MODE_DEMO", None)
        if demo is None or getattr(account, "trade_mode", None) != demo:
            raise RuntimeError("MT5 account is not provably DEMO")
        if positions is None or orders is None or history is None:
            raise RuntimeError("MT5 exposure query failed")
        return account, calls
    finally:
        if initialized or "initialize" in calls:
            lifecycle("shutdown")
