"""Illinois — ilga.gov.

A classic ASP.NET postback grid: the bill list is only reachable by submitting
a form, and paging replays ``__VIEWSTATE``. Scrapy can do that, but the site
also drops the session after roughly forty requests, so this adapter runs
through the browser fetcher and hands rendered HTML back to the same parsing
code.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from statehouse.scraping.spiders.base import JurisdictionSpider
from statehouse.utils.dates import parse_date
from statehouse.utils.text import clean_text, split_list

__all__ = ["IllinoisSpider"]

_INDEX = "{base}/legislation/grplist.asp?num1=1&num2=9999&DocTypeID={doc}&GA={ga}&SessionId={sid}"


class IllinoisSpider(JurisdictionSpider):
    name = "il_bills"
    jurisdiction_code = "il"

    #: The doc types worth ingesting; the portal offers a dozen more that are
    #: procedural noise.
    doc_types = ("HB", "SB", "HR", "SR", "HJR", "SJR")

    def general_assembly(self) -> int:
        """GA number for the biennium (103rd = 2023-2024)."""
        start = int(str(self.session).split("-")[0])
        return (start - 2023) // 2 + 103

    def index_urls(self) -> list[str]:
        ga = self.general_assembly()
        session_id = self.jurisdiction_session_id(ga)
        return [
            _INDEX.format(base=self.jurisdiction.portal_url, doc=doc, ga=ga, sid=session_id)
            for doc in self.doc_types
        ]

    def jurisdiction_session_id(self, general_assembly: int) -> int:
        """Map a GA number onto the portal's opaque ``SessionId``.

        There is no formula the site documents; the offset below was derived
        from the last six assemblies and is checked by the smoke test, which
        fails loudly if the portal renumbers.
        """
        return general_assembly - 15

    def parse_index(self, response: Any) -> Iterator[Any]:
        self.stats["index_pages"] += 1
        rows = response.css("table td li a")
        if not rows:
            self.logger.warning("empty grid, session probably expired url=%s", response.url)
        for link in rows:
            href = link.css("::attr(href)").get()
            if href and "BillStatus" in href:
                request = self.follow(response, href, self.parse_detail, kind="detail")
                if request is not None:
                    yield request

    def parse_detail(self, response: Any) -> Iterator[Any]:
        self.stats["detail_pages"] += 1
        heading = clean_text(response.css("span.heading::text").get() or "")
        identifier = heading.split("-")[0].strip() if heading else ""
        if not identifier:
            return

        item = self.base_item(identifier, response.url)
        item["title"] = clean_text(response.css("span.content span::text").get() or "")
        item["summary"] = clean_text(" ".join(response.css("span.synopsis ::text").getall()))
        item["sponsors"] = split_list(" ".join(response.css("a[href*=member] ::text").getall()))
        item["introduced_on"] = parse_date(response.css("span.first-reading::text").get())

        for index, row in enumerate(response.css("table.actions tr")):
            cells = [clean_text(c) for c in row.css("td::text").getall()]
            if len(cells) < 3:
                continue
            item["actions"].append(
                {
                    "date": cells[0],
                    "chamber": cells[1],
                    "description": cells[2],
                    "sequence": index,
                }
            )

        body = clean_text(" ".join(response.css("div#fulltext ::text").getall()))
        if body:
            item["versions"].append({"label": "introduced", "text": body})

        self.stats["documents"] += 1
        yield item
