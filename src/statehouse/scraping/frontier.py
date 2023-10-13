"""The crawl frontier.

Scrapy has its own scheduler, but it forgets everything between runs and it
does not know that a bill detail page matters more than the eighteenth page of
a sponsor index. The frontier holds that judgement, and it is what a
checkpoint serialises.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from statehouse.utils.urls import canonical_url, registrable_host, same_site

__all__ = ["FrontierEntry", "CrawlFrontier"]


@dataclass(frozen=True)
class FrontierEntry:
    """One URL waiting to be fetched."""

    url: str
    priority: int = 0
    depth: int = 0
    kind: str = "page"
    referer: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", canonical_url(self.url))
        if not self.url:
            raise ValueError("FrontierEntry requires a url")
        if self.depth < 0:
            raise ValueError("depth cannot be negative")

    @property
    def host(self) -> str:
        return registrable_host(self.url)


class CrawlFrontier:
    """Priority queue of URLs with de-duplication and depth limiting.

    Higher priority is fetched first; ties break on insertion order so a crawl
    is reproducible. Every URL is canonicalised on the way in, which is what
    stops the same page entering four times under four tracking parameters.
    """

    def __init__(
        self,
        *,
        max_depth: int = 4,
        allowed_host: str | None = None,
        seen: Iterable[str] = (),
    ) -> None:
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative")
        self.max_depth = max_depth
        self.allowed_host = allowed_host
        self._seen: set[str] = {canonical_url(url) for url in seen if url}
        self._heap: list[tuple[int, int, FrontierEntry]] = []
        self._counter = 0
        self._rejected = 0

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    @property
    def rejected_count(self) -> int:
        return self._rejected

    def push(self, entry: FrontierEntry) -> bool:
        """Queue an entry. Returns whether it was accepted.

        Rejected for any of: already seen, deeper than ``max_depth``, or off
        the allowed host. Rejections are counted rather than raised — a crawl
        that dies because a page linked somewhere unexpected is a bad crawl.
        """
        if entry.depth > self.max_depth:
            self._rejected += 1
            return False
        if self.allowed_host and not same_site(entry.url, f"https://{self.allowed_host}"):
            self._rejected += 1
            return False
        if entry.url in self._seen:
            self._rejected += 1
            return False
        self._seen.add(entry.url)
        heapq.heappush(self._heap, (-entry.priority, self._counter, entry))
        self._counter += 1
        return True

    def push_many(self, entries: Iterable[FrontierEntry]) -> int:
        """Queue several entries, returning how many were accepted."""
        return sum(1 for entry in entries if self.push(entry))

    def pop(self) -> FrontierEntry | None:
        """Highest-priority entry, or ``None`` when the frontier is empty."""
        if not self._heap:
            return None
        return heapq.heappop(self._heap)[2]

    def drain(self, limit: int | None = None) -> list[FrontierEntry]:
        """Pop up to ``limit`` entries in priority order."""
        if limit is not None and limit < 0:
            raise ValueError("limit cannot be negative")
        out: list[FrontierEntry] = []
        while self._heap and (limit is None or len(out) < limit):
            out.append(heapq.heappop(self._heap)[2])
        return out

    def pending_urls(self) -> list[str]:
        """URLs still queued, in priority order. Used to write a checkpoint."""
        return [entry.url for _p, _c, entry in sorted(self._heap)]

    def __iter__(self) -> Iterator[FrontierEntry]:
        while self._heap:
            yield heapq.heappop(self._heap)[2]
