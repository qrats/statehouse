"""Ohio — the legislature's JSON API.

An actual documented API, which makes this the cheapest adapter in the
registry. The only wrinkle is that the API paginates with a cursor that
expires after ten minutes, so a slow run has to restart the page walk rather
than resume it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from statehouse.core.errors import ParseError
from statehouse.scraping.spiders.base import JurisdictionSpider
from statehouse.utils.text import clean_text

__all__ = ["OhioSpider", "parse_api_page"]

_LIST = "{base}/legislation/search?generalAssemblies={ga}&pageSize=100&pageNumber={page}"


def parse_api_page(payload: str | bytes) -> tuple[list[dict[str, Any]], str | None]:
    """Split an API page into records and the next cursor.

    Returns ``([], None)`` for a well-formed empty page. Raises
    :class:`~statehouse.core.errors.ParseError` when the payload is not JSON or
    lacks the ``items`` envelope, because silently treating an error page as
    "no results" is how a jurisdiction goes quiet for a week without anyone
    noticing.
    """
    try:
        body = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ParseError("Ohio API returned non-JSON") from exc
    if not isinstance(body, dict) or "items" not in body:
        raise ParseError("Ohio API response has no items envelope")
    items = body.get("items") or []
    if not isinstance(items, list):
        raise ParseError("Ohio API items is not a list")
    cursor = body.get("nextCursor") or None
    return [item for item in items if isinstance(item, dict)], cursor


class OhioSpider(JurisdictionSpider):
    name = "oh_bills"
    jurisdiction_code = "oh"

    def general_assembly(self) -> int:
        start = int(str(self.session).split("-")[0])
        return (start - 2023) // 2 + 135

    def index_urls(self) -> list[str]:
        return [
            _LIST.format(base=self.jurisdiction.portal_url, ga=self.general_assembly(), page=1)
        ]

    def parse_index(self, response: Any) -> Iterator[Any]:
        self.stats["index_pages"] += 1
        records, cursor = parse_api_page(response.body)
        for record in records:
            yield self.to_item(record, response.url)
        if cursor:
            page = int(response.url.split("pageNumber=")[-1].split("&")[0]) + 1
            url = _LIST.format(
                base=self.jurisdiction.portal_url, ga=self.general_assembly(), page=page
            )
            request = self.follow(response, url, self.parse_index, kind="index", priority=40)
            if request is not None:
                yield request

    def to_item(self, record: dict[str, Any], url: str) -> dict[str, Any]:
        identifier = clean_text(str(record.get("number") or ""), keep_paragraphs=False)
        item = self.base_item(identifier or "unknown", url)
        item["title"] = clean_text(str(record.get("shortTitle") or ""), keep_paragraphs=False)
        item["summary"] = clean_text(str(record.get("longTitle") or ""))
        item["status"] = clean_text(str(record.get("status") or ""), keep_paragraphs=False)
        item["introduced_on"] = record.get("introductionDate")
        item["chamber"] = "upper" if identifier.upper().startswith("S") else "lower"
        item["sponsors"] = [
            clean_text(str(person.get("name") or ""), keep_paragraphs=False)
            for person in record.get("sponsors") or []
            if isinstance(person, dict)
        ]
        item["subjects"] = [str(s) for s in record.get("topics") or []]
        item["actions"] = [
            {
                "date": entry.get("date"),
                "description": entry.get("description"),
                "chamber": entry.get("chamber"),
                "sequence": index,
            }
            for index, entry in enumerate(record.get("actions") or [])
            if isinstance(entry, dict)
        ]
        self.stats["documents"] += 1
        return item

    def parse_detail(self, response: Any) -> Iterator[Any]:  # pragma: no cover - unused
        """Ohio needs no detail fetch; the list endpoint is complete."""
        return iter(())
