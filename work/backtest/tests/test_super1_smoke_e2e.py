from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.run_super1_xm_mt5_forward as super1
from backtest.live.approval import ApprovalStore, proposal_hash, wire_request_hash
from backtest.live.strategy_health import LockedOOSBaseline, StrategyHealth


ROOT = Path(__file__).resolve().parents[1]


class FakeSmokeMt5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_ORDER_LIMIT = 2
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_FILLING_RETURN = 2
    ORDER_TIME_SPECIFIED = 2
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 6
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009
    ORDER_STATE_CANCELED = 2
    ORDER_STATE_EXPIRED = 6
    ORDER_STATE_REJECTED = 5

    def __init__(self, server: str) -> None:
        self.server = server
        self.pending: list[SimpleNamespace] = []
        self.history: list[SimpleNamespace] = []
        self.entry_send_count = 0
        self.cancel_send_count = 0
        self.tick_bid = 100.0
        self.readback_uncertain = False
        self.cancel_reject = False

    def initialize(self, *args, **kwargs) -> bool:
        return True

    def login(self, *args, **kwargs) -> bool:
        return True

    def shutdown(self) -> None:
        return None

    def last_error(self) -> tuple[int, str]:
        return (0, "ok")

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            login=[REDACTED, server=self.server, company="XM Global Limited", trade_mode=0,
            trade_allowed=True, trade_expert=True, equity=10_000.0, margin_free=5_000.0, leverage=2.0,
        )

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(connected=True, trade_allowed=True, tradeapi_disabled=False)

    def symbol_select(self, symbol: str, selected: bool) -> bool:
        return selected and symbol == "US100Cash"

    def symbol_info(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=symbol, description="US100", visible=True, digits=2, point=0.01,
            trade_tick_size=0.01, trade_contract_size=1.0, volume_min=0.1,
            volume_max=100.0, volume_step=0.1, currency_base="USD",
            currency_profit="USD", currency_margin="USD", trade_calc_mode=0,
            trade_mode=4, order_mode=2, trade_stops_level=0, trade_freeze_level=0,
        )

    def symbol_info_tick(self, symbol: str) -> SimpleNamespace:
        assert symbol == "US100Cash"
        now = super1.pd.Timestamp(super1.core.utc_now()).timestamp()
        return SimpleNamespace(bid=self.tick_bid, ask=self.tick_bid + 1.0, time_msc=int(now * 1000))

    def orders_get(self, ticket: int | None = None) -> tuple[SimpleNamespace, ...]:
        if ticket is None:
            return tuple(self.pending)
        if self.readback_uncertain:
            return ()
        return tuple(item for item in self.pending if int(item.ticket) == int(ticket))

    def positions_get(self) -> tuple[SimpleNamespace, ...]:
        return ()

    def history_deals_get(self, *args, **kwargs) -> tuple[object, ...]:
        return ()

    def history_orders_get(self, *args, **kwargs) -> tuple[SimpleNamespace, ...]:
        ticket = kwargs.get("ticket")
        if ticket is None and args and isinstance(args[0], int):
            ticket = args[0]
        return tuple(item for item in self.history if ticket is None or int(item.ticket) == int(ticket))

    def order_calc_profit(self, action, symbol, volume, entry, stop) -> float:
        return -900.0 * float(volume)

    def order_calc_margin(self, action, symbol, volume, entry) -> float:
        return 100.0

    def order_check(self, request: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(retcode=0, comment="ok")

    def order_send(self, request: dict[str, object]) -> SimpleNamespace:
        if int(request["action"]) == self.TRADE_ACTION_REMOVE:
            self.cancel_send_count += 1
            ticket = int(request["order"])
            if self.cancel_reject:
                return SimpleNamespace(retcode=10006, order=ticket, deal=0, comment="rejected")
            current = next(item for item in self.pending if int(item.ticket) == ticket)
            self.pending.remove(current)
            self.history.append(SimpleNamespace(
                ticket=ticket, state=self.ORDER_STATE_CANCELED, volume_initial=current.volume_initial,
                volume_current=current.volume_initial,
            ))
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=ticket, deal=0)
        self.entry_send_count += 1
        ticket = 700 + self.entry_send_count
        self.pending.append(SimpleNamespace(
            ticket=ticket, symbol=request["symbol"], type=request["type"],
            volume_initial=request["volume"], price_open=request["price"],
            sl=request["sl"], tp=request["tp"], time_expiration=request["expiration"],
            magic=request["magic"], comment=request["comment"],
        ))
        return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=ticket, deal=0)


