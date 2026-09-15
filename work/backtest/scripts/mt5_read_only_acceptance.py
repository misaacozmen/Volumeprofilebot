"""Owner-controlled, read-only MT5 acceptance adapter.

The adapter deliberately receives no credentials and never exposes broker identity
fields.  The owner supplies a proxy whose initialize() is credentialless.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
from typing import Any, Mapping


FORBIDDEN_OPERATIONS = frozenset({
    "login", "order_send", "order_check", "symbol_select", "market_book_add", "market_book_release",
    "copy_rates", "copy_ticks", "symbols_get",
})
ALLOWED_READ_OPERATIONS = frozenset({"account_info", "positions_get", "orders_get", "history_deals_get", "symbol_info"})
WRITE_API_NAMES = frozenset({"initialize", "shutdown", "login", "order_send", "order_check", "symbol_select", "market_book_add", "market_book_release", "copy_ticks_from", "copy_ticks_range", "copy_rates_from", "copy_rates_from_pos", "copy_rates_range", "positions_total", "orders_total", "history_orders_total", "history_deals_total"})
PINNED_PROBE_SHA256 = "3c97b264a537a0efcd323f6889d7355ce54c07570bf9e783da922ce936ae717d"


class ReadOnlyAcceptanceError(RuntimeError):
    pass


class ReadOnlyMt5Proxy:
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
    return ReadOnlyMt5Proxy(module)


def _rows(value: Any, operation: str) -> tuple[Any, ...]:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        raise ReadOnlyAcceptanceError(f"MT5 read returned malformed state: {operation}")
    try:
        return tuple(value)
    except TypeError as exc:
        raise ReadOnlyAcceptanceError(f"MT5 read returned malformed state: {operation}") from exc


def _account_identity_hash(account: Any) -> str:
    values = {"login": str(getattr(account, "login", "")), "server": str(getattr(account, "server", "")), "company": str(getattr(account, "company", ""))}
    if any(not value.strip() for value in values.values()):
        raise ReadOnlyAcceptanceError("MT5 account identity is incomplete")
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def run_read_only_acceptance(module: Any, *, symbol: str = "", now: datetime | None = None) -> dict[str, Any]:
    mt5 = install_read_only_traps(module)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    account = mt5.account_info()
    identity_hash = _account_identity_hash(account)
    positions = _rows(mt5.positions_get(), "positions_get")
    orders = _rows(mt5.orders_get(), "orders_get")
    history = _rows(mt5.history_deals_get(current - timedelta(days=1), current), "history_deals_get")
    if symbol.strip() and mt5.symbol_info(symbol.strip()) is None:
        raise ReadOnlyAcceptanceError("MT5 symbol metadata is unavailable")
    expected = ["account_info", "positions_get", "orders_get", "history_deals_get"] + (["symbol_info"] if symbol.strip() else [])
    if mt5.calls != expected:
        raise ReadOnlyAcceptanceError("MT5 read sequence is not canonical")
    return {"status": "TEST_ONLY_READ_ONLY", "checked_at_utc": current.astimezone(timezone.utc).isoformat(), "account_identity_sha256": identity_hash, "read_operations": list(mt5.calls), "open_positions": len(positions), "pending_orders": len(orders), "history_deals": len(history), "order_send": 0, "write_operations": 0}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def run_adapter(adapter_path: str | Path, *, owner_hmac_key_path: str | Path, binding_id: str, run_id: str, source_commit: str, source_tree_oid: str, nonce: str) -> dict[str, Any]:
    raise ValueError("external MT5 adapters are disabled; use the pinned read-only probe")


def run_pinned_read_only_acceptance(*, owner_hmac_key_path: str | Path, binding_id: str, run_id: str, source_commit: str, source_tree_oid: str, nonce: str, symbol: str = "", now: datetime | None = None) -> dict[str, Any]:
    probe_path = Path(__file__).with_name("mt5_read_only_probe.py")
    if not probe_path.is_file() or sha256(probe_path.read_bytes()).hexdigest() != PINNED_PROBE_SHA256:
        raise ValueError("pinned MT5 read-only probe hash differs")
    key = Path(owner_hmac_key_path).resolve().read_bytes()
    if not key:
        raise ValueError("owner HMAC key is empty")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    operations: list[str] = []
    shutdown_called = False
    identity_hmac: str | None = None
    status = "BLOCKED_EXTERNAL_ACCEPTANCE"
    error_code = "UNKNOWN"
    try:
        import MetaTrader5 as mt5
        from scripts.mt5_read_only_probe import execute
        account, operations = execute(mt5, now=current.astimezone(timezone.utc), symbol=symbol)
        shutdown_called = operations[-1:] == ["shutdown"]
        identity_material = _canonical({"login": str(getattr(account, "login", "")), "server": str(getattr(account, "server", "")), "company": str(getattr(account, "company", ""))})
        identity_hmac = hmac.new(key, identity_material, "sha256").hexdigest()
        status = "PASS_EXTERNAL"
    except Exception as exc:
        error_code = type(exc).__name__
        shutdown_called = operations[-1:] == ["shutdown"]
    payload = {
        "schema_version": 1,
        "status": status,
        "run_id": str(run_id),
        "source_commit": str(source_commit),
        "source_tree_oid": str(source_tree_oid),
        "nonce": str(nonce),
        "binding_id_sha256": sha256(str(binding_id).encode("utf-8")).hexdigest(),
        "identity_hmac_sha256": identity_hmac,
        "account_trade_mode": "DEMO" if status == "PASS_EXTERNAL" else None,
        "order_send": 0,
        "write_operations": 0,
        "forbidden_calls": 0,
        "operations": operations,
        "shutdown_called": shutdown_called,
    }
    if status != "PASS_EXTERNAL":
        payload["error_code"] = error_code
    return payload


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pinned-read-only", action="store_true")
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--owner-hmac-key", type=Path)
    parser.add_argument("--binding-id")
    parser.add_argument("--run-id")
    parser.add_argument("--source-commit")
    parser.add_argument("--source-tree-oid")
    parser.add_argument("--nonce")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--symbol", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.adapter is not None:
        parser.error("external MT5 adapters are disabled; use --pinned-read-only")
    if args.pinned_read_only:
        required = (args.owner_hmac_key, args.binding_id, args.run_id, args.source_commit, args.source_tree_oid, args.nonce, args.report)
        if any(value is None for value in required):
            parser.error("pinned read-only mode requires all owner parameters")
        payload = run_pinned_read_only_acceptance(owner_hmac_key_path=args.owner_hmac_key, binding_id=args.binding_id, run_id=args.run_id, source_commit=args.source_commit, source_tree_oid=args.source_tree_oid, nonce=args.nonce, symbol=args.symbol)
        target = args.report
    else:
        parser.error("legacy read-only acceptance is not an owner gate; use --pinned-read-only")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_canonical(payload) + b"\n")
    print(json.dumps({"status": payload["status"], "order_send": payload.get("order_send", 0), "write_operations": payload.get("write_operations", 0)}, sort_keys=True))
    return 0 if payload["status"] == "PASS_EXTERNAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
