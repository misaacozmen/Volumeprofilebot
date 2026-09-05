from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any

import MetaTrader5 as mt5


SYMBOL = ""
MAGIC = 0
COMMENT = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def result_fields(result: Any) -> dict[str, object]:
    return {
        "retcode": int(result.retcode),
        "order": int(result.order),
        "deal": int(result.deal),
        "volume": float(result.volume),
        "price": float(result.price),
    }


def minimum_volume(info: Any) -> float:
    step = float(info.volume_step)
    steps = math.ceil((float(info.volume_min) - 1e-12) / step)
    return round(steps * step, 8)


def filling_type(info: Any) -> int:
    flags = int(info.filling_mode)
    if flags & int(getattr(mt5, "SYMBOL_FILLING_IOC", 2)):
        return int(mt5.ORDER_FILLING_IOC)
    if flags & int(getattr(mt5, "SYMBOL_FILLING_FOK", 1)):
        return int(mt5.ORDER_FILLING_FOK)
    if int(info.trade_exemode) != int(getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2)):
        return int(mt5.ORDER_FILLING_RETURN)
    raise RuntimeError("No legal market-order filling mode is advertised by the symbol.")


def own_positions() -> list[Any]:
    return [
        item
        for item in mt5.positions_get(symbol=SYMBOL) or ()
        if int(getattr(item, "magic", -1)) == MAGIC
    ]


def own_orders() -> list[Any]:
    return [
        item
        for item in mt5.orders_get(symbol=SYMBOL) or ()
        if int(getattr(item, "magic", -1)) == MAGIC
    ]


def close_position(position: Any, filling: int) -> Any:
    last_result = None
    for _ in range(3):
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            time.sleep(0.25)
            continue
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(position.ticket),
            "symbol": SYMBOL,
            "volume": float(position.volume),
            "type": mt5.ORDER_TYPE_SELL,
            "price": float(tick.bid),
            "deviation": 50,
            "magic": MAGIC,
            "comment": COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        check = mt5.order_check(request)
        if check is None or int(check.retcode) != 0:
            time.sleep(0.25)
            continue
        last_result = mt5.order_send(request)
        if last_result is not None and int(last_result.retcode) in {
            int(mt5.TRADE_RETCODE_DONE),
            int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
        }:
            for _ in range(10):
                if not own_positions():
                    return last_result
                time.sleep(0.2)
        time.sleep(0.25)
    raise RuntimeError(
        "Demo smoke position could not be closed."
        if last_result is None
        else f"Demo smoke close failed with retcode {int(last_result.retcode)}."
    )


