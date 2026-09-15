from __future__ import annotations

import getpass
import json
import os
import sys

from backtest.live.settings import environment_value

DEFAULT_SERVERS = ["XMGlobal-MT5", *[f"XMGlobal-MT5 {number}" for number in range(1, 51)]]


def main() -> None:
    raw_login = environment_value("XM_MT5_ACCOUNT_LOGIN").strip()
    if not raw_login.isascii() or not raw_login.isdigit() or int(raw_login) <= 0:
        raise SystemExit("XM_MT5_ACCOUNT_LOGIN must be supplied through the approved environment schema.")
    account_login = int(raw_login)
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise SystemExit("MetaTrader5 Python package is not installed.") from exc
    password = environment_value("XM_MT5_READ_ONLY_PASSWORD") or getpass.getpass(
        "XM salt-okunur MetaTrader parolasi: "
    )
    terminal_path = environment_value("XM_MT5_TERMINAL_PATH").strip()
    server_override = environment_value("XM_MT5_SERVER").strip()
    servers = [server_override] if server_override else DEFAULT_SERVERS
    for server in servers:
        kwargs = {
            "login": account_login,
            "password": password,
            "server": server,
            "timeout": 20_000,
        }
        ok = mt5.initialize(terminal_path, **kwargs) if terminal_path else mt5.initialize(**kwargs)
        account = mt5.account_info() if ok else None
        if account is not None and int(account.login) == account_login and str(account.server) == server:
            print(json.dumps({"state": "FOUND", "server": server}))
            mt5.shutdown()
            return
        mt5.shutdown()
    raise SystemExit("XM demo server was not found; check the account number/read-only password.")


if __name__ == "__main__":
    main()
