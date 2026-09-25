from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backtest.live.approval import ApprovalError, ApprovalStore, wire_request_hash


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
LEASE = {"state": "ACTIVE", "lease_id": "l1", "invocation_nonce": "n1", "runner_sid": "S-1-5-18", "authorized_operator_sid": "S-1-5-19", "campaign_id": "c1", "account": "a1", "release_id": "r1"}


def proposal() -> dict[str, object]:
    request = {"action": "pending", "symbol": "US100Cash"}
    return {
        "proposal_id": "p1", "entry": 1,
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(), "candidate_hash": "c1",
        "wire_request": request, "wire_request_hash": wire_request_hash(request),
    }


def test_approval_is_lease_bound_and_expires_at_proposal_or_ttl(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals.json", now=lambda: NOW)
    staged = {**proposal(), "approval_type": "market"}
    store.stage(staged, campaign_id="c1", account_key="a1", release_id="r1", candidate_hash="c1", approval_type="market")
    record = store.approve("p1", lease=LEASE, operator_sid="S-1-5-19", release_id="r1", candidate_hash="c1")
    assert record.lease_id == "l1"
    assert record.expires_at_utc == (NOW + timedelta(seconds=30)).isoformat()
    assert store.is_valid(record.approval_id, staged, now=NOW)
    changed = {**staged, "entry": 2}
    assert not store.is_valid(record.approval_id, changed, now=NOW)


def test_approval_rejects_missing_lease_and_exposes_only_local_commands(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals.sqlite3", now=lambda: NOW)
    with pytest.raises(ApprovalError):
        store.approve("p1", lease={}, operator_sid="S-1", release_id="r1", candidate_hash="c1")


def test_approval_rejects_staged_proposal_without_exact_wire_request_hash(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approvals.sqlite3", now=lambda: NOW)
    invalid = dict(proposal())
    invalid.pop("wire_request_hash")
    store.stage(invalid, campaign_id="c1", account_key="a1", release_id="r1", candidate_hash="c1")
    with pytest.raises(ApprovalError, match="wire_request_hash"):
        store.approve("p1", lease=LEASE, operator_sid="S-1-5-19", release_id="r1", candidate_hash="c1")


def test_runner_sid_cannot_approve_its_own_proposal(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "orders" / "idempotency.sqlite3", now=lambda: NOW)
    staged = {**proposal(), "approval_type": "limit"}
    store.stage(staged, campaign_id="c1", account_key="a1", release_id="r1", candidate_hash="c1")
    with pytest.raises(ApprovalError, match="interactive operator"):
        store.approve("p1", lease=LEASE, operator_sid=LEASE["runner_sid"], release_id="r1", candidate_hash="c1")
    store.close()


def test_proposal_is_single_lifecycle_and_consumption_is_replay_safe(tmp_path) -> None:
    path = tmp_path / "orders" / "idempotency.sqlite3"
    store = ApprovalStore(path, now=lambda: NOW)
    staged = {**proposal(), "approval_type": "limit"}
    store.stage(staged, campaign_id="c1", account_key="a1", release_id="r1", candidate_hash="c1")
    approval = store.approve("p1", lease=LEASE, operator_sid="S-1-5-19", release_id="r1", candidate_hash="c1")
    with pytest.raises(ApprovalError, match="terminal or approved|already has"):
        store.stage(staged, campaign_id="c1", account_key="a1", release_id="r1", candidate_hash="c1")
    with pytest.raises(ApprovalError, match="persisted STAGED|already has"):
        store.approve("p1", lease=LEASE, operator_sid="S-1-5-19", release_id="r1", candidate_hash="c1")

    armed: list[tuple[str, str]] = []
    consumed = store.consume_and_arm(
        approval.approval_id,
        proposal=staged,
        campaign_id="c1",
        account_key="a1",
        release_id="r1",
        candidate_hash="c1",
        lease_nonce="n1",
        operator_sid="S-1-5-19",
        order_id="p1",
        request={"action": "pending", "symbol": "US100Cash"},
        arm=lambda _connection, request_hash, approval_type: armed.append((request_hash, approval_type)),
        now=NOW,
    )
    assert consumed.state == "CONSUMED"
    assert armed and armed[0][1] == "limit"
    with pytest.raises(ApprovalError, match="missing, consumed"):
        store.consume_and_arm(
            approval.approval_id,
            proposal=staged,
            campaign_id="c1",
            account_key="a1",
            release_id="r1",
            candidate_hash="c1",
            lease_nonce="n1",
            operator_sid="S-1-5-19",
            order_id="p1",
            request={"action": "pending", "symbol": "US100Cash"},
            arm=lambda *_: None,
            now=NOW,
        )
    row = store.connection.execute("SELECT state FROM staged_proposals WHERE proposal_id='p1'").fetchone()
    assert row == ("CONSUMED",)
    states = [row[0] for row in store.connection.execute("SELECT state FROM approval_state_outbox ORDER BY rowid")]
    assert states == ["STAGED", "APPROVED", "CONSUMED"]
    store.close()


def test_expired_consume_is_persisted_with_audit_in_same_store(tmp_path) -> None:
    store = ApprovalStore(tmp_path / "approval.sqlite3", now=lambda: NOW)
    staged = {**proposal(), "approval_type": "market"}
    store.stage(staged, campaign_id="c1", account_key="a1", release_id="r1", candidate_hash="c1", approval_type="market")
    approval = store.approve("p1", lease=LEASE, operator_sid="S-1-5-19", release_id="r1", candidate_hash="c1")
    with pytest.raises(ApprovalError, match="expired"):
        store.consume_and_arm(
            approval.approval_id, proposal=staged, campaign_id="c1", account_key="a1",
            release_id="r1", candidate_hash="c1", lease_nonce="n1", operator_sid="S-1-5-19",
            order_id="p1", request=staged["wire_request"], arm=lambda *_: None,
            now=NOW + timedelta(seconds=31),
        )
    assert store.show(approval.approval_id).state == "EXPIRED"
    assert store.connection.execute("SELECT state FROM approval_state_outbox ORDER BY rowid DESC LIMIT 1").fetchone() == ("EXPIRED",)
