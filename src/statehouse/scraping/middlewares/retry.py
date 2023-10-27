"""Retry classification.

Scrapy's built-in retry middleware treats every failure the same. A portal
serving a maintenance page with HTTP 200 is a retry; a 404 on a bill that was
withdrawn is not; a 403 after twenty fast requests is a politeness problem and
retrying immediately makes it worse.
"""

from __future__ import annotations

import re
from typing import Any

from statehouse.core.errors import FetchError, PortalUnavailable, RateLimited
from statehouse.utils.retry import RetryPolicy

__all__ = ["ClassifyingRetryMiddleware", "classify_response", "MAINTENANCE_MARKERS"]

#: Phrases that mean "this 200 is not a document".
MAINTENANCE_MARKERS: tuple[re.Pattern[bytes], ...] = (
    re.compile(rb"(?i)site\s+(?:is\s+)?(?:down|unavailable)\s+for\s+maintenance"),
    re.compile(rb"(?i)scheduled\s+maintenance"),
    re.compile(rb"(?i)temporarily\s+unavailable"),
    re.compile(rb"(?i)service\s+unavailable"),
    re.compile(rb"(?i)please\s+try\s+again\s+later"),
    re.compile(rb"(?i)request\s+blocked"),
)

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 522, 524})
_FATAL_STATUS = frozenset({400, 401, 403, 404, 405, 410, 451})

#: Below this, a 200 response is an empty shell rather than a page.
MIN_PLAUSIBLE_BODY = 512


def classify_response(status: int, body: bytes) -> Exception | None:
    """Return the error a response represents, or ``None`` when it is fine.

    A short 2xx body or one carrying a maintenance marker is
    :class:`PortalUnavailable`, which is retryable; 429 and 503 are
    :class:`RateLimited`; other retryable statuses are :class:`FetchError`;
    the fatal set returns ``None`` so the spider can decide what a missing
    page means for that jurisdiction.
    """
    if status in (429, 503):
        return RateLimited("source refused the request", status=status)
    if status in _RETRYABLE_STATUS:
        return FetchError("retryable status", status=status)
    if status in _FATAL_STATUS:
        return None
    if 200 <= status < 300:
        payload = body or b""
        if len(payload) < MIN_PLAUSIBLE_BODY:
            return PortalUnavailable("response body is implausibly short", size=len(payload))
        for marker in MAINTENANCE_MARKERS:
            if marker.search(payload[:20_000]):
                return PortalUnavailable("maintenance page served with a 2xx status")
    return None


class ClassifyingRetryMiddleware:
    """Turns responses into typed errors and counts what it saw."""

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self.policy = policy or RetryPolicy()
        self.counts: dict[str, int] = {}

    @classmethod
    def from_crawler(cls, crawler: Any) -> "ClassifyingRetryMiddleware":  # pragma: no cover
        return cls()

    def _count(self, label: str) -> None:
        self.counts[label] = self.counts.get(label, 0) + 1

    def process_response(self, request: Any, response: Any, spider: Any) -> Any:
        status = int(getattr(response, "status", 200))
        body = getattr(response, "body", b"") or b""
        error = classify_response(status, body)
        if error is None:
            self._count("ok" if 200 <= status < 300 else f"fatal_{status}")
            return response

        self._count(type(error).__name__)
        attempts = int((getattr(request, "meta", {}) or {}).get("retry_times", 0)) + 1
        if attempts >= self.policy.max_attempts:
            self._count("exhausted")
            raise error

        retry = request.replace(dont_filter=True)
        retry.meta["retry_times"] = attempts
        retry.priority = getattr(request, "priority", 0) - 10
        if isinstance(error, RateLimited) and error.retry_after_seconds:
            retry.meta["download_delay"] = float(error.retry_after_seconds)
        return retry

    def process_exception(self, request: Any, exception: Exception, spider: Any) -> Any:
        """Retry transport-level failures the same way."""
        self._count(type(exception).__name__)
        attempts = int((getattr(request, "meta", {}) or {}).get("retry_times", 0)) + 1
        if attempts >= self.policy.max_attempts:
            return None
        retry = request.replace(dont_filter=True)
        retry.meta["retry_times"] = attempts
        return retry
