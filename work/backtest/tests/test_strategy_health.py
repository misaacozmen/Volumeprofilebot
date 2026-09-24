from __future__ import annotations

import pytest
from pathlib import Path

from backtest.live.strategy_health import LockedOOSBaseline, StrategyHealth, StrategyHealthError, StrategyHealthState


def baseline(**updates):
    value = dict(candidate_hash="candidate", closed_trades=100, valid_sessions=100, years=(2021, 2022, 2023), rolling_net_r_p05=-1.0, drawdown_p95=5.0, drawdown_p99=7.0, seed=1)
    value.update(updates)
    return LockedOOSBaseline(**value)


def deals(count: int, *, r: float = 0.0, start: int = 0):
    return [
        {
            "deal_id": str(index),
            "r": r,
            "terminal_known_time": f"2026-01-{(index % 28) + 1:02d}T12:00:00+00:00",
            "session_id": f"session-{index}",
        }
        for index in range(start, start + count)
    ]


def test_strategy_health_is_monotonic_and_decay_requires_checkpoint() -> None:
    health = StrategyHealth(baseline=baseline())
    health.activate(broker_deals=deals(60))
    assert health.state == StrategyHealthState.ACTIVE
    first = health.evaluate(broker_deals=deals(60, r=-0.1))
    assert first.state == StrategyHealthState.MONITORING
    second = health.evaluate(broker_deals=deals(70, r=-0.1))
    assert second.state == StrategyHealthState.DECAYED
    with pytest.raises(StrategyHealthError):
        health.transition(StrategyHealthState.ACTIVE)
    assert not health.allows_new_position()


def test_insufficient_baseline_blocks_promotion_and_severe_dd_decays_once() -> None:
    health = StrategyHealth(baseline=baseline(closed_trades=99))
    assert health.evaluate(broker_deals=deals(100)).insufficient_baseline
    health = StrategyHealth(baseline=baseline())
    health.activate(broker_deals=deals(60))
    assert health.evaluate(broker_deals=deals(60, r=-1.0)).state == StrategyHealthState.DECAYED


def test_decayed_disables_only_after_owned_exposure_is_flat() -> None:
    health = StrategyHealth(StrategyHealthState.DECAYED, baseline=baseline())
    assert health.reconcile_decay(owned_positions=1, owned_pending_orders=0).state == StrategyHealthState.DECAYED
    assert health.reconcile_decay(owned_positions=0, owned_pending_orders=0).state == StrategyHealthState.DISABLED


def test_canonical_deal_set_is_append_only(tmp_path: Path) -> None:
    path = tmp_path / "health.sqlite3"
    health = StrategyHealth(baseline=baseline())
    health.activate(broker_deals=deals(2))
    health.persist_canonical(path)
    health.activate(broker_deals=deals(1))
    with pytest.raises(StrategyHealthError, match="append-only"):
        health.persist_canonical(path)
