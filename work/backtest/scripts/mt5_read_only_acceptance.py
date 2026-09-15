"""Owner-run MT5 acceptance probe with a read-only SDK boundary.

The terminal must already be connected by the owner.  This module deliberately
does not initialize, shut down, select symbols, calculate orders, or write to
the broker.  It only reads the four account/exposure/history views and, when
requested, symbol metadata.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


class ReadOnlyAcceptanceError(RuntimeError):
    pass


ALLOWED_READ_OPERATIONS = frozenset(
    {"account_info", "positions_get", "orders_get", "history_deals_get", "symbol_info"}
)

# Keep the denylist explicit at the import boundary.  The proxy also rejects
# every non-allowlisted callable, so an SDK addition cannot silently become a
# write path.
WRITE_API_NAMES = frozenset(
    {
        "initialize", "shutdown", "login", "order_send", "order_check",
        "symbol_select", "market_book_add", "market_book_release",
        "copy_ticks_from", "copy_ticks_range", "copy_rates_from",
        "copy_rates_from_pos", "copy_rates_range", "positions_total",
        "orders_total", "history_orders_total", "history_deals_total",
    }
)


class ReadOnlyMt5Proxy:
    """Expose only approved MT5 reads and trap all known write APIs."""

    def __init__(self, module: Any) -> None:
        self._module = module
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name in WRITE_API_NAMES or name not in ALLOWED_READ_OPERATIONS:
            raise ReadOnlyAcceptanceError(f"MT5 operation is not allowlisted: {name}")
        operation = getattr(self._module, name, None)
        if not callable(operation):
            raise ReadOnlyAcceptanceError(f"MT5 read operation is unavailable: {name}")

        def read(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return operation(*args, **kwargs)

        return read


def install_read_only_traps(module: Any) -> ReadOnlyMt5Proxy:
    """Create the only object accepted by the read-only acceptance routine."""
    return ReadOnlyMt5Proxy(module)


def _rows(value: Any, operation: str) -> tuple[Any, ...]:
    if value is None:
        raise ReadOnlyAcceptanceError(f"MT5 read returned unknown state: {operation}")
    if isinstance(value, (str, bytes, Mapping)):
        raise ReadOnlyAcceptanceError(f"MT5 read returned malformed state: {operation}")
    try:
        return tuple(value)
    except TypeError as exc:
        raise ReadOnlyAcceptanceError(f"MT5 read returned malformed state: {operation}") from exc


def _account_identity_hash(account: Any) -> str:
    values = {
        "login": str(getattr(account, "login", "")),
        "server": str(getattr(account, "server", "")),
        "company": str(getattr(account, "company", "")),
    }
    if any(not value.strip() for value in values.values()):
        raise ReadOnlyAcceptanceError("MT5 account identity is incomplete")
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_read_only_acceptance(
    module: Any,
    *,
    symbol: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read broker state and return redacted, owner-verifiable evidence."""
    mt5 = install_read_only_traps(module)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    account = mt5.account_info()
    identity_hash = _account_identity_hash(account)
    positions = _rows(mt5.positions_get(), "positions_get")
    orders = _rows(mt5.orders_get(), "orders_get")
    history = _rows(
        mt5.history_deals_get(current - timedelta(days=1), current),
        "history_deals_get",
    )
    symbol_checked = bool(symbol.strip())
    if symbol_checked and mt5.symbol_info(symbol.strip()) is None:
        raise ReadOnlyAcceptanceError("MT5 symbol metadata is unavailable")
    expected = ["account_info", "positions_get", "orders_get", "history_deals_get"]
    if symbol_checked:
        expected.append("symbol_info")
    if mt5.calls != expected:
        raise ReadOnlyAcceptanceError("MT5 read sequence is not canonical")
    return {
        "status": "PASS_READ_ONLY",
        "checked_at_utc": current.astimezone(timezone.utc).isoformat(),
        "account_identity_sha256": identity_hash,
        "read_operations": list(mt5.calls),
        "open_positions": len(positions),
        "pending_orders": len(orders),
        "history_deals": len(history),
        "order_send": 0,
        "write_operations": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run owner-side MT5 read-only acceptance.")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        import MetaTrader5 as mt5
    except Exception:
        payload = {"status": "BLOCKED_EXTERNAL_ACCEPTANCE", "reason": "MetaTrader5 package is unavailable"}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return 2
    try:
        payload = run_read_only_acceptance(mt5, symbol=args.symbol)
    except ReadOnlyAcceptanceError as exc:
        payload = {"status": "BLOCKED_EXTERNAL_ACCEPTANCE", "reason": str(exc)}
        code = 2
    else:
        code = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
