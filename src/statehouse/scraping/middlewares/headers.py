"""Request headers.

We identify ourselves honestly — the user agent names the bot and an operator
address — but a couple of portals serve a stripped page to anything that does
not look like a browser, so the accept headers are browser-shaped.
"""

from __future__ import annotations

from typing import Any

from statehouse.config.settings import load_settings

__all__ = ["RotatingHeadersMiddleware", "BASE_HEADERS"]

BASE_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_JSON_HEADERS = {
    "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}

_XML_HEADERS = {"Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8"}


class RotatingHeadersMiddleware:
    """Applies the right header set for the kind of request being made.

    "Rotating" is historical: an earlier version cycled user agents, which was
    both rude and ineffective. It now only varies the accept headers by the
    ``kind`` the spider recorded in request meta.
    """

    def __init__(self, user_agent: str | None = None) -> None:
        self.user_agent = user_agent or load_settings().user_agent

    @classmethod
    def from_crawler(cls, crawler: Any) -> RotatingHeadersMiddleware:  # pragma: no cover
        return cls()

    def headers_for(self, kind: str) -> dict[str, str]:
        headers = dict(BASE_HEADERS)
        headers["User-Agent"] = self.user_agent
        if kind == "api":
            headers.update(_JSON_HEADERS)
        elif kind in ("bulk", "xml"):
            headers.update(_XML_HEADERS)
        return headers

    def process_request(self, request: Any, spider: Any) -> None:
        meta = getattr(request, "meta", {}) or {}
        kind = (meta.get("statehouse") or {}).get("kind", "page")
        for key, value in self.headers_for(kind).items():
            if key not in request.headers:
                request.headers[key] = value
        return None
