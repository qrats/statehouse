"""Retry policy.

Sleeping is injected rather than imported so a test can assert on the delay
sequence without spending it. The jitter is deterministic given a seed, for
the same reason.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

from statehouse.core.errors import RateLimited, StatehouseError, TransientError

__all__ = ["RetryPolicy", "call_with_retry", "backoff_delays"]

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with a cap and optional jitter."""

    max_attempts: int = 4
    base_seconds: float = 2.0
    multiplier: float = 2.0
    max_seconds: float = 120.0
    jitter: float = 0.25
    retry_on: tuple[type[BaseException], ...] = (TransientError,)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_seconds < 0:
            raise ValueError("base_seconds cannot be negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if not 0.0 <= self.jitter < 1.0:
            raise ValueError("jitter must be in [0, 1)")

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Delay before ``attempt`` (1-based). Attempt 1 never waits."""
        if attempt <= 1:
            return 0.0
        raw = self.base_seconds * (self.multiplier ** (attempt - 2))
        capped = min(raw, self.max_seconds)
        if self.jitter and rng is not None:
            spread = capped * self.jitter
            capped = max(0.0, capped + rng.uniform(-spread, spread))
        return capped

    def should_retry(self, error: BaseException) -> bool:
        return isinstance(error, self.retry_on)


def backoff_delays(policy: RetryPolicy, *, seed: int | None = None) -> list[float]:
    """The full delay sequence a policy would produce, for logging and tests."""
    rng = random.Random(seed) if seed is not None else None
    return [policy.delay_for(attempt, rng=rng) for attempt in range(1, policy.max_attempts + 1)]


def call_with_retry(
    operation: Callable[[], T],
    policy: RetryPolicy | None = None,
    *,
    sleep: Callable[[float], None] | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    seed: int | None = None,
) -> T:
    """Run ``operation``, retrying per ``policy``.

    A :class:`~statehouse.core.errors.RateLimited` carrying
    ``retry_after_seconds`` overrides the computed backoff — when a source
    tells us how long to wait, arguing with it just gets us blocked. The final
    failure is re-raised unchanged so the caller sees the real cause.
    """
    active = policy or RetryPolicy()
    rng = random.Random(seed) if seed is not None else None
    waiter = sleep if sleep is not None else _noop_sleep
    last: BaseException | None = None

    for attempt in range(1, active.max_attempts + 1):
        if attempt > 1:
            delay = active.delay_for(attempt, rng=rng)
            if isinstance(last, RateLimited) and last.retry_after_seconds is not None:
                delay = max(delay, float(last.retry_after_seconds))
            if on_retry is not None and last is not None:
                on_retry(attempt, last, delay)
            if delay > 0:
                waiter(delay)
        try:
            return operation()
        except BaseException as error:  # noqa: BLE001 - re-raised below
            if not active.should_retry(error) or attempt == active.max_attempts:
                raise
            last = error

    raise StatehouseError("retry loop exhausted without result")  # pragma: no cover


def _noop_sleep(_seconds: float) -> None:
    """Default waiter. Deliberately does nothing.

    Callers that actually want to block pass ``time.sleep``; the DAG tasks pass
    a waiter that also renews the task heartbeat.
    """
    return None


def first_success(
    operations: Iterable[Callable[[], T]],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Try each operation in turn, returning the first that succeeds.

    Used where a jurisdiction publishes the same document at more than one
    URL and the primary is flaky. Raises the last error when all fail, and
    ``ValueError`` when handed nothing to try.
    """
    attempts = list(operations)
    if not attempts:
        raise ValueError("first_success requires at least one operation")
    last: BaseException | None = None
    for operation in attempts:
        try:
            return call_with_retry(operation, policy, sleep=sleep)
        except BaseException as error:  # noqa: BLE001
            last = error
    assert last is not None
    raise last
