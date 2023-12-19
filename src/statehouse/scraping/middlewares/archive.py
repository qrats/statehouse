"""Raw archival.

Every successful fetch is written to the raw lake before anything parses it.
This is the single most useful thing in the platform: a parser bug on a
portal that keeps ninety days of history is a reprocess, not a data loss.
"""

from __future__ import annotations

from typing import Any

from statehouse.core.clock import Clock, utc_now
from statehouse.core.enums import FetchMethod
from statehouse.core.models import RawFetch, SourceRef

__all__ = ["RawArchiveMiddleware", "build_raw_fetch"]


def build_raw_fetch(
    url: str,
    body: bytes,
    *,
    jurisdiction: str,
    status: int = 200,
    method: str | FetchMethod = FetchMethod.HTTP,
    content_type: str = "text/html",
    spider: str | None = None,
    headers: dict[str, str] | None = None,
    clock: Clock | None = None,
) -> RawFetch:
    """Assemble the archive record for one fetch."""
    return RawFetch(
        source=SourceRef(
            jurisdiction=jurisdiction,
            url=url,
            method=FetchMethod.parse(method, FetchMethod.HTTP),
            fetched_at=utc_now(clock),
            http_status=status,
            spider=spider,
        ),
        body=body,
        content_type=content_type,
        headers=headers or {},
    )


class RawArchiveMiddleware:
    """Hands each successful response to a writer.

    The writer is injected: production passes the S3 writer, the local runner
    passes one that drops files under ``var/raw``, and tests pass a list. A
    writer failure is logged and swallowed — losing the archive copy of one
    page is bad, but failing the crawl over it is worse.
    """

    def __init__(self, writer: Any = None, clock: Clock | None = None) -> None:
        self.writer = writer
        self._clock = clock
        self.archived = 0
        self.failures = 0

    @classmethod
    def from_crawler(cls, crawler: Any) -> RawArchiveMiddleware:  # pragma: no cover
        return cls()

    def process_response(self, request: Any, response: Any, spider: Any) -> Any:
        status = int(getattr(response, "status", 200))
        if not 200 <= status < 300 or self.writer is None:
            return response

        meta = (getattr(request, "meta", {}) or {}).get("statehouse") or {}
        headers = {}
        raw_headers = getattr(response, "headers", None)
        if raw_headers is not None:
            for key, value in dict(raw_headers).items():
                name = key.decode() if isinstance(key, bytes) else str(key)
                first = value[0] if isinstance(value, list | tuple) and value else value
                headers[name] = first.decode() if isinstance(first, bytes) else str(first)

        fetch = build_raw_fetch(
            getattr(response, "url", ""),
            getattr(response, "body", b"") or b"",
            jurisdiction=str(
                meta.get("jurisdiction") or getattr(spider, "jurisdiction_code", "unknown")
            ),
            status=status,
            method=meta.get("method", FetchMethod.HTTP),
            content_type=headers.get("Content-Type", "text/html").split(";")[0],
            spider=getattr(spider, "name", None),
            headers=headers,
            clock=self._clock,
        )

        try:
            self.writer.write(fetch)
            self.archived += 1
        except Exception as exc:  # noqa: BLE001 - archival must never fail a crawl
            self.failures += 1
            logger = getattr(spider, "logger", None)
            if logger is not None:
                logger.warning("raw archive write failed url=%s error=%s", fetch.source.url, exc)
        return response

    def stats(self) -> dict[str, int]:
        return {"archived": self.archived, "archive_failures": self.failures}
