from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from backtest.signals import SignalProposal
from backtest.live.approval import ApprovalStore, wire_request_hash
from backtest.live.audit_ledger import AuditLedger
from backtest.live.contracts import BrokerSnapshot, InstrumentContract, LiveRiskPolicy
from backtest.live.deal_ingestion import TerminalDealIngestor
from backtest.live.execution import Mt5ExecutionAdapter
from backtest.live.halt import HaltController
from backtest.live.instruments import InstrumentRegistry
from backtest.live.order_state import OrderState, OrderStateMachine
from backtest.live.production_flow import ProductionDependencies, ProductionOrderFlow
from backtest.live.risk_guard import RiskGuard
from backtest.live.settings import RuntimeSettings
from backtest.live.strategy_health import LockedOOSBaseline, StrategyHealth


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _contract() -> InstrumentContract:
    return InstrumentContract(
        "i1", "index", "MT5", "srv", "US100Cash", "UTC", "USD", "USD", "USD", 2,
        0.01, 0.01, 1.0, 0.01, 100.0, 0.01, 0.01, "price", 0,
    )


def _proposal() -> SignalProposal:
    return SignalProposal(
        "order-1", "c" * 64, "i1", "long", 100.33333333333333, 99.0, 103.0,
        NOW - timedelta(seconds=1), NOW + timedelta(seconds=10), "e" * 64,
    )


def _staged(proposal: SignalProposal) -> dict[str, object]:
    request = {"symbol": "US100Cash"}
    return {
        "proposal_id": proposal.proposal_id,
        "candidate_hash": proposal.candidate_hash,
        "instrument_id": proposal.instrument_id,
        "direction": proposal.direction,
        "entry_price": proposal.entry_price,
        "stop_price": proposal.stop_price,
        "target_price": proposal.target_price,
        "decision_time": proposal.decision_time.isoformat(),
        "expires_at": proposal.expires_at.isoformat(),
        "evidence_hash": proposal.evidence_hash,
        "wire_request": request,
        "wire_request_hash": wire_request_hash(request),
    }


def test_actual_super1_production_flow_sends_once_then_reconciles_fake_mt5(tmp_path: Path) -> None:
    proposal = _proposal()
    campaign_id, account_key, release_id = "campaign-1", "12345678", "release-1"
    approvals = ApprovalStore(tmp_path / "approvals.sqlite3", now=lambda: NOW)
    lease = {
        "state": "ACTIVE", "lease_id": "lease-1", "invocation_nonce": "nonce-1",
        "runner_sid": "S-1-5-18", "authorized_operator_sid": "S-1-5-19", "campaign_id": campaign_id,
        "account": account_key, "release_id": release_id,
    }
    approvals.stage(_staged(proposal), campaign_id=campaign_id, account_key=account_key, release_id=release_id, candidate_hash=proposal.candidate_hash)
    approval = approvals.approve(proposal.proposal_id, lease=lease, operator_sid=lease["authorized_operator_sid"], release_id=release_id, candidate_hash=proposal.candidate_hash, now=NOW)

    contract = _contract()
    policy = LiveRiskPolicy(
        -1.0, (("i1", 1), ("i2", 2)), 3,
        (("i1", "US100Cash"), ("i2", "US500Cash")), 1, 2, 300,
        (("i1", 100.0), ("i2", 100.0)),
    )

    def profit(action: str, symbol: str, volume: float, entry: float, stop: float) -> float:
        return -abs(entry - stop) * volume * 10.0

    def margin(action: str, symbol: str, volume: float, entry: float) -> float:
        return volume * 100.0

    snapshots = iter(
        (
                BrokerSnapshot(NOW, 10_000.0, 5_000.0, daily_realized_r=0.0, strategy_health="ACTIVE", halt=False, instrument_contract=contract, total_stop_risk=0.0, snapshot_hash="1" * 64, policy_hash=policy.policy_hash, instrument_contract_hash=contract.contract_hash, order_calc_profit=profit, order_calc_margin=margin, total_entry_count=0, leverage=2.0, pair_exposure_percent=0.0, concentration_percent=0.0, deal_facts_hash="d" * 64, deal_reconciliation_at=NOW, deal_watermark_time_msc=0, deal_watermark_ticket=0),
                BrokerSnapshot(NOW, 10_000.0, 5_000.0, daily_realized_r=0.0, strategy_health="ACTIVE", approval=True, halt=False, instrument_contract=contract, total_stop_risk=0.0, snapshot_hash="2" * 64, policy_hash=policy.policy_hash, instrument_contract_hash=contract.contract_hash, order_calc_profit=profit, order_calc_margin=margin, total_entry_count=0, leverage=2.0, pair_exposure_percent=0.0, concentration_percent=0.0, deal_facts_hash="d" * 64, deal_reconciliation_at=NOW, deal_watermark_time_msc=0, deal_watermark_ticket=0),
        )
    )

    class FakeMt5:
        def __init__(self) -> None:
            self.send_calls = 0

        def order_send(self, request: dict[str, object]) -> object:
            self.send_calls += 1
            return SimpleNamespace(retcode=10009, order=123, deal=456)

        def orders_get(self) -> tuple[object, ...]:
            return (SimpleNamespace(ticket=123),)

    fake_mt5 = FakeMt5()
    adapter = Mt5ExecutionAdapter(
        fake_mt5,
        request_builder=lambda _order: {"symbol": "US100Cash"},
        write_once=fake_mt5.order_send,
    )
    baseline = LockedOOSBaseline("c" * 64, 100, 100, (2022, 2023, 2024), 0.0, 1.0, 2.0, 7)
    health = StrategyHealth("ACTIVE", baseline=baseline, expected_candidate_hash="c" * 64)
    ledger = AuditLedger(tmp_path / "audit.sqlite3", campaign_id=campaign_id, account_key=account_key)
    dependencies = ProductionDependencies(
        risk_guard=RiskGuard(policy=policy, clock=lambda: NOW), order_state=OrderStateMachine(), audit_ledger=ledger,
        halt_controller=HaltController(tmp_path / "runtime"), instrument_registry=InstrumentRegistry([contract]),
        approval_store=approvals, runtime_settings=RuntimeSettings("DEMO_ORDER"), strategy_health=health,
        execution_adapter=adapter,
        deal_ingestor=TerminalDealIngestor(
            tmp_path / "approvals.sqlite3", read_deals=lambda _start, _end: (),
            account_key=account_key, campaign_id=campaign_id, candidate_hash="c" * 64,
            campaign_start=NOW - timedelta(days=1), magic=1, now=lambda: NOW,
        ),
    )

    flow = ProductionOrderFlow(dependencies)
    result = flow.send(
        proposal,
        snapshot_provider=lambda: next(snapshots),
        approval_id=approval.approval_id,
        campaign_id=campaign_id,
        account_key=account_key,
        release_id=release_id,
        candidate_hash=proposal.candidate_hash,
    )

    assert result.retcode == 10009
    assert fake_mt5.send_calls == 1
    assert dependencies.order_state.state is OrderState.SUBMITTED
    row = ledger.connection.execute("SELECT state, request_hash FROM order_state_records WHERE order_id=?", (proposal.proposal_id,)).fetchone()
    assert row and row[0] == "SUBMITTED" and len(row[1]) == 64
    ledger.close()
