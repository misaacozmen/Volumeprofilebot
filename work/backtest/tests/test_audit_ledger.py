from __future__ import annotations

import json
import sqlite3

import pytest

from backtest.live.audit_ledger import AuditLedger, AuditLedgerError, GENESIS_HASH, event_hash


def test_audit_chain_hash_and_transactional_sequence(tmp_path) -> None:
    ledger = AuditLedger(tmp_path / "orders.sqlite3", legacy_snapshot_hash="legacy-hash")
    first = ledger.append("SIGNAL", entity_type="proposal", entity_id="p1", payload={"b": 2, "a": 1})
    second = ledger.append("RISK_DECISION", entity_type="proposal", entity_id="p1", payload={"approved": False})
    assert first.sequence == 1 and first.prev_event_hash == GENESIS_HASH
    assert first.event_hash == event_hash(first.prev_event_hash, {
        "schema_version": 2, "sequence": 1, "event_id": first.event_id,
        "campaign_id": "", "account_key": "", "occurred_at_utc": first.occurred_at_utc,
        "event_type": "SIGNAL", "entity_type": "proposal", "entity_id": "p1",
        "payload": {"a": 1, "b": 2, "genesis": True, "legacy_snapshot_hash": "legacy-hash"},
        "previous_hash": GENESIS_HASH,
    })
    assert second.sequence == 2
    assert ledger.verify()
    ledger.close()


def test_mutation_and_sequence_gap_fail_closed(tmp_path) -> None:
    path = tmp_path / "orders.sqlite3"
    ledger = AuditLedger(path)
    ledger.append("SIGNAL", payload={"ok": True})
    ledger.append("STAGE", payload={"ok": True})
    ledger.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE audit_chain_events SET payload_canonical_json='{}' WHERE sequence=1")
    connection.commit()
    connection.close()
    with pytest.raises(AuditLedgerError):
        AuditLedger(path)


def test_startup_anchor_loss_fails_closed(tmp_path) -> None:
    path = tmp_path / "orders.sqlite3"
    ledger = AuditLedger(path)
    ledger.append("SIGNAL", payload={"ok": True})
    ledger.close()
    (tmp_path / "orders.sqlite3.anchor").unlink()
    with pytest.raises(AuditLedgerError, match="anchor"):
        AuditLedger(path)


@pytest.mark.parametrize("state", ["PENDING", "DELIVERY_ATTEMPTED", "FAILED"])
def test_restart_replays_recoverable_anchor_suffix_with_stable_identity(tmp_path, state) -> None:
    path = tmp_path / "orders.sqlite3"
    delivered: list[tuple[str, str]] = []
    ledger = AuditLedger(path, anchor=lambda digest, event_id: delivered.append((digest, event_id)))
    first = ledger.append("SIGNAL", entity_type="proposal", entity_id="p1")
    second = ledger.append("STAGED", entity_type="proposal", entity_id="p1")
    ledger.close()
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE audit_anchor_outbox SET state=?,acked_at_utc=NULL WHERE event_id=?",
        (state, second.event_id),
    )
    connection.commit()
    connection.close()
    (tmp_path / "orders.sqlite3.anchor").write_text(
        json.dumps({"schema_version": 2, "sequence": first.sequence, "event_hash": first.event_hash, "event_id": first.event_id}),
        encoding="utf-8",
    )

    restarted = AuditLedger(path, anchor=lambda digest, event_id: delivered.append((digest, event_id)))
    row = restarted.connection.execute(
        "SELECT state,event_hash FROM audit_anchor_outbox WHERE event_id=?", (second.event_id,)
    ).fetchone()
    assert row == ("ACKED", second.event_hash)
    assert delivered[-1] == (second.event_hash, second.event_id)
    restarted.close()


def test_restart_after_db_commit_before_first_anchor_recovers(tmp_path) -> None:
    path = tmp_path / "orders.sqlite3"
    ledger = AuditLedger(path)
    event = ledger.append_in_transaction("SIGNAL", entity_type="proposal", entity_id="p1")
    ledger.connection.commit()
    ledger.connection.close()

    restarted = AuditLedger(path)
    assert restarted.connection.execute(
        "SELECT state FROM audit_anchor_outbox WHERE event_id=?", (event.event_id,)
    ).fetchone() == ("ACKED",)
    restarted.close()


def test_restart_acks_exact_external_event_without_duplicate_delivery(tmp_path) -> None:
    path = tmp_path / "orders.sqlite3"
    external: dict[str, str] = {}
    ledger = AuditLedger(path, anchor=lambda digest, event_id: external.__setitem__(event_id, digest))
    event = ledger.append("SIGNAL", entity_type="proposal", entity_id="p1")
    ledger.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE audit_anchor_outbox SET state='FAILED',acked_at_utc=NULL WHERE event_id=?", (event.event_id,))
    connection.commit()
    connection.close()
    (tmp_path / "orders.sqlite3.anchor").unlink()
    duplicate_writes: list[str] = []

    restarted = AuditLedger(
        path,
        anchor=lambda digest, event_id: duplicate_writes.append(event_id),
        anchor_lookup=lambda event_id: external.get(event_id),
    )
    assert duplicate_writes == []
    assert restarted.connection.execute(
        "SELECT state FROM audit_anchor_outbox WHERE event_id=?", (event.event_id,)
    ).fetchone() == ("ACKED",)
    restarted.close()


def test_external_event_id_hash_conflict_fails_closed(tmp_path) -> None:
    ledger = AuditLedger(
        tmp_path / "orders.sqlite3",
        anchor_lookup=lambda _event_id: "f" * 64,
    )
    with pytest.raises(AuditLedgerError, match="different hash"):
        ledger.append("SIGNAL", entity_type="proposal", entity_id="p1")
    ledger.close()
