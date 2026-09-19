from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from broker_identity_env import BrokerIdentityError
from scripts import discover_super1_xm_account, discover_xm_mt5_server


ROOT = Path(__file__).resolve().parents[1]


def _load_smoke(monkeypatch):
    fake_module = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_module)
    spec = importlib.util.spec_from_file_location(
        "mt5_market_roundtrip_smoke_test", ROOT / "scripts" / "mt5_market_roundtrip_smoke.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, fake_module


def test_discovery_requires_login_before_importing_mt5(monkeypatch) -> None:
    monkeypatch.delenv("XM_MT5_ACCOUNT_LOGIN", raising=False)
    with pytest.raises(BrokerIdentityError):
        discover_super1_xm_account.main()


def test_server_discovery_transfers_valid_login_from_environment(monkeypatch, capsys) -> None:
    calls = []

    class FakeMt5:
        def initialize(self, *args, **kwargs):
            calls.append((args, kwargs))
            return True

        def account_info(self):
            return SimpleNamespace(login=42424242, server="XM-DEMO")

        def shutdown(self):
            pass

    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMt5())
    monkeypatch.setenv("XM_MT5_ACCOUNT_LOGIN", " 42424242 ")
    monkeypatch.setenv("XM_MT5_READ_ONLY_PASSWORD", "fixture-password")
    monkeypatch.setenv("XM_MT5_SERVER", "XM-DEMO")
    discover_xm_mt5_server.main()
    assert calls[0][1]["login"] == 42424242
    assert '"login": 42424242' in capsys.readouterr().out


@pytest.mark.parametrize(
    "account",
    [
        SimpleNamespace(login=42424242, server="wrong", company="XM Global Limited", trade_mode=0),
        SimpleNamespace(login=42424242, server="XMGlobal-MT5 6", company="wrong", trade_mode=0),
        SimpleNamespace(login=42424242, server="XMGlobal-MT5 6", company="XM Global Limited", trade_mode=1),
    ],
)
def test_super1_discovery_rejects_wrong_server_company_or_real_account(monkeypatch, account) -> None:
    class FakeMt5:
        ACCOUNT_TRADE_MODE_DEMO = 0

        def initialize(self, *args, **kwargs):
            self.calls = (args, kwargs)
            return True

        def account_info(self):
            return account

        def terminal_info(self):
            return SimpleNamespace()

        def shutdown(self):
            pass

    fake = FakeMt5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    monkeypatch.setenv("XM_MT5_ACCOUNT_LOGIN", "42424242")
    monkeypatch.setenv("XM_MT5_READ_ONLY_PASSWORD", "fixture-password")
    monkeypatch.setenv("XM_MT5_TERMINAL_PATH", "fixture-terminal")
    with pytest.raises(SystemExit):
        discover_super1_xm_account.main()
    assert fake.calls[1]["login"] == 42424242


def test_market_smoke_rejects_identity_before_order_send(monkeypatch, tmp_path) -> None:
    module, fake = _load_smoke(monkeypatch)
    fake.ACCOUNT_TRADE_MODE_DEMO = 0
    fake.TRADE_ACTION_DEAL = 1
    fake.order_send_calls = 0

    def initialize(path):
        return True

    fake.initialize = initialize
    fake.account_info = lambda: SimpleNamespace(
        login=42424242,
        server="wrong-server",
        company="XM Global Limited",
        trade_mode=0,
    )
    fake.terminal_info = lambda: SimpleNamespace()
    fake.positions_get = lambda **kwargs: ()
    fake.orders_get = lambda **kwargs: ()
    fake.symbol_info = lambda symbol: None
    fake.order_send = lambda request: (_ for _ in ()).throw(AssertionError("order_send called"))
    fake.shutdown = lambda: None
    monkeypatch.setenv("XM_MT5_ACCOUNT_LOGIN", "42424242")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke",
            "--terminal-path", "fixture-terminal",
            "--evidence", str(tmp_path / "evidence.json"),
            "--confirm-demo",
        ],
    )
    with pytest.raises(RuntimeError, match="identity gate"):
        module.main()
