from __future__ import annotations

import json
import os
from pathlib import Path

from broker_identity_env import read_account_login

SERVER = "XMGlobal-MT5 6"
COMPANY = "XM Global Limited"


def main() -> None:
    account_login = read_account_login()
    import MetaTrader5 as mt5

    password = os.environ.get("XM_MT5_READ_ONLY_PASSWORD", "")
    terminal = os.environ.get("XM_MT5_TERMINAL_PATH", "")
    if not password or not terminal:
        raise SystemExit("Password and dedicated terminal path are required.")
    if not mt5.initialize(
        terminal,
        login=account_login,
        password=password,
        server=SERVER,
        timeout=60_000,
        portable=True,
    ):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        account = mt5.account_info()
        terminal_info = mt5.terminal_info()
        demo_mode = int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
        if (
            account is None
            or terminal_info is None
            or int(account.login) != account_login
            or str(account.server) != SERVER
            or str(account.company) != COMPANY
            or int(account.trade_mode) != demo_mode
        ):
            raise SystemExit("The connected account failed the fixed Super1 XM demo identity gate.")
        symbols = list(mt5.symbols_get() or ())

        def choose(exact: str, tokens: tuple[str, ...]) -> str:
            exact_matches = [str(item.name) for item in symbols if str(item.name).lower() == exact.lower()]
            if len(exact_matches) == 1:
                return exact_matches[0]
            candidates = []
            for item in symbols:
                text = f"{item.name} {getattr(item, 'description', '')}".upper()
                if any(token in text for token in tokens):
                    candidates.append(str(item.name))
            candidates = sorted(set(candidates))
            if len(candidates) != 1:
                raise SystemExit(f"Symbol discovery is ambiguous for {exact}: {candidates[:20]}")
            return candidates[0]

        payload = {
            "state": "FOUND",
            "login": int(account.login),
            "server": str(account.server),
            "company": str(account.company),
            "demo_verified": True,
            "trade_allowed": bool(getattr(account, "trade_allowed", False)),
            "trade_expert": bool(getattr(account, "trade_expert", False)),
            "terminal_trade_allowed": bool(getattr(terminal_info, "trade_allowed", False)),
            "symbols": {
                "nq": choose("US100Cash", ("US100", "NASDAQ 100", "NASDAQ100")),
                "spx": choose("US500Cash", ("US500", "S&P 500", "SP500")),
            },
        }
        print(json.dumps(payload, sort_keys=True))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
