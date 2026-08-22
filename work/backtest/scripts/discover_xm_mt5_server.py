from __future__ import annotations

import getpass
import json
import os
import sys


ACCOUNT_LOGIN = 318413815
DEFAULT_SERVERS = ["XMGlobal-MT5", *[f"XMGlobal-MT5 {number}" for number in range(1, 51)]]


def main() -> None:
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise SystemExit("MetaTrader5 Python package is not installed.") from exc
    password = os.environ.get("XM_MT5_READ_ONLY_PASSWORD") or getpass.getpass(
        "XM salt-okunur MetaTrader parolasi: "
    )
    terminal_path = os.environ.get("XM_MT5_TERMINAL_PATH", "").strip()
    server_override = os.environ.get("XM_MT5_SERVER", "").strip()
    servers = [server_override] if server_override else DEFAULT_SERVERS
    for server in servers:
        kwargs = {
            "login": ACCOUNT_LOGIN,
            "password": password,
            "server": server,
            "timeout": 20_000,
        }
        ok = mt5.initialize(terminal_path, **kwargs) if terminal_path else mt5.initialize(**kwargs)
        account = mt5.account_info() if ok else None
        if account is not None and int(account.login) == ACCOUNT_LOGIN and str(account.server) == server:
            print(json.dumps({"state": "FOUND", "login": ACCOUNT_LOGIN, "server": server}))
            mt5.shutdown()
            return
        mt5.shutdown()
    raise SystemExit("XM demo server was not found; check the account number/read-only password.")


if __name__ == "__main__":
    main()
