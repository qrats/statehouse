"""Politeness middleware.

Wraps :class:`~statehouse.scraping.throttle.DomainThrottle` so the same budget
applies whether a request came from a spider, the browser fetcher or a
one-off backfill.
"""

from __future__ import annotations

import time
from typing import Any

from statehouse.config.jurisdictions import default_registry
from statehouse.core.errors import RateLimited
from statehouse.scraping.throttle import DomainThrottle
from statehouse.utils.urls import registrable_host

__all__ = ["PolitenessMiddleware"]


class PolitenessMiddleware:
    """Delays or defers requests that would exceed a host's budget.

    Scrapy has no way to say "come back in n seconds", so a request that
    cannot be admitted is failed with :class:`RateLimited` carrying
    ``retry_after_seconds``; the retry middleware downstream reschedules it.
    That is uglier than blocking but keeps the reactor free.
    """

    def __init__(self, throttle: DomainThrottle | None = None, clock: Any = None) -> None:
        self.throttle = throttle or self._build_default()
        self._clock = clock or time.monotonic
        self.deferred = 0
        self.admitted = 0

    @staticmethod
    def _build_default() -> DomainThrottle:
        registry = default_registry()
        policies = {
            registrable_host(entry.portal_url): entry.politeness
            for entry in registry.values()
        }
        return DomainThrottle(policies)

    @classmethod
    def from_crawler(cls, crawler: Any) -> "PolitenessMiddleware":  # pragma: no cover
        return cls()

    def process_request(self, request: Any, spider: Any) -> None:
        host = registrable_host(getattr(request, "url", ""))
        now = self._clock()
        if self.throttle.acquire(host, now):
            self.admitted += 1
            return None
        wait = self.throttle.wait_time(host, now)
        self.deferred += 1
        raise RateLimited(
            "host budget exhausted", retry_after_seconds=wait, host=host, spider=getattr(spider, "name", "")
        )

    def process_response(self, request: Any, response: Any, spider: Any) -> Any:
        """Honour an explicit ``Retry-After`` by draining the host's bucket."""
        status = getattr(response, "status", 200)
        if status in (429, 503):
            host = registrable_host(getattr(request, "url", ""))
            header = b""
            headers = getattr(response, "headers", None)
            if headers is not None:
                header = headers.get("Retry-After", b"") or b""
            try:
                seconds = float(header.decode() if isinstance(header, bytes) else header)
            except (TypeError, ValueError):
                seconds = 30.0
            raise RateLimited(
                "source asked us to slow down",
                retry_after_seconds=seconds,
                host=host,
                status=status,
            )
        return response

    def stats(self) -> dict[str, int]:
        return {"admitted": self.admitted, "deferred": self.deferred}
