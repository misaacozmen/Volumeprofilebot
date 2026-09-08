"""Retry policy for safe broker reads only."""

from __future__ import annotations

from dataclasses import dataclass
import random
import time
import math
from typing import Any, Callable


class RetryError(RuntimeError):
    pass


class RetryableReadError(RetryError):
    def __init__(self, message: str = "retryable read failure", *, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class AllowedTransportError(RetryableReadError):
    """Explicitly classified safe transport failure for a read operation."""


class NonRetryableReadError(RetryError):
    pass


class SchemaError(NonRetryableReadError):
    pass


class ContractMismatchError(NonRetryableReadError):
    pass


class CircuitOpenError(RetryError):
    pass


@dataclass
class CircuitBreaker:
    threshold: int = 5
    open_seconds: float = 60.0
    failures: int = 0
    opened_at: float | None = None

    def is_open(self, now: float) -> bool:
        if self.opened_at is None:
            return False
        if now - self.opened_at >= self.open_seconds:
            self.opened_at = None
            self.failures = 0
            return False
        return True

    def before_read(self, now: float) -> None:
        if self.is_open(now):
            raise CircuitOpenError("read circuit breaker is open")

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_retryable_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = now

    def send_allowed(self, now: float) -> bool:
        return not self.is_open(now)


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    deadline_seconds: float = 60.0
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    breaker: CircuitBreaker | None = None
    sleeper: Callable[[float], None] = time.sleep
    rng: Callable[[float, float], float] = random.uniform
    monotonic: Callable[[], float] = time.monotonic

    def _delay(self, attempt: int, error: RetryableReadError) -> float:
        retry_after = error.retry_after
        if retry_after is not None:
            try:
                retry_after = float(retry_after)
            except (TypeError, ValueError, OverflowError) as exc:
                raise NonRetryableReadError("Retry-After is not numeric") from exc
            if not math.isfinite(retry_after):
                raise NonRetryableReadError("Retry-After is not finite")
            return max(1.0, min(60.0, retry_after))
        cap = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(0, attempt - 1)))
        return max(0.0, min(self.max_delay_seconds, float(self.rng(0.0, cap))))

    def read(self, operation: Callable[[], Any]) -> Any:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        started = self.monotonic()
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            now = self.monotonic()
            if self.breaker is not None:
                self.breaker.before_read(now)
            try:
                result = operation()
            except (NonRetryableReadError, PermissionError) as exc:
                raise
            except RetryableReadError as exc:
                if exc.status_code in {401, 403}:
                    raise NonRetryableReadError("authentication/permission failure is not retryable") from exc
                if exc.status_code is not None and not (exc.status_code == 429 or 500 <= exc.status_code <= 599):
                    raise NonRetryableReadError("HTTP status is not retryable") from exc
                last = exc
                if self.breaker is not None:
                    self.breaker.record_retryable_failure(now)
                if attempt >= self.max_attempts:
                    break
                delay = self._delay(attempt, exc)
            except Exception as exc:
                # Unknown exceptions are not transport evidence and must not
                # be retried. Callers must classify safe transport failures.
                raise NonRetryableReadError("unclassified read exception is not retryable") from exc
            else:
                if self.breaker is not None:
                    self.breaker.record_success()
                return result
            if self.monotonic() - started + delay > self.deadline_seconds:
                break
            self.sleeper(delay)
        if last is not None:
            raise RetryError(f"read retry deadline exhausted after {self.max_attempts} attempts") from last
        raise RetryError("read retry failed")


def retry_read(operation: Callable[[], Any], *, policy: RetryPolicy | None = None) -> Any:
    return (policy or RetryPolicy()).read(operation)


def write_once(operation: Callable[[], Any]) -> Any:
    """Call a broker write exactly once; unknown result is caller-owned."""
    return operation()
