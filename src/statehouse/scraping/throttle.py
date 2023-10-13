"""Politeness enforcement.

Scrapy's AUTOTHROTTLE is per-spider and reactive; several of these portals
need a hard, absolute ceiling that holds across every spider and the browser
fetcher at once. Time is a parameter throughout — a throttle that can only be
tested by sleeping is a throttle nobody tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from statehouse.config.jurisdictions import PolitenessPolicy

__all__ = ["TokenBucket", "DomainThrottle"]


@dataclass
class TokenBucket:
    """Classic token bucket, driven by an explicit monotonic time value.

    ``capacity`` is the burst size and ``refill_per_second`` the steady rate.
    """

    capacity: float
    refill_per_second: float
    tokens: float = field(default=0.0)
    last_refill: float = 0.0

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        if not self.tokens:
            self.tokens = float(self.capacity)

    def _refill(self, now: float) -> None:
        if now <= self.last_refill:
            return
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.last_refill = now

    def take(self, now: float, amount: float = 1.0) -> bool:
        """Consume ``amount`` tokens if available. Returns whether it worked."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        self._refill(now)
        if self.tokens + 1e-9 >= amount:
            self.tokens -= amount
            return True
        return False

    def wait_time(self, now: float, amount: float = 1.0) -> float:
        """Seconds until ``amount`` tokens would be available. Zero if now."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        if amount > self.capacity:
            raise ValueError("amount exceeds bucket capacity")
        self._refill(now)
        if self.tokens + 1e-9 >= amount:
            return 0.0
        return (amount - self.tokens) / self.refill_per_second


class DomainThrottle:
    """A bucket per host, sized from each jurisdiction's politeness policy.

    Hosts not explicitly registered fall back to ``default_policy``, which
    keeps an adapter that follows an unexpected redirect from hammering
    somebody else's server.
    """

    def __init__(
        self,
        policies: Mapping[str, PolitenessPolicy] | None = None,
        *,
        default_policy: PolitenessPolicy | None = None,
    ) -> None:
        self._policies: dict[str, PolitenessPolicy] = {
            host.lower(): policy for host, policy in (policies or {}).items()
        }
        self._default = default_policy or PolitenessPolicy(requests_per_minute=10, concurrency=1)
        self._buckets: dict[str, TokenBucket] = {}

    def policy_for(self, host: str) -> PolitenessPolicy:
        return self._policies.get((host or "").lower(), self._default)

    def register(self, host: str, policy: PolitenessPolicy) -> None:
        """Add or replace a host policy, discarding any accumulated tokens."""
        key = (host or "").lower()
        self._policies[key] = policy
        self._buckets.pop(key, None)

    def _bucket(self, host: str) -> TokenBucket:
        key = (host or "").lower()
        bucket = self._buckets.get(key)
        if bucket is None:
            policy = self.policy_for(key)
            bucket = TokenBucket(
                capacity=float(policy.burst),
                refill_per_second=policy.requests_per_minute / 60.0,
            )
            self._buckets[key] = bucket
        return bucket

    def acquire(self, host: str, now: float) -> bool:
        """Try to spend one request against ``host``."""
        return self._bucket(host).take(now)

    def wait_time(self, host: str, now: float) -> float:
        """Seconds to wait before ``host`` will admit another request."""
        return self._bucket(host).wait_time(now)

    def hosts(self) -> list[str]:
        return sorted(set(self._policies) | set(self._buckets))
