from __future__ import annotations

import pytest

from backtest.live.retry import CircuitBreaker, ContractMismatchError, RetryError, RetryPolicy, RetryableReadError, retry_read, write_once


def test_read_retries_at_most_five_with_full_jitter_and_no_write_retry() -> None:
    calls: list[int] = []
    sleeps: list[float] = []
    policy = RetryPolicy(sleeper=sleeps.append, rng=lambda low, high: high, monotonic=lambda: 0.0)
    with pytest.raises(RetryError):
        retry_read(lambda: calls.append(1) or (_ for _ in ()).throw(RetryableReadError()), policy=policy)
    assert len(calls) == 5
    assert sleeps == [0.5, 1.0, 2.0, 4.0]
    writes: list[int] = []
    with pytest.raises(RuntimeError):
        write_once(lambda: writes.append(1) or (_ for _ in ()).throw(RuntimeError("unknown")))
    assert writes == [1]


def test_retry_after_is_clamped_and_auth_contract_errors_are_not_retried() -> None:
    sleeps: list[float] = []
    policy = RetryPolicy(sleeper=sleeps.append, monotonic=lambda: 0.0)
    with pytest.raises(RetryError):
        retry_read(lambda: (_ for _ in ()).throw(RetryableReadError(retry_after=500)), policy=policy)
    assert sleeps[0] == 60.0
    calls: list[int] = []
    with pytest.raises(ContractMismatchError):
        retry_read(lambda: calls.append(1) or (_ for _ in ()).throw(ContractMismatchError()), policy=policy)
    assert calls == [1]


def test_five_failures_open_circuit_and_reopen_after_sixty_seconds() -> None:
    breaker = CircuitBreaker()
    for index in range(5):
        breaker.record_retryable_failure(float(index))
    assert not breaker.send_allowed(10.0)
    assert breaker.send_allowed(70.0)