def smoke_config() -> dict[str, object]:
    config = json.loads((ROOT / "live_forward/super1_xm_mt5_demo_config.json").read_text(encoding="utf-8"))
    registry_path = ROOT / "tests/fixtures/super1_smoke_instrument_registry.json"
    config.update({"campaign_id": "smoke-campaign", "release_id": "smoke-release"})
    config["base_risk_percent"] = 1.0
    config["risk_limits"] = {
        "daily_loss_cap_r": -1.0,
        "max_total_stop_risk_percent": 2.2,
        "max_margin_fraction": 0.25,
        "max_leverage": 5.0,
        "max_pair_exposure_percent": 100.0,
        "max_concentration_percent": 100.0,
    }
    config["strategy_health_baseline"] = {
        "candidate_hash": str(config["candidate_artifact_sha256"]),
        "closed_trades": 100,
        "valid_sessions": 100,
        "years": [2022, 2023, 2024],
        "rolling_net_r_p05": 0.0,
        "drawdown_p95": 1.0,
        "drawdown_p99": 2.0,
        "seed": 7,
        "locked": True,
    }
    config["legs"]["nq"].update({
        "instrument_id": "TEST_US100",
        "instrument_registry_path": registry_path.relative_to(ROOT).as_posix(),
        "instrument_registry_sha256": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
    })
    return config


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[object, FakeSmokeMt5, dict[str, object]]:
    config = smoke_config()
    fake = FakeSmokeMt5(str(config["expected_server"]))
    client = super1.Super1XmMt5DemoOrderClient(config, {"XM_MT5_SERVER": str(config["expected_server"])}, mt5_module=fake)
    client.connected = True
    client.demo_verified = True
    client.account = fake.account_info()
    client.terminal = fake.terminal_info()
    client._assert_super1_lease = lambda output_root, *, for_order=True: {"state": "ACTIVE"}
    monkeypatch.setattr(super1, "assert_account_binding", lambda mt5, cfg: None)
    client._audit_anchor_callback = lambda event_hash, event_id: None
    (tmp_path / "strategy_health.json").write_text(json.dumps({
        "state": "ACTIVE", "closed_fills": 0, "valid_sessions": 0,
        "rolling_net_r": 0.0, "drawdown_r": 0.0,
        "broker_deal_ids": [], "session_ids": [],
    }), encoding="utf-8")
    candidate_hash = str(config["candidate_artifact_sha256"])
    health = StrategyHealth(
        "ACTIVE",
        baseline=LockedOOSBaseline(candidate_hash, 100, 100, (2022, 2023, 2024), 0.0, 1.0, 2.0, 7),
        expected_candidate_hash=candidate_hash,
    )
    health.persist_canonical(tmp_path / "orders" / "idempotency.sqlite3")
    return client, fake, config


def approve_smoke(tmp_path: Path, config: dict[str, object], proposal_id: str):
    store = ApprovalStore(tmp_path / "orders" / "idempotency.sqlite3")
    lease = {
        "state": "ACTIVE", "lease_id": "lease-1", "invocation_nonce": "nonce-1",
        "runner_sid": "S-1-5-18", "authorized_operator_sid": "S-1-5-19",
        "campaign_id": config["campaign_id"], "account": str(config["account_login"]),
        "release_id": config["release_id"],
    }
    approval = store.approve(
        proposal_id, lease=lease, operator_sid=lease["authorized_operator_sid"],
        release_id=config["release_id"], candidate_hash=config["candidate_artifact_sha256"],
    )
    store.close()
    return approval


