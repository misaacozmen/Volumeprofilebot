from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys


def _write_result(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def load_mt5_environment(required_names: tuple[str, ...]) -> dict[str, str]:
    names = tuple(dict.fromkeys((*required_names, "XM_MT5_TERMINAL_PATH")))
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Required MT5 environment is incomplete: {', '.join(missing)}")
    return values


def evaluate_readiness(
    config: dict[str, object],
    mt5_module: object,
    account: object,
    terminal: object,
    orders: tuple[object, ...] | list[object],
    positions: tuple[object, ...] | list[object],
) -> dict[str, object]:
    identity_checks = {
        "login": int(getattr(account, "login")) == int(config["account_login"]),
        "server": str(getattr(account, "server")) == str(config["expected_server"]),
        "company": str(getattr(account, "company")) == str(config["expected_company"]),
        "demo": int(getattr(account, "trade_mode"))
        == int(getattr(mt5_module, "ACCOUNT_TRADE_MODE_DEMO")),
    }
    permission_checks = {
        "account_trade_allowed": bool(getattr(account, "trade_allowed", False)),
        "account_trade_expert": bool(getattr(account, "trade_expert", False)),
        "terminal_connected": bool(getattr(terminal, "connected", False)),
        "terminal_trade_allowed": bool(getattr(terminal, "trade_allowed", False)),
        "terminal_tradeapi_enabled": not bool(getattr(terminal, "tradeapi_disabled", True)),
    }
    flat = len(orders) == 0 and len(positions) == 0
    terminal_data_path = Path(str(getattr(terminal, "data_path"))).resolve()
    if bool(config.get("portable", False)):
        # Super1 always seals terminal_path in its signed config.  The
        # environment fallback keeps the generic diagnostic compatible with
        # older forward fixtures; Super1's runtime guard rejects that shape.
        terminal_path = str(
            config.get("terminal_path") or os.environ.get("XM_MT5_TERMINAL_PATH", "")
        ).strip()
        if not terminal_path:
            raise RuntimeError("Portable MT5 readiness requires a pinned terminal path.")
        expected_data_root = Path(terminal_path).resolve().parent
        identity_checks["windows_profile"] = terminal_data_path == expected_data_root
    else:
        expected_data_root = (Path(os.environ["APPDATA"]) / "MetaQuotes" / "Terminal").resolve()
        identity_checks["windows_profile"] = (
            os.path.commonpath(
                (os.path.normcase(expected_data_root), os.path.normcase(terminal_data_path))
            )
            == os.path.normcase(expected_data_root)
        )
    identity_ready = all(identity_checks.values())
    transport_ready = all(permission_checks.values())
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "account_login": int(getattr(account, "login")),
        "server": str(getattr(account, "server")),
        "company": str(getattr(account, "company")),
        "terminal_data_path": str(terminal_data_path),
        "open_orders": len(orders),
        "open_positions": len(positions),
        "flat": flat,
        "identity_checks": identity_checks,
        "identity_ready": identity_ready,
        "permission_checks": permission_checks,
        "transport_ready": transport_ready,
        "ready": identity_ready and transport_ready and flat,
    }


