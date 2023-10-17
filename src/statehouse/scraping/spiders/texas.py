"""Texas — capitol.texas.gov.

Texas splits a bill across four pages (history, text, actions, authors) and
uses a session code rather than a year. Special sessions get their own code
and appear without warning, so the index is discovered rather than assumed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from statehouse.scraping.spiders.base import PRIORITY_TEXT, JurisdictionSpider
from statehouse.utils.text import clean_text, split_list

__all__ = ["TexasSpider", "session_code"]

_FILED = "{base}/Reports/Report.aspx?LegSess={code}&ID=allfiled"
_HISTORY = "{base}/BillLookup/History.aspx?LegSess={code}&Bill={bill}"
_TEXT = "{base}/BillLookup/Text.aspx?LegSess={code}&Bill={bill}"


def session_code(session: str, *, special: int = 0) -> str:
    """Render Texas's session code, e.g. ``"2023-2024"`` -> ``"88R"``.

    The legislature numbers sessions from 1846, one per biennium. A non-zero
    ``special`` produces the special-session suffix instead of ``R``.
    """
    start = int(str(session).split("-")[0])
    number = (start - 1846) // 2 + 1
    suffix = "R" if special == 0 else str(special)
    return f"{number}{suffix}"


class TexasSpider(JurisdictionSpider):
    name = "tx_bills"
    jurisdiction_code = "tx"

    @property
    def code(self) -> str:
        return session_code(self.session)

    def index_urls(self) -> list[str]:
        return [_FILED.format(base=self.jurisdiction.portal_url, code=self.code)]

    def parse_index(self, response: Any) -> Iterator[Any]:
        self.stats["index_pages"] += 1
        for link in response.css("table#tblBills tbody tr td:first-child a"):
            bill = clean_text(link.css("::text").get() or "")
            if not bill:
                continue
            url = _HISTORY.format(
                base=self.jurisdiction.portal_url, code=self.code, bill=bill.replace(" ", "")
            )
            request = self.follow(response, url, self.parse_detail, kind="detail")
            if request is not None:
                request.meta["identifier"] = bill
                yield request

    def parse_detail(self, response: Any) -> Iterator[Any]:
        self.stats["detail_pages"] += 1
        identifier = response.meta.get("identifier") or clean_text(
            response.css("#usrBillInfoTabs_lblBill::text").get() or ""
        )
        if not identifier:
            return

        item = self.base_item(identifier, response.url)
        item["title"] = clean_text(response.css("#cellCaptionText::text").get() or "")
        item["sponsors"] = split_list(response.css("#cellAuthors::text").get() or "")
        item["subjects"] = split_list(" ".join(response.css("#cellSubjects::text").getall()))

        for index, row in enumerate(response.css("table#usrBillInfoActions_tblActions tbody tr")):
            cells = [clean_text(c) for c in row.css("td::text").getall()]
            if len(cells) < 3:
                continue
            item["actions"].append(
                {
                    "chamber": cells[0],
                    "description": cells[1],
                    "date": cells[2],
                    "sequence": index,
                }
            )

        text_url = _TEXT.format(
            base=self.jurisdiction.portal_url,
            code=self.code,
            bill=identifier.replace(" ", ""),
        )
        request = self.follow(
            response, text_url, self.parse_text, kind="text", priority=PRIORITY_TEXT, depth=2
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
        for row in response.css("table#usrBillInfoTexts_tblBillTexts tbody tr"):
            label = clean_text(row.css("td:first-child::text").get() or "")
            body = clean_text(" ".join(row.css("td:nth-child(2) ::text").getall()))
            if label and body:
                item["versions"].append({"label": label, "text": body})
        self.stats["documents"] += 1
        yield item