def test_super1_smoke_requires_smoke_approval_and_sends_entry_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, config = make_client(tmp_path, monkeypatch)
    lock = {"runtime_config_hash": "runtime-hash"}
    first = client.smoke_order(tmp_path, lock)
    assert first["state"] == "STAGED_NO_SEND"
    assert fake.entry_send_count == 0

    store = ApprovalStore(tmp_path / "orders" / "idempotency.sqlite3")
    proposal = store.get_proposal(first["proposal_id"])
    assert proposal is not None and proposal["approval_type"] == "SMOKE"
    lease = {
        "state": "ACTIVE", "lease_id": "lease-1", "invocation_nonce": "nonce-1",
        "runner_sid": "S-1-5-18", "authorized_operator_sid": "S-1-5-19",
        "campaign_id": config["campaign_id"], "account": str(config["account_login"]),
        "release_id": config["release_id"],
    }
    approval = store.approve(
        first["proposal_id"], lease=lease, operator_sid=lease["authorized_operator_sid"],
        release_id=config["release_id"], candidate_hash=config["candidate_artifact_sha256"],
    )
    store.close()

    result = client.smoke_order(tmp_path, {**lock, "approval_id": approval.approval_id})
    assert result["state"] == "PASS"
    assert result["entry_order_send_count"] == 1
    assert fake.entry_send_count == 1
    assert fake.cancel_send_count == 1
    assert result["open_orders_after"] == 0
    assert result["open_positions_after"] == 0


def test_super1_smoke_replay_and_request_hash_mismatch_never_resend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, config = make_client(tmp_path, monkeypatch)
    first = client.smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash"})
    assert fake.entry_send_count == 0
    store = ApprovalStore(tmp_path / "orders" / "idempotency.sqlite3")
    proposal = store.get_proposal(first["proposal_id"])
    assert proposal is not None
    lease = {
        "state": "ACTIVE", "lease_id": "lease-1", "invocation_nonce": "nonce-1",
        "runner_sid": "S-1-5-18", "authorized_operator_sid": "S-1-5-19",
        "campaign_id": config["campaign_id"], "account": str(config["account_login"]),
        "release_id": config["release_id"],
    }
    approval = store.approve(first["proposal_id"], lease=lease, operator_sid=lease["authorized_operator_sid"], release_id=config["release_id"], candidate_hash=config["candidate_artifact_sha256"])
    store.close()
    client.smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash", "approval_id": approval.approval_id})
    replay = client.smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash", "approval_id": approval.approval_id})
    assert replay["state"] == "REPLAY_NO_SEND"
    assert fake.entry_send_count == 1


def test_super1_smoke_request_hash_mismatch_latches_halt_without_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, config = make_client(tmp_path, monkeypatch)
    first = client.smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash"})
    assert first["state"] == "STAGED_NO_SEND"
    fake.tick_bid = 102.0
    with pytest.raises(Exception, match="request hash mismatch"):
        client.smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash"})
    assert fake.entry_send_count == 0
    assert (tmp_path / "fatal_latch.json").is_file()


def test_super1_smoke_readback_uncertainty_latches_halt_after_single_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, config = make_client(tmp_path, monkeypatch)
    first = client.smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash"})
    approval = approve_smoke(tmp_path, config, first["proposal_id"])
    fake.readback_uncertain = True
    with pytest.raises(Exception, match="HALT|unknown"):
        client.smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash", "approval_id": approval.approval_id})
    assert fake.entry_send_count == 1
    assert (tmp_path / "fatal_latch.json").is_file()


def test_super1_smoke_cancel_failure_latches_halt_and_never_reports_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, config = make_client(tmp_path, monkeypatch)
    first = client.smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash"})
    approval = approve_smoke(tmp_path, config, first["proposal_id"])
    fake.cancel_reject = True
    with pytest.raises(Exception):
        client.smoke_order(tmp_path, {"runtime_config_hash": "runtime-hash", "approval_id": approval.approval_id})
    assert fake.entry_send_count == 1
    assert fake.cancel_send_count == 1
    assert (tmp_path / "fatal_latch.json").is_file()
