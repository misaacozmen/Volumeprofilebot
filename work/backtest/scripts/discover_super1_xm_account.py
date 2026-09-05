from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover only the sealed Super1 XM demo account.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--credential-stdin", action="store_true", required=True)
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if (
        config.get("environment") != "XM_MT5_DEMO_ORDER"
        or config.get("account_mode") != "DEMO_ORDER"
        or config.get("manual_required") is not False
    ):
        raise SystemExit("Super1 config is not the sealed XM demo order profile.")
    password = sys.stdin.readline().rstrip("\r\n")
    terminal = str(config.get("terminal_path") or "").strip()
    if not password or not terminal:
        raise SystemExit("Credential stdin and the signed terminal path are required.")

    import MetaTrader5 as mt5

    if not mt5.initialize(
        terminal,
        login=int(config["account_login"]),
        password=password,
        server=str(config["expected_server"]),
        timeout=60_000,
        portable=bool(config.get("portable", True)),
    ):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        account = mt5.account_info()
        terminal_info = mt5.terminal_info()
        demo_mode = int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
        if (
            account is None
            or terminal_info is None
            or int(account.login) != int(config["account_login"])
            or str(account.server) != str(config["expected_server"])
            or str(account.company) != str(config["expected_company"])
            or int(account.trade_mode) != demo_mode
        ):
            raise SystemExit("The connected account failed the sealed Super1 XM demo identity gate.")
        symbols = list(mt5.symbols_get() or ())

        def choose(key: str) -> str:
            expected = str(config["legs"][key]["epic"])
            matches = [str(item.name) for item in symbols if str(item.name).lower() == expected.lower()]
            if len(matches) != 1:
                raise SystemExit(f"Configured Super1 symbol is unavailable or ambiguous: {expected}")
            return matches[0]

        payload = {
            "state": "FOUND",
            "login": int(account.login),
            "server": str(account.server),
            "company": str(account.company),
            "demo_verified": True,
            "trade_allowed": bool(getattr(account, "trade_allowed", False)),
            "trade_expert": bool(getattr(account, "trade_expert", False)),
            "terminal_trade_allowed": bool(getattr(terminal_info, "trade_allowed", False)),
            "symbols": {"nq": choose("nq"), "spx": choose("spx")},
        }
        print(json.dumps(payload, sort_keys=True))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
