from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_mt5_flat", ROOT / "scripts" / "check_mt5_flat.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _config() -> dict[str, object]:
    return {
        "account_login": 123,
        "expected_server": "XM-Demo",
        "expected_company": "Fixture Broker Ltd",
    }


def _account(**overrides: object) -> SimpleNamespace:
    values = {
        "login": 123,
        "server": "XM-Demo",
        "company": "Fixture Broker Ltd",
        "trade_mode": 0,
        "trade_allowed": True,
        "trade_expert": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _terminal(**overrides: object) -> SimpleNamespace:
    values = {
        "connected": True,
        "trade_allowed": True,
        "tradeapi_disabled": False,
        "data_path": str(Path(os.environ["APPDATA"]) / "MetaQuotes" / "Terminal" / "fixture"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_flat_check_requires_the_pinned_terminal_environment(monkeypatch) -> None:
    monkeypatch.setenv("XM_MT5_SERVER", "XM-Demo")
    monkeypatch.delenv("XM_MT5_TERMINAL_PATH", raising=False)
    try:
        MODULE.load_mt5_environment(("XM_MT5_SERVER",))
    except RuntimeError as exc:
        assert "XM_MT5_TERMINAL_PATH" in str(exc)
    else:
        raise AssertionError("Missing pinned terminal path was accepted.")

    monkeypatch.setenv("XM_MT5_TERMINAL_PATH", r"C:\Program Files\XM MT5\terminal64.exe")
    loaded = MODULE.load_mt5_environment(("XM_MT5_SERVER",))
    assert loaded == {
        "XM_MT5_SERVER": "XM-Demo",
        "XM_MT5_TERMINAL_PATH": r"C:\Program Files\XM MT5\terminal64.exe",
    }


def test_readiness_requires_fixed_demo_identity_permissions_and_flat_account() -> None:
    result = MODULE.evaluate_readiness(
        _config(), SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0), _account(), _terminal(), (), ()
    )

    assert result["identity_ready"] is True
    assert result["transport_ready"] is True
    assert result["flat"] is True
    assert result["ready"] is True


def test_readiness_fails_closed_on_wrong_server_disabled_transport_or_exposure() -> None:
    result = MODULE.evaluate_readiness(
        _config(),
        SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0),
        _account(server="wrong"),
        _terminal(trade_allowed=False),
        (object(),),
        (),
    )

    assert result["identity_ready"] is False
    assert result["transport_ready"] is False
    assert result["flat"] is False
    assert result["ready"] is False


def test_readiness_accepts_only_the_pinned_portable_terminal_data_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    terminal = tmp_path / "terminal" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"fixture")
    monkeypatch.setenv("XM_MT5_TERMINAL_PATH", str(terminal))
    config = {**_config(), "portable": True}

    matching = MODULE.evaluate_readiness(
        config,
        SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0),
        _account(),
        _terminal(data_path=str(terminal.parent)),
        (),
        (),
    )
    mismatched = MODULE.evaluate_readiness(
        config,
        SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0),
        _account(),
        _terminal(data_path=str(tmp_path / "other")),
        (),
        (),
    )

    assert matching["identity_checks"]["windows_profile"] is True
    assert matching["ready"] is True
    assert mismatched["identity_checks"]["windows_profile"] is False
    assert mismatched["ready"] is False


def test_combined_readiness_requires_order_check_without_order_send() -> None:
    passing_checks = [{"retcode": 0}] * 4

    assert MODULE.combined_readiness(
        {"ready": True},
        {"state": "READY"},
        {"state": "PASS", "order_send_called": False, "checks": passing_checks},
    )
    assert not MODULE.combined_readiness(
        {"ready": True},
        {"state": "READY"},
        {"state": "PASS", "order_send_called": True, "checks": passing_checks},
    )


def test_combined_readiness_accepts_only_safe_deferred_market_states() -> None:
    base = {"ready": True}
    permission = {"state": "READY"}

    assert MODULE.combined_readiness(
        base,
        permission,
        {"state": "NON_TRADING_DAY", "reason": "WEEKEND", "order_send_called": False},
        is_weekend=True,
        scheduled_closed=True,
    )
    assert MODULE.combined_readiness(
        base,
        permission,
        {
            "state": "WAITING_PREFLIGHT_WINDOW",
            "opens_at": "09:30",
            "order_send_called": False,
        },
        before_preflight_window=True,
    )
    assert MODULE.combined_readiness(
        base,
        permission,
        {"state": "WAITING_ORDER_CHECK", "retcode": 10018, "order_send_called": False},
        scheduled_closed=True,
    )
    assert not MODULE.combined_readiness(
        base,
        permission,
        {"state": "NON_TRADING_DAY", "reason": "WEEKEND", "order_send_called": False},
        is_weekend=True,
        scheduled_closed=False,
    )

    for preflight in (
        {"state": "WAITING_ORDER_CHECK", "retcode": None, "order_send_called": False},
        {"state": "WAITING_ORDER_CHECK", "reason_code": "ENTRY_DISTANCE_WAIT", "order_send_called": False},
        {"state": "WAITING_ORDER_CHECK", "retcode": 10031, "order_send_called": False},
        {"state": "NON_TRADING_DAY", "reason": "UNKNOWN", "order_send_called": False},
        {"state": "WAITING_PREFLIGHT_WINDOW", "opens_at": "10:00", "order_send_called": False},
        {"state": "NON_TRADING_DAY", "reason": "WEEKEND", "order_send_called": True},
        {"state": "NON_TRADING_DAY", "reason": "WEEKEND", "order_send_called": False},
        {"state": "WAITING_PREFLIGHT_WINDOW", "opens_at": "09:30", "order_send_called": False},
        {"state": "WAITING_ORDER_CHECK", "retcode": 10018, "order_send_called": False},
    ):
        assert not MODULE.combined_readiness(base, permission, preflight)


def test_read_only_proof_requires_flat_demo_identity_and_disabled_order_paths() -> None:
    base = MODULE.evaluate_readiness(
        _config(),
        SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0),
        _account(),
        _terminal(trade_allowed=False, tradeapi_disabled=False),
        (),
        (),
    )
    ready = MODULE.evaluate_read_only_readiness(
        base, _terminal(trade_allowed=False, tradeapi_disabled=False)
    )
    assert ready["ready"] is True

    exposed = MODULE.evaluate_readiness(
        _config(),
        SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0),
        _account(),
        _terminal(trade_allowed=False, tradeapi_disabled=False),
        (object(),),
        (),
    )
    assert not MODULE.evaluate_read_only_readiness(
        exposed, _terminal(trade_allowed=False, tradeapi_disabled=False)
    )["ready"]
    assert not MODULE.evaluate_read_only_readiness(
        dict(ready), _terminal(trade_allowed=True, tradeapi_disabled=False)
    )["ready"]
    assert not MODULE.evaluate_read_only_readiness(
        dict(ready), _terminal(trade_allowed=False, tradeapi_disabled=True)
    )["ready"]
    account_disabled = MODULE.evaluate_readiness(
        _config(),
        SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0),
        _account(trade_expert=False),
        _terminal(trade_allowed=False, tradeapi_disabled=False),
        (),
        (),
    )
    assert not MODULE.evaluate_read_only_readiness(
        account_disabled, _terminal(trade_allowed=False, tradeapi_disabled=False)
    )["ready"]
