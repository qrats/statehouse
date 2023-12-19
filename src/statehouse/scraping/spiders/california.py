"""California — leginfo.

The bill list is a POST-driven search result, but the same rows are reachable
through the per-session "all bills" index, which is stable and does not need a
browser. Detail pages carry the docket in a plain table and the text under a
separate ``billTextClient`` URL.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from statehouse.scraping.spiders.base import PRIORITY_DETAIL, PRIORITY_TEXT, JurisdictionSpider
from statehouse.utils.dates import parse_date
from statehouse.utils.text import clean_text, split_list

__all__ = ["CaliforniaSpider"]

_INDEX = "{base}/faces/billSearchClient.xhtml?session_year={session}&house=Both&author=All"
_TEXT = "{base}/faces/billTextClient.xhtml?bill_id={bill_id}"


class CaliforniaSpider(JurisdictionSpider):
    name = "ca_bills"
    jurisdiction_code = "ca"

    def index_urls(self) -> list[str]:
        return [_INDEX.format(base=self.jurisdiction.portal_url, session=self.session)]

    def parse_index(self, response: Any) -> Iterator[Any]:
        self.stats["index_pages"] += 1
        rows = response.css("table#bill_results tbody tr")
        if not rows:
            self.logger.warning("no rows on index page url=%s", response.url)
        for row in rows:
            href = row.css("td:nth-child(1) a::attr(href)").get()
            if href:
                request = self.follow(response, href, self.parse_detail, kind="detail")
                if request is not None:
                    yield request
        next_page = response.css("a.next-page::attr(href)").get()
        if next_page:
            request = self.follow(
                response, next_page, self.parse_index, kind="index", priority=PRIORITY_DETAIL - 1
            )
            if request is not None:
                yield request

    def parse_detail(self, response: Any) -> Iterator[Any]:
        self.stats["detail_pages"] += 1
        identifier = clean_text(response.css("#bill_header h1::text").get() or "")
        if not identifier:
            self.logger.warning("detail page with no identifier url=%s", response.url)
            return

        item = self.base_item(identifier, response.url)
        item["title"] = clean_text(response.css("#bill_title::text").get() or "")
        item["summary"] = clean_text(" ".join(response.css("#digesttext ::text").getall()))
        item["status"] = clean_text(response.css("#statusTitle::text").get() or "")
        item["introduced_on"] = parse_date(response.css("#introduced_date::text").get())
        item["subjects"] = split_list(response.css("#subject::text").get() or "")
        item["sponsors"] = split_list(response.css("#leadAuthors::text").get() or "")

        for index, row in enumerate(response.css("table#billhistory tbody tr")):
            cells = [clean_text(c) for c in row.css("td::text").getall()]
            if len(cells) < 2:
                continue
            item["actions"].append(
                {
                    "date": cells[0],
                    "description": cells[1],
                    "chamber": cells[2] if len(cells) > 2 else "",
                    "sequence": index,
                }
            )

        bill_id = response.url.split("bill_id=")[-1].split("&")[0]
        if bill_id:
            item["extras"]["bill_id"] = bill_id
            request = self.follow(
                response,
                _TEXT.format(base=self.jurisdiction.portal_url, bill_id=bill_id),
                self.parse_text,
                kind="text",
                priority=PRIORITY_TEXT,
                depth=2,
            )
            if request is not None:
                request.meta["item"] = item
                yield request
                return

        self.stats["documents"] += 1
        yield item

    def parse_text(self, response: Any) -> Iterator[Any]:
        item = response.meta.get("item")
        if item is None:
            return
        body = clean_text(" ".join(response.css("#bill_all ::text").getall()))
        if body:
            item["versions"].append(
                {
                    "label": clean_text(
                        response.css("#version_select option[selected]::text").get() or "current"
                    ),
                    "text": body,
                    "published_on": item.get("introduced_on"),
                }
            )
        self.stats["documents"] += 1
        yield item