def verify_symbol_transport_contract(client: object, mt5_module: object, config: dict[str, object]) -> dict[str, object]:
    """Prove symbol geometry/calculation/order_check without calling order_send."""
    proof: dict[str, object] = {}
    for leg_key in ("nq", "spx"):
        symbol = str(config["legs"][leg_key]["epic"])
        if not mt5_module.symbol_select(symbol, True):
            raise RuntimeError(f"{symbol}: symbol_select failed in no-send proof.")
        info = mt5_module.symbol_info(symbol)
        tick = mt5_module.symbol_info_tick(symbol)
        if info is None or tick is None:
            raise RuntimeError(f"{symbol}: symbol metadata/tick unavailable in no-send proof.")
        values = {
            "bid": float(getattr(tick, "bid")),
            "ask": float(getattr(tick, "ask")),
            "point": float(getattr(info, "point")),
            "tick_size": float(getattr(info, "trade_tick_size")),
            "volume_min": float(getattr(info, "volume_min")),
            "volume_max": float(getattr(info, "volume_max")),
            "volume_step": float(getattr(info, "volume_step")),
            "stops_level": float(getattr(info, "trade_stops_level", 0)),
            "freeze_level": float(getattr(info, "trade_freeze_level", 0)),
        }
        if any(not math.isfinite(value) for value in values.values()):
            raise RuntimeError(f"{symbol}: non-finite symbol/tick value in no-send proof.")
        if values["bid"] >= values["ask"] or values["point"] <= 0 or values["tick_size"] <= 0:
            raise RuntimeError(f"{symbol}: invalid bid/ask or tick geometry in no-send proof.")
        if values["volume_min"] <= 0 or values["volume_max"] < values["volume_min"] or values["volume_step"] <= 0:
            raise RuntimeError(f"{symbol}: invalid volume contract in no-send proof.")
        if values["stops_level"] < 0 or values["freeze_level"] < 0:
            raise RuntimeError(f"{symbol}: invalid stop/freeze contract in no-send proof.")
        if not bool(getattr(info, "visible", False)):
            raise RuntimeError(f"{symbol}: symbol is not visible in no-send proof.")
        order_mode = getattr(info, "order_mode", None)
        limit_flag = getattr(mt5_module, "SYMBOL_ORDER_LIMIT", None)
        if order_mode is None or limit_flag is None or not (int(order_mode) & int(limit_flag)):
            raise RuntimeError(f"{symbol}: limit-order mode is not advertised.")
        filling_mode = getattr(info, "filling_mode", None)
        expiration_mode = getattr(info, "expiration_mode", None)
        if filling_mode is None or expiration_mode is None or int(filling_mode) < 0 or int(expiration_mode) < 0:
            raise RuntimeError(f"{symbol}: filling/time modes are not advertised.")
        spread = values["ask"] - values["bid"]
        distance = max(
            values["stops_level"] * values["point"] + 2 * values["tick_size"],
            4 * spread,
            10 * values["tick_size"],
        )
        request = client._pending_request(
            symbol,
            "long",
            values["ask"] - distance,
            values["ask"] - 2 * distance,
            values["ask"],
            "SUPER1:CHECK",
        )
        request["expiration"] = int(datetime.now(timezone.utc).timestamp()) + 900
        profit = mt5_module.order_calc_profit(
            int(request["type"]), symbol, float(request["volume"]), float(request["price"]), float(request["tp"])
        )
        margin = mt5_module.order_calc_margin(
            int(request["type"]), symbol, float(request["volume"]), float(request["price"])
        )
        account = mt5_module.account_info()
        free_margin = float(getattr(account, "margin_free")) if account is not None else float("nan")
        if profit is None or margin is None or not math.isfinite(float(profit)) or not math.isfinite(float(margin)):
            raise RuntimeError(f"{symbol}: order_calc_profit/order_calc_margin was indeterminate.")
        if not math.isfinite(free_margin) or float(margin) > free_margin * 0.25:
            raise RuntimeError(f"{symbol}: margin requirement exceeds the 25% free-margin gate.")
        check = mt5_module.order_check(request)
        if check is None or int(getattr(check, "retcode", -1)) != 0:
            raise RuntimeError(f"{symbol}: order_check failed in no-send proof.")
        proof[leg_key] = {
            "symbol": symbol,
            "bid": values["bid"],
            "ask": values["ask"],
            "volume_min": values["volume_min"],
            "volume_max": values["volume_max"],
            "volume_step": values["volume_step"],
            "order_calc_profit": float(profit),
            "order_calc_margin": float(margin),
            "order_check_called": True,
            "order_send_called": False,
        }
    return proof
def combined_readiness(
    base: dict[str, object],
    permission: dict[str, object],
    preflight: dict[str, object],
    *,
    is_weekend: bool = False,
    before_preflight_window: bool = False,
    scheduled_closed: bool = False,
) -> bool:
    state = preflight.get("state")
    transport_ready = bool(
        (
            state == "PASS"
            and len(preflight.get("checks", [])) >= 4
        )
        or (
            state == "NON_TRADING_DAY"
            and preflight.get("reason") == "WEEKEND"
            and is_weekend
            and scheduled_closed
        )
        or (
            state == "WAITING_PREFLIGHT_WINDOW"
            and preflight.get("opens_at") == "09:30"
            and before_preflight_window
        )
        or (
            state == "WAITING_ORDER_CHECK"
            and preflight.get("retcode") == 10018
            and scheduled_closed
        )
    )
    return bool(
        base.get("ready")
        and permission.get("state") == "READY"
        and preflight.get("order_send_called") is False
        and transport_ready
    )


def evaluate_read_only_readiness(
    base: dict[str, object], terminal: object
) -> dict[str, object]:
    account_permissions = dict(base.get("permission_checks", {}))
    controls = {
        "account_trade_allowed": bool(account_permissions.get("account_trade_allowed")),
        "account_trade_expert": bool(account_permissions.get("account_trade_expert")),
        "terminal_connected": bool(getattr(terminal, "connected", False)),
        "terminal_trade_disabled": not bool(getattr(terminal, "trade_allowed", True)),
        "python_trade_api_enabled": not bool(
            getattr(terminal, "tradeapi_disabled", True)
        ),
    }
    base["permission_checks"] = controls
    base["transport_ready"] = all(controls.values())
    base["ready"] = bool(
        base.get("identity_ready") and base.get("flat") and base["transport_ready"]
    )
    return base


