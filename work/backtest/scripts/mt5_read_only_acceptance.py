"""Owner-controlled, read-only MT5 acceptance adapter.

The adapter deliberately receives no credentials and never exposes broker identity
fields.  The owner supplies a proxy whose initialize() is credentialless.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from hashlib import sha256
import hmac
import importlib.util
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
    return {"status": "PASS_READ_ONLY", "checked_at_utc": current.astimezone(timezone.utc).isoformat(), "account_identity_sha256": identity_hash, "read_operations": list(mt5.calls), "open_positions": len(positions), "pending_orders": len(orders), "history_deals": len(history), "order_send": 0, "write_operations": 0}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _load_adapter(path: Path) -> Any:
    source = path.read_text(encoding="utf-8")
    lowered = source.lower()
    if any(re.search(rf"\b{re.escape(name)}\b", lowered) for name in FORBIDDEN_OPERATIONS):
        raise ValueError("MT5 adapter contains a forbidden broker operation")
    spec = importlib.util.spec_from_file_location("owner_mt5_adapter", path)
    if spec is None or spec.loader is None:
        raise ValueError("MT5 adapter cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_adapter(adapter_path: str | Path, *, owner_hmac_key_path: str | Path, binding_id: str, run_id: str, source_commit: str, source_tree_oid: str, nonce: str) -> dict[str, Any]:
    adapter = _load_adapter(Path(adapter_path).resolve())
    key = Path(owner_hmac_key_path).resolve().read_bytes()
    if not key:
        raise ValueError("owner HMAC key is empty")
    client = adapter.build_client()
    if client is None:
        raise ValueError("MT5 adapter returned no owner-controlled proxy")
    initialized = False
    shutdown_called = False
    operations: list[str] = []
    try:
        initialized = bool(client.initialize())
        operations.append("initialize")
        if not initialized:
            raise ValueError("credentialless MT5 initialize failed")
        account = client.account_info()
        operations.append("account_info")
        positions = client.positions_get()
        operations.append("positions_get")
        orders = client.orders_get()
        operations.append("orders_get")
        deals = client.history_deals_get()
        operations.append("history_deals_get")
        module = getattr(client, "module", client)
        demo_constant = getattr(module, "ACCOUNT_TRADE_MODE_DEMO", None)
        trade_mode = getattr(account, "trade_mode", None)
        if demo_constant is None or trade_mode != demo_constant:
            raise ValueError("MT5 account is not provably DEMO")
        identity_material = _canonical({
            "login": str(getattr(account, "login", "")),
            "server": str(getattr(account, "server", "")),
            "company": str(getattr(account, "company", "")),
        })
        identity_hmac = hmac.new(key, identity_material, "sha256").hexdigest()
        if positions is None or orders is None or deals is None:
            raise ValueError("read-only MT5 exposure query failed")
        status = "PASS_EXTERNAL"
    except Exception as exc:
        status = "BLOCKED_EXTERNAL_ACCEPTANCE"
        identity_hmac = None
        error_code = type(exc).__name__
    finally:
        try:
            client.shutdown()
            shutdown_called = True
            operations.append("shutdown")
        except Exception:
            shutdown_called = False
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
        required = (args.owner_hmac_key, args.binding_id, args.run_id, args.source_commit, args.source_tree_oid, args.nonce, args.report)
        if any(value is None for value in required):
            parser.error("owner adapter mode requires all owner parameters")
        payload = run_adapter(args.adapter, owner_hmac_key_path=args.owner_hmac_key, binding_id=args.binding_id, run_id=args.run_id, source_commit=args.source_commit, source_tree_oid=args.source_tree_oid, nonce=args.nonce)
        target = args.report
    else:
        if args.output is None:
            parser.error("legacy mode requires --output")
        try:
            import MetaTrader5 as mt5
            payload = run_read_only_acceptance(mt5, symbol=args.symbol)
            code = 0
        except Exception as exc:
            payload = {"status": "BLOCKED_EXTERNAL_ACCEPTANCE", "reason_code": type(exc).__name__}
            code = 2
        target = args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_canonical(payload) + b"\n")
    print(json.dumps({"status": payload["status"], "order_send": payload.get("order_send", 0), "write_operations": payload.get("write_operations", 0)}, sort_keys=True))
    return 0 if payload["status"] in {"PASS_EXTERNAL", "PASS_READ_ONLY"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
