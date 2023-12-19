"""Browser pool.

Chrome instances are the most expensive thing this platform runs — roughly
400MB each and slow to start — so they are pooled, reused across requests, and
recycled on a page count rather than left to leak.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from statehouse.core.errors import PermanentError, TransientError

__all__ = ["BrowserLease", "BrowserPool", "DriverFactory"]

DriverFactory = Callable[[], Any]


@dataclass
class BrowserLease:
    """One checked-out driver and its usage counters."""

    driver: Any
    pages_served: int = 0
    failures: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def record_page(self) -> int:
        self.pages_served += 1
        return self.pages_served

    def record_failure(self) -> int:
        self.failures += 1
        return self.failures


class BrowserPool:
    """A fixed-size pool of drivers.

    ``recycle_after`` bounds how many pages one driver serves before it is
    quit and replaced; long-lived Chrome sessions accumulate memory and, on
    two of the portals here, cookies that eventually break navigation.
    ``max_failures`` retires a driver that keeps erroring rather than handing
    it out again.
    """

    def __init__(
        self,
        factory: DriverFactory,
        *,
        size: int = 2,
        recycle_after: int = 200,
        max_failures: int = 3,
    ) -> None:
        if size <= 0:
            raise ValueError("size must be positive")
        if recycle_after <= 0:
            raise ValueError("recycle_after must be positive")
        if max_failures <= 0:
            raise ValueError("max_failures must be positive")
        self._factory = factory
        self.size = size
        self.recycle_after = recycle_after
        self.max_failures = max_failures
        self._idle: list[BrowserLease] = []
        self._leased: list[BrowserLease] = []
        self._created = 0
        self._recycled = 0
        self._closed = False

    @property
    def created(self) -> int:
        return self._created

    @property
    def recycled(self) -> int:
        return self._recycled

    @property
    def available(self) -> int:
        return len(self._idle) + max(0, self.size - len(self._idle) - len(self._leased))

    @property
    def in_use(self) -> int:
        return len(self._leased)

    def acquire(self) -> BrowserLease:
        """Check out a driver, starting one if the pool has room."""
        if self._closed:
            raise PermanentError("browser pool is closed")
        if self._idle:
            lease = self._idle.pop()
        elif len(self._leased) < self.size:
            lease = BrowserLease(driver=self._factory())
            self._created += 1
        else:
            raise TransientError("no browser available", size=self.size, in_use=len(self._leased))
        self._leased.append(lease)
        return lease

    def release(self, lease: BrowserLease) -> None:
        """Return a driver, recycling it if it is worn out."""
        if lease in self._leased:
            self._leased.remove(lease)
        if self._closed:
            self._quit(lease)
            return
        if lease.pages_served >= self.recycle_after or lease.failures >= self.max_failures:
            self._quit(lease)
            self._recycled += 1
            return
        self._idle.append(lease)

    @contextmanager
    def lease(self) -> Iterator[BrowserLease]:
        """Acquire for the duration of a block, always releasing.

        A failure inside the block is counted against the driver before it is
        released, so a driver that fails repeatedly is retired even when every
        individual caller handled its own error.
        """
        lease = self.acquire()
        try:
            yield lease
        except Exception:
            lease.record_failure()
            raise
        finally:
            self.release(lease)

    def _quit(self, lease: BrowserLease) -> None:
        quit_fn = getattr(lease.driver, "quit", None)
        if callable(quit_fn):
            try:
                quit_fn()
            except Exception:  # noqa: BLE001 - a dead driver cannot be quit twice
                pass

    def close(self) -> None:
        """Quit every driver. Safe to call more than once."""
        self._closed = True
        for lease in list(self._idle) + list(self._leased):
            self._quit(lease)
        self._idle.clear()
        self._leased.clear()

    def __enter__(self) -> BrowserPool:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
