"""New York — nyassembly.gov.

The Assembly site serves both chambers. Bills are paginated twenty to a page
behind a query string, and the docket is embedded in a ``<pre>`` block rather
than a table, which is why this adapter parses lines instead of cells.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from statehouse.scraping.spiders.base import JurisdictionSpider
from statehouse.utils.dates import parse_date
from statehouse.utils.text import clean_text, split_list

__all__ = ["NewYorkSpider", "parse_status_line"]

_INDEX = "{base}/leg/?default_fld=&leg_video=&bn={prefix}&term={term}&Actions=Y"
_LINE = re.compile(r"^(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<text>.+)$")


def parse_status_line(line: str) -> dict[str, Any] | None:
    """Parse one ``MM/DD/YY  description`` docket line.

    Returns ``None`` for continuation lines, which the caller appends to the
    previous action rather than dropping — several New York actions wrap.
    """
    match = _LINE.match(line.strip())
    if not match:
        return None
    return {"date": match.group("date"), "description": clean_text(match.group("text"))}


class NewYorkSpider(JurisdictionSpider):
    name = "ny_bills"
    jurisdiction_code = "ny"

    def index_urls(self) -> list[str]:
        term = str(self.session).split("-")[0]
        return [
            _INDEX.format(base=self.jurisdiction.portal_url, prefix=prefix, term=term)
            for prefix in ("A", "S")
        ]

    def parse_index(self, response: Any) -> Iterator[Any]:
        self.stats["index_pages"] += 1
        for href in response.css("div#bill-results a::attr(href)").getall():
            request = self.follow(response, href, self.parse_detail, kind="detail")
            if request is not None:
                yield request
        nxt = response.css("a[rel=next]::attr(href)").get()
        if nxt:
            request = self.follow(response, nxt, self.parse_index, kind="index", priority=40)
            if request is not None:
                yield request

    def parse_detail(self, response: Any) -> Iterator[Any]:
        self.stats["detail_pages"] += 1
        identifier = clean_text(response.css("span.nv_bold::text").get() or "")
        if not identifier:
            return

        item = self.base_item(identifier, response.url)
        item["title"] = clean_text(response.css("div.bill-title::text").get() or "")
        item["summary"] = clean_text(" ".join(response.css("div.summary ::text").getall()))
        item["sponsors"] = split_list(response.css("div.sponsors::text").get() or "")
        item["subjects"] = split_list(response.css("div.law-section::text").get() or "")
        item["introduced_on"] = parse_date(response.css("span.intro-date::text").get())

        block = "\n".join(response.css("pre.nv_bill_actions::text").getall())
        actions: list[dict[str, Any]] = []
        for line in block.splitlines():
            parsed = parse_status_line(line)
            if parsed is not None:
                parsed["sequence"] = len(actions)
                actions.append(parsed)
            elif actions and line.strip():
                actions[-1]["description"] = clean_text(
                    f"{actions[-1]['description']} {line.strip()}"
                )
        item["actions"] = actions

        body = clean_text(" ".join(response.css("pre.nv_bill_text::text").getall()))
        if body:
            item["versions"].append({"label": "as introduced", "text": body})

        self.stats["documents"] += 1
        yield item
