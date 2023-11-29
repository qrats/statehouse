"""Shared spider behaviour.

Subclasses supply URL construction and page parsing. Everything else —
session resolution, the frontier, checkpoint interaction, run counters — is
here, because getting it slightly different in each of twenty spiders is how
this codebase looked before.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date
from typing import Any

from statehouse.config.jurisdictions import Jurisdiction, default_registry
from statehouse.core.errors import ParseError
from statehouse.scraping.frontier import CrawlFrontier, FrontierEntry
from statehouse.utils.urls import absolutise, registrable_host

try:  # pragma: no cover - Scrapy is optional in the pure-Python test path
    from scrapy import Spider as _BaseSpider
    from scrapy.http import Request, Response
except ImportError:  # pragma: no cover

    class _BaseSpider:  # type: ignore[no-redef]
        name = "spider"

        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    Request = Any  # type: ignore[assignment,misc]
    Response = Any  # type: ignore[assignment,misc]


__all__ = ["JurisdictionSpider"]

#: Priorities used across every spider so the frontier orders consistently.
PRIORITY_DETAIL = 100
PRIORITY_INDEX = 50
PRIORITY_TEXT = 25


class JurisdictionSpider(_BaseSpider):
    """Base class for a jurisdiction adapter.

    ``jurisdiction_code`` picks the registry entry. ``session`` and
    ``since`` may be passed on the command line by the DAG; when absent the
    spider ingests the current session with no lower bound.
    """

    name = "jurisdiction"
    jurisdiction_code = ""

    def __init__(
        self,
        session: str | None = None,
        since: str | None = None,
        until: str | None = None,
        registry: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # ``is None`` rather than ``or``: a registry is a Mapping, so an empty
        # one is falsy and would be silently replaced by the default.
        self.registry = default_registry() if registry is None else registry
        if not self.jurisdiction_code:
            raise ParseError("spider does not declare a jurisdiction_code", spider=self.name)
        self.jurisdiction: Jurisdiction = self.registry[self.jurisdiction_code]
        self.session = session or self.jurisdiction.session_label(date.today().year)
        self.since = since
        self.until = until
        self.frontier = CrawlFrontier(
            max_depth=4, allowed_host=registrable_host(self.jurisdiction.portal_url)
        )
        self.stats: dict[str, int] = {"index_pages": 0, "detail_pages": 0, "documents": 0}

    # -- construction ----------------------------------------------------

    def index_urls(self) -> Iterable[str]:
        """URLs that list documents. Subclasses must implement."""
        raise NotImplementedError

    def parse_index(self, response: Response) -> Iterator[Any]:
        """Yield detail URLs (and further index URLs) from a listing page."""
        raise NotImplementedError

    def parse_detail(self, response: Response) -> Iterator[Any]:
        """Yield a document item from a detail page."""
        raise NotImplementedError

    # -- Scrapy entry points ---------------------------------------------

    def start_requests(self) -> Iterator[Any]:  # pragma: no cover - needs Scrapy
        for url in self.index_urls():
            entry = FrontierEntry(url=url, priority=PRIORITY_INDEX, kind="index")
            if self.frontier.push(entry):
                yield Request(entry.url, callback=self.parse_index, meta=self.meta_for(entry))

    def meta_for(self, entry: FrontierEntry) -> dict[str, Any]:
        """Request metadata every middleware in the chain relies on."""
        return {
            "statehouse": {
                "jurisdiction": self.jurisdiction.code,
                "session": self.session,
                "kind": entry.kind,
                "depth": entry.depth,
                "host": entry.host,
            },
            "download_slot": entry.host,
        }

    def follow(
        self,
        response: Response,
        href: str,
        callback: Any,
        *,
        kind: str = "page",
        priority: int = PRIORITY_DETAIL,
        depth: int = 1,
    ) -> Any | None:
        """Queue a link, honouring the frontier's de-duplication.

        Returns ``None`` when the frontier rejects the URL, so a caller can
        simply ``yield from filter(None, ...)``.
        """
        url = absolutise(getattr(response, "url", self.jurisdiction.portal_url), href)
        if not url:
            return None
        entry = FrontierEntry(url=url, priority=priority, depth=depth, kind=kind)
        if not self.frontier.push(entry):
            return None
        return Request(entry.url, callback=callback, meta=self.meta_for(entry))

    def base_item(self, identifier: str, url: str) -> dict[str, Any]:
        """The fields every adapter fills in identically."""
        return {
            "jurisdiction": self.jurisdiction.code,
            "session": self.session,
            "identifier": identifier,
            "url": url,
            "fetch_method": self.jurisdiction.method.value,
            "actions": [],
            "sponsors": [],
            "subjects": [],
            "versions": [],
            "extras": {},
        }

    def closed(self, reason: str) -> None:  # pragma: no cover - Scrapy hook
        self.logger.info(
            "spider closed jurisdiction=%s session=%s reason=%s stats=%s",
            self.jurisdiction.code,
            self.session,
            reason,
            self.stats,
        )