def write_evidence(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Exact-config-gated XM MT5 pending-order round-trip smoke test.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--terminal-path")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--confirm-demo", action="store_true")
    parser.add_argument("--credential-stdin", action="store_true")
    args = parser.parse_args()
    evidence_path = Path(args.evidence)
    if not args.confirm_demo:
        raise RuntimeError("XM demo smoke requires the separate explicit confirmation.")
    if evidence_path.exists():
        raise RuntimeError(f"Evidence path already exists: {evidence_path}")
    config_path = Path(args.config).resolve()
    if not config_path.is_file() or config_path.is_symlink():
        raise RuntimeError("The signed Super1 config is missing or is a reparse point.")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The signed Super1 config is unreadable.") from exc
    if not isinstance(config, dict) or config.get("environment") != "XM_MT5_DEMO_ORDER":
        raise RuntimeError("The smoke config is not an XM demo-order config.")
    global SYMBOL, MAGIC, COMMENT
    try:
        SYMBOL = str(config["legs"]["nq"]["symbol"])
        MAGIC = int(config["magic_number"])
        COMMENT = (str(config["order_comment_prefix"]) + ":SMOKE")[:31]
        login = int(config["account_login"])
        expected_server = str(config["expected_server"])
        expected_company = str(config["expected_company"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("The signed Super1 config has incomplete broker identity fields.") from exc
    terminal_path = str(args.terminal_path or config.get("terminal_path") or "").strip()
    if not terminal_path:
        raise RuntimeError("The signed Super1 config has no terminal path.")
    if not args.credential_stdin:
        raise RuntimeError("XM demo smoke requires a transient password on stdin.")
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise RuntimeError("XM demo smoke received no transient password.")

    evidence: dict[str, object] = {
        "schema_version": 1,
        "started_at": utc_now(),
        "state": "RUNNING",
        "symbol": SYMBOL,
        "magic": MAGIC,
        "config": str(config_path),
        "config_sha256": __import__("hashlib").sha256(config_path.read_bytes()).hexdigest(),
        "creation_send_count": 0,
        "cancel_send_count": 0,
        "duplicate_send": 0,
        "unknown": 0,
    }
    initialized = False
    filling = None
    try:
        initialized = bool(
            mt5.initialize(
                terminal_path,
                login=login,
                password=password,
                server=expected_server,
                timeout=60_000,
                portable=True,
            )
        )
        if not initialized:
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if account is None or terminal is None:
            raise RuntimeError("MT5 account or terminal information is unavailable.")
        identity = {
            "login": int(account.login),
            "server": str(account.server),
            "company": str(account.company),
            "trade_mode": int(account.trade_mode),
        }
        evidence["identity"] = identity
        if identity != {
            "login": login,
            "server": expected_server,
            "company": expected_company,
            "trade_mode": int(mt5.ACCOUNT_TRADE_MODE_DEMO),
        }:
            raise RuntimeError("Exact XM demo identity gate failed.")
        permissions = {
            "account_trade_allowed": bool(account.trade_allowed),
            "account_trade_expert": bool(account.trade_expert),
            "terminal_connected": bool(terminal.connected),
            "terminal_trade_allowed": bool(terminal.trade_allowed),
            "terminal_tradeapi_disabled": bool(terminal.tradeapi_disabled),
        }
        evidence["permissions"] = permissions
        if not (
            permissions["account_trade_allowed"]
            and permissions["account_trade_expert"]
            and permissions["terminal_connected"]
            and permissions["terminal_trade_allowed"]
            and not permissions["terminal_tradeapi_disabled"]
        ):
            raise RuntimeError("Demo order permission gate failed.")
        if own_positions() or own_orders():
            raise RuntimeError("Smoke magic already has a broker object; refusing duplicate.")
        if not mt5.symbol_select(SYMBOL, True):
            raise RuntimeError("US100Cash symbol_select failed.")
        info = mt5.symbol_info(SYMBOL)
        tick = mt5.symbol_info_tick(SYMBOL)
        if info is None or tick is None:
            raise RuntimeError("US100Cash symbol metadata or tick is unavailable.")
        if int(info.trade_mode) != int(mt5.SYMBOL_TRADE_MODE_FULL):
            raise RuntimeError("US100Cash is not in full trade mode.")
        volume = minimum_volume(info)
        filling = filling_type(info)
        digits = int(info.digits)
        distance = max(
            float(info.trade_stops_level) * float(info.point) + 100 * float(info.point),
            float(tick.ask) * 0.01,
        )
        pending_price = round(float(tick.bid) - distance, digits)
        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": SYMBOL,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY_LIMIT,
            "price": pending_price,
            "sl": round(pending_price - distance, digits),
            "tp": round(pending_price + distance, digits),
            "deviation": 50,
            "magic": MAGIC,
            "comment": COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        evidence["request"] = {
            "volume": volume,
            "filling": filling,
            "sl": request["sl"],
            "tp": request["tp"],
        }
        if not pending_price < float(tick.ask):
            raise RuntimeError("Pending smoke price is marketable.")
        check = mt5.order_check(request)
        if check is None or int(check.retcode) != 0:
            raise RuntimeError(
                "Demo pending order_check failed."
                if check is None
                else f"Demo market order_check retcode {int(check.retcode)}."
            )
        opened = mt5.order_send(request)
        evidence["creation_send_count"] = 1
        if opened is None or int(opened.retcode) not in {
            int(mt5.TRADE_RETCODE_DONE),
        }:
            raise RuntimeError(
                "Demo pending order returned no definitive acceptance."
                if opened is None
                else f"Demo pending order retcode {int(opened.retcode)}."
            )
        evidence["opened"] = result_fields(opened)
        ticket = int(getattr(opened, "order", 0) or 0)
        if ticket <= 0:
            raise RuntimeError("Accepted pending order has no broker ticket.")
        pending = None
        for _ in range(10):
            orders = [item for item in own_orders() if int(getattr(item, "ticket", 0)) == ticket]
            if orders:
                pending = orders[0]
                break
            time.sleep(0.2)
        if pending is None:
            positions = own_positions()
            if positions:
                evidence["unknown"] = 1
                raise RuntimeError("Pending smoke order filled before the first readback.")
            evidence["unknown"] = 1
            raise RuntimeError("Accepted pending order was not observable by broker readback.")
        evidence["pending_readback"] = {"ticket": ticket, "symbol": str(pending.symbol)}
        cancelled = mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": ticket,
                "symbol": SYMBOL,
                "magic": MAGIC,
                "comment": COMMENT,
            }
        )
        evidence["cancel_send_count"] = 1
        if cancelled is None or int(cancelled.retcode) != int(mt5.TRADE_RETCODE_DONE):
            evidence["unknown"] = 1
            raise RuntimeError("Pending smoke cancellation has no definitive broker result.")
        evidence["cancelled"] = result_fields(cancelled)
        for _ in range(10):
            if not own_orders() and not own_positions():
                break
            time.sleep(0.2)
        remaining_positions = own_positions()
        remaining_orders = own_orders()
        if remaining_positions or remaining_orders:
            evidence["unknown"] = 1
            raise RuntimeError("Smoke broker objects remain after close.")
        evidence.update(
            {
                "completed_at": utc_now(),
                "state": "PASS",
                "remaining_positions": 0,
                "remaining_orders": 0,
                "open_orders": 0,
                "open_positions": 0,
                "logical_replay_send_count": 0,
            }
        )
        write_evidence(evidence_path, evidence)
        print(json.dumps(evidence, indent=2, sort_keys=True))
    except Exception as exc:
        emergency_errors = []
        if initialized:
            if filling is None:
                info = mt5.symbol_info(SYMBOL)
                if info is not None:
                    try:
                        filling = filling_type(info)
                    except Exception as filling_exc:
                        emergency_errors.append(str(filling_exc))
            if filling is not None:
                for position in own_positions():
                    try:
                        close_position(position, filling)
                    except Exception as close_exc:
                        emergency_errors.append(str(close_exc))
        evidence.update(
            {
                "completed_at": utc_now(),
                "state": "FAIL",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "emergency_errors": emergency_errors,
                "remaining_positions": len(own_positions()) if initialized else None,
                "remaining_orders": len(own_orders()) if initialized else None,
                "open_orders": len(own_orders()) if initialized else None,
                "open_positions": len(own_positions()) if initialized else None,
            }
        )
        if not evidence_path.exists():
            write_evidence(evidence_path, evidence)
        raise
    finally:
        if initialized:
            mt5.shutdown()


if __name__ == "__main__":
    main()
