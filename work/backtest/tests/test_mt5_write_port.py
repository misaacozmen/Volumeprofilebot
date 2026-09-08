from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from backtest.live.approval import ApprovalStore, wire_request_hash
from backtest.live.audit_ledger import AuditLedger
from backtest.live.execution import ExecutionBoundaryError, Mt5WritePort


def test_mt5_write_port_claims_durable_order_once_and_blocks_restart_replay(tmp_path: Path) -> None:
    db = tmp_path / "orders" / "idempotency.sqlite3"
    now = datetime.now(timezone.utc)
    request = {"action": 5, "symbol": "US100Cash", "volume": 0.1, "position": 0}
    request_hash = wire_request_hash(request)
    proposal = {
        "proposal_id": "order-1", "candidate_hash": "c" * 64, "instrument_id": "i1",
        "direction": "long", "entry_price": 100.0, "stop_price": 99.0, "target_price": 101.0,
        "decision_time": (now - timedelta(seconds=1)).isoformat(), "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "evidence_hash": "e" * 64, "wire_request": request, "wire_request_hash": request_hash,
    }
    store = ApprovalStore(db, now=lambda: now)
    store.stage(proposal, campaign_id="campaign", account_key="account", release_id="release", candidate_hash="c" * 64)
    lease = {
        "state": "ACTIVE", "lease_id": "lease", "invocation_nonce": "nonce", "runner_sid": "S-1-5-18",
        "authorized_operator_sid": "S-1-5-19", "campaign_id": "campaign", "account": "account", "release_id": "release",
    }
    approval = store.approve("order-1", lease=lease, operator_sid="S-1-5-19", release_id="release", candidate_hash="c" * 64, now=now)
    store.connection.execute("UPDATE approvals SET state='CONSUMED' WHERE approval_id=?", (approval.approval_id,))
    store.close()
    ledger = AuditLedger(db, campaign_id="campaign", account_key="account")
    ledger.append("SEND_ARMED", entity_type="order", entity_id="order-1", payload={"request_hash": request_hash})
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO order_state_records(order_id,state,request_hash,updated_at_utc) VALUES(?,?,?,?)", ("order-1", "SEND_ARMED", request_hash, now.isoformat()))
    port = Mt5WritePort(None, db)
    port.arm_in_transaction(connection, order_id="order-1", request=request, request_hash=request_hash, approval_id=approval.approval_id)
    connection.commit()
    connection.close()

    class FakeMt5:
        def __init__(self) -> None:
            self.calls = 0

        def order_send(self, value: dict[str, object]) -> object:
            self.calls += 1
            assert value == request
            return SimpleNamespace(retcode=10009, order=123)

    fake = FakeMt5()
    port = Mt5WritePort(fake, db)
    assert port.send("order-1").retcode == 10009
    assert fake.calls == 1
    with pytest.raises(ExecutionBoundaryError, match="SEND_ARMED"):
        port.send("order-1")
    ledger.close()


def test_raw_operation_api_cannot_reach_broker(tmp_path: Path) -> None:
    class FakeMt5:
        def __init__(self) -> None:
            self.calls = 0

        def order_send(self, _request: dict[str, object]) -> object:
            self.calls += 1
            return SimpleNamespace(retcode=10009)

    fake = FakeMt5()
    port = Mt5WritePort(fake, tmp_path / "orders.sqlite3")
    assert not hasattr(port, "send_operation")
    assert fake.calls == 0
