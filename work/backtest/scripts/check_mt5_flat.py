from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
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
        expected_data_root = Path(os.environ["XM_MT5_TERMINAL_PATH"]).resolve().parent
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
        if args.read_only_proof:
            result = evaluate_read_only_readiness(result, terminal_info)
            result.update(
                {
                    "evidence_nonce": args.evidence_nonce,
                    "profile": args.profile,
                    "terminal_path": str(Path(secrets["XM_MT5_TERMINAL_PATH"]).resolve()),
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
                "terminal_path": str(Path(secrets["XM_MT5_TERMINAL_PATH"]).resolve()),
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
