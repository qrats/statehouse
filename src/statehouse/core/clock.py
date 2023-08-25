"""Time, injectable.

Nothing in the platform calls ``datetime.now()`` directly. Ingest decisions
depend on "now" (is this watermark stale? has the backoff elapsed?) and those
decisions have to be reproducible in a test and in a backfill, so the clock is
a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

__all__ = ["Clock", "SystemClock", "FrozenClock", "utc_now", "ensure_utc", "isoformat"]

UTC = timezone.utc


class Clock(Protocol):
    """Anything that can tell the time in UTC."""

    def now(self) -> datetime:  # pragma: no cover - protocol
        ...


class SystemClock:
    """The real clock. Always returns an aware UTC datetime."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "SystemClock()"


@dataclass
class FrozenClock:
    """A clock that only moves when told to.

    ``advance`` returns the new time so a test can chain reads without keeping
    a separate variable around.
    """

    current: datetime = field(default_factory=lambda: datetime(2024, 1, 1, tzinfo=UTC))

    def __post_init__(self) -> None:
        self.current = ensure_utc(self.current)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float = 0.0, **kwargs: float) -> datetime:
        self.current = self.current + timedelta(seconds=seconds, **kwargs)
        return self.current

    def set(self, moment: datetime) -> datetime:
        self.current = ensure_utc(moment)
        return self.current


def utc_now(clock: Clock | None = None) -> datetime:
    """Current UTC time, honouring an injected clock when one is given."""
    return (clock or SystemClock()).now()


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    Naive input is *assumed* to be UTC rather than local time: every source we
    read either states its offset or publishes in the jurisdiction's own zone,
    which the jurisdiction adapter has already resolved by the time a datetime
    reaches this function.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def isoformat(value: datetime) -> str:
    """Canonical serialisation: UTC, second precision, ``Z`` suffix."""
    return ensure_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")
