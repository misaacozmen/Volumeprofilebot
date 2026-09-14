from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from backtest.live.deal_ingestion import TerminalDealIngestionError, TerminalDealIngestor


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def deal(ticket: int, entry: str, volume: str, profit: str, *, order: int = 101, position: str = "501", offset: int = 0) -> dict[str, object]:
    return {
        "deal_id": ticket, "order": order, "position_id": position, "symbol": "INDEX",
        "magic": 77, "comment": "S1:test", "type": "BUY", "entry": entry, "reason": "CLIENT",
        "time_msc": int((NOW + timedelta(seconds=offset)).timestamp() * 1000),
        "volume": volume, "price": "100", "profit": profit,
        "commission": "-1", "swap": "0", "fee": "0",
    }


def ingestor(tmp_path, rows):
    return TerminalDealIngestor(
        tmp_path / "orders.sqlite3", read_deals=lambda _start, _end: tuple(rows),
        account_key="account-hash", campaign_id="campaign", candidate_hash="c" * 64,
        campaign_start=NOW - timedelta(days=30), magic=77, comment_prefix="S1:", now=lambda: NOW,
    )


def bind(store: TerminalDealIngestor) -> None:
    store.record_entry_risk_intent(
        proposal_id="proposal", instrument_id="instrument", request_hash="r" * 64,
        approved_volume="2", approved_risk_cash="100", created_at=NOW - timedelta(minutes=1),
    )
    store.bind_order(order_ticket=101, proposal_id="proposal", bound_at=NOW - timedelta(seconds=30))


def test_ingestion_allocates_partial_fill_and_counts_one_closed_episode(tmp_path) -> None:
    rows = [deal(1, "IN", "1", "0", offset=-20), deal(2, "OUT", ".4", "20", offset=-10), deal(3, "OUT", ".6", "32")]
    store = ingestor(tmp_path, rows)
    bind(store)
    snapshot = store.ingest()
    assert snapshot.high_water_ticket == 3
    assert snapshot.daily_realized_r == Decimal("0.98")  # (52 - 3 costs) / allocated risk 50
    assert snapshot.total_entry_count == 1
    assert len(snapshot.closed_episodes) == 1
    assert snapshot.closed_episodes[0]["starting_risk_cash"] == "50"
    assert snapshot.closed_episodes[0]["deal_tickets_json"] == "[1,2,3]"


def test_overlap_is_idempotent_but_immutable_conflict_rolls_back_cursor(tmp_path) -> None:
    rows = [deal(1, "IN", "1", "0", offset=-20)]
    store = ingestor(tmp_path, rows)
    bind(store)
    first = store.ingest()
    assert store.ingest(observed_at=NOW + timedelta(minutes=1)).deal_facts_hash == first.deal_facts_hash
    rows[0] = {**rows[0], "price": "101"}
    with pytest.raises(TerminalDealIngestionError, match="immutable conflict"):
        store.ingest(observed_at=NOW + timedelta(minutes=2))
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT high_water_ticket FROM terminal_ingestion_cursor").fetchone()[0] == 1


def test_read_failure_does_not_create_or_advance_cursor(tmp_path) -> None:
    def fail(_start, _end):
        raise OSError("offline")

    store = TerminalDealIngestor(
        tmp_path / "orders.sqlite3", read_deals=fail, account_key="account-hash",
        campaign_id="campaign", candidate_hash="c" * 64,
        campaign_start=NOW - timedelta(days=1), magic=77, now=lambda: NOW,
    )
    with pytest.raises(TerminalDealIngestionError, match="cursor was not advanced"):
        store.ingest()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM terminal_ingestion_cursor").fetchone()[0] == 0


def test_unknown_ownership_fails_closed_and_inout_splits_episodes(tmp_path) -> None:
    unbound = ingestor(tmp_path / "unbound", [deal(1, "IN", "1", "0")])
    with pytest.raises(TerminalDealIngestionError, match="binding"):
        unbound.ingest()
    rows = [
        deal(1, "IN", "1", "0", offset=-20),
        deal(2, "INOUT", "1.5", "10", order=102, offset=-10),
        deal(3, "OUT", ".5", "5", order=103),
    ]
    inout = ingestor(tmp_path / "inout", rows)
    bind(inout)
    inout.record_entry_risk_intent(
        proposal_id="reversal", instrument_id="instrument", request_hash="s" * 64,
        approved_volume="1", approved_risk_cash="40", created_at=NOW - timedelta(minutes=1),
    )
    inout.bind_order(order_ticket=102, proposal_id="reversal")
    snapshot = inout.ingest()
    assert len(snapshot.closed_episodes) == 2
    assert [row["starting_risk_cash"] for row in snapshot.closed_episodes] == ["50", "20"]


def test_parallel_daily_slot_reservations_cannot_exceed_limit(tmp_path) -> None:
    store = ingestor(tmp_path, [])

    def reserve(index: int) -> bool:
        try:
            store.reserve_daily_slot(
                session_date="2026-09-08", instrument_id="instrument",
                proposal_id=f"proposal-{index}", request_hash=f"{index:064x}",
                max_total=2, max_instrument=2,
            )
            return True
        except TerminalDealIngestionError:
            return False

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(pool.map(reserve, range(5))) == 2
    with pytest.raises(TerminalDealIngestionError, match="terminal broker evidence"):
        store.transition_daily_slot(proposal_id="proposal-0", state="RELEASED_CANCELLED")