def main() -> int:
    import pandas as pd

    parser = argparse.ArgumentParser(
        description="Verify XM demo identity, permissions, flat exposure, and order_check transport."
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile", choices=("forward", "super1"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-nonce", default="")
    parser.add_argument("--read-only-proof", action="store_true")
    parser.add_argument(
        "--binding-proof",
        action="store_true",
        help="Verify the exact broker binding and permissions without order_check or order_send.",
    )
    parser.add_argument("--credential-stdin", action="store_true")
    args = parser.parse_args()
    app_root = (args.root / "app").resolve()
    sys.path.insert(0, str(app_root / "scripts"))
    client = None
    try:
        if args.profile == "forward":
            import run_xm_mt5_forward as harness
        else:
            import run_super1_xm_mt5_forward as harness

        harness.configure_core()
        core = harness.core
        requested_config = (app_root / args.config).resolve()
        if Path(harness.RUNTIME_CONFIG).resolve() != requested_config:
            raise RuntimeError("Readiness profile/config mismatch.")
        config = core.runtime_config()
        if args.profile == "super1":
            if not args.credential_stdin:
                raise RuntimeError("Super1 flat diagnostic requires credential stdin.")
            secrets = core.credentials()
            if secrets is None:
                raise RuntimeError("Super1 credential stdin was empty.")
        else:
            secrets = load_mt5_environment(tuple(core.REQUIRED_ENV))
        client = core.CapitalDemoClient(config, secrets)
        login_result = client.login()
        account = client.mt5.account_info()
        terminal_info = client.mt5.terminal_info()
        orders = client.mt5.orders_get()
        positions = client.mt5.positions_get()
        if account is None or terminal_info is None or orders is None or positions is None:
            raise RuntimeError(f"MT5 exposure query failed: {client.mt5.last_error()}")
        result = evaluate_readiness(config, client.mt5, account, terminal_info, orders, positions)
        if args.binding_proof:
            result["markets"] = verify_symbol_transport_contract(client, client.mt5, config)
            result.update(
                {
                    "evidence_nonce": args.evidence_nonce,
                    "profile": args.profile,
                    "terminal_path": str(Path(str(config["terminal_path"])).resolve()),
                    "login": login_result,
                    "permission": {
                        "state": "BINDING_PROOF",
                        "checks": result["permission_checks"],
                    },
                    "order_transport_preflight": {
                        "state": "DISABLED_FOR_BINDING_PROOF",
                        "order_send_called": False,
                    },
                    "transport_preflight_deferred": True,
                    "evidence_root": "",
                }
            )
            _write_result(args.output, result)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["ready"] else 2
        if args.read_only_proof:
            result = evaluate_read_only_readiness(result, terminal_info)
            result.update(
                {
                    "evidence_nonce": args.evidence_nonce,
                    "profile": args.profile,
                    "terminal_path": str(Path(str(config["terminal_path"])).resolve()),
                    "login": login_result,
                    "permission": {
                        "state": "READ_ONLY_PROOF",
                        "checks": result["permission_checks"],
                    },
                    "markets": {},
                    "order_transport_preflight": {
                        "state": "DISABLED_FOR_READ_ONLY_PROOF",
                        "order_send_called": False,
                    },
                    "transport_preflight_deferred": True,
                    "evidence_root": "",
                }
            )
            _write_result(args.output, result)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["ready"] else 2
        permission = client.order_permission_status()
        markets = core.verify_markets(client, config)
        now = pd.Timestamp.now(tz="UTC")
        evidence_root = (
            args.output.parent
            / "readiness_evidence"
            / f"{args.profile}-{now.strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        preflight = dict(client.preflight_order_transport(evidence_root, now))
        preflight.setdefault("order_send_called", False)
        local = now.tz_convert(str(config["market_schedule_ny"]["timezone"]))
        result.update(
            {
                "evidence_nonce": args.evidence_nonce,
                "profile": args.profile,
                "terminal_path": str(Path(str(config["terminal_path"])).resolve()),
                "login": login_result,
                "permission": permission,
                "markets": markets,
                "order_transport_preflight": preflight,
                "transport_preflight_deferred": preflight.get("state") != "PASS",
                "evidence_root": str(evidence_root),
                "ready": combined_readiness(
                    result,
                    permission,
                    preflight,
                    is_weekend=local.weekday() >= 5,
                    before_preflight_window=(
                        local.weekday() < 5 and local.strftime("%H:%M") < "09:30"
                    ),
                    scheduled_closed=bool(core.scheduled_closed_minute(now, config)),
                ),
            }
        )
        _write_result(args.output, result)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ready"] else 2
    except Exception as exc:
        _write_result(
            args.output,
            {
                "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                "evidence_nonce": args.evidence_nonce,
                "error": str(exc),
                "ready": False,
            },
        )
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
