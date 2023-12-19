"""Federal — congress.gov bulk data.

Congress publishes daily bulk XML, which is far better than scraping the site:
complete, versioned and rate-limit free. This adapter walks the bulk index and
parses the per-bill XML; the HTML site is only consulted for rendered text
where the XML omits it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from xml.etree import ElementTree

from statehouse.core.errors import ParseError
from statehouse.scraping.spiders.base import JurisdictionSpider
from statehouse.utils.text import clean_text

__all__ = ["FederalSpider", "parse_bill_xml"]

_BULK = "{base}/bulkdata/BILLSTATUS/{congress}/{chamber}"


def _text(node: Any, path: str, default: str = "") -> str:
    found = node.find(path) if node is not None else None
    if found is None or found.text is None:
        return default
    return clean_text(found.text, keep_paragraphs=False)


def parse_bill_xml(payload: str | bytes) -> dict[str, Any]:
    """Parse one BILLSTATUS document into a loose record.

    Raises :class:`~statehouse.core.errors.ParseError` when the payload is not
    a bill-status document — the bulk index also lists manifests and checksum
    files, and confusing one for a bill produces a very confusing row.
    """
    try:
        root = ElementTree.fromstring(
            payload if isinstance(payload, str) else payload.decode("utf-8")
        )
    except ElementTree.ParseError as exc:
        raise ParseError("bill status payload is not XML") from exc

    bill = root.find("bill") if root.tag != "bill" else root
    if bill is None:
        raise ParseError("bill status payload has no bill element", root=root.tag)

    number = _text(bill, "billNumber") or _text(bill, "number")
    bill_type = _text(bill, "billType") or _text(bill, "type")
    if not number or not bill_type:
        raise ParseError("bill status payload has no identifier")

    actions: list[dict[str, Any]] = []
    for index, node in enumerate(bill.findall("./actions/item")):
        actions.append(
            {
                "date": _text(node, "actionDate"),
                "description": _text(node, "text"),
                "chamber": _text(node, "chamber"),
                "sequence": index,
            }
        )

    sponsors = [
        clean_text(_text(node, "fullName"), keep_paragraphs=False)
        for node in bill.findall("./sponsors/item")
    ]
    cosponsors = [
        clean_text(_text(node, "fullName"), keep_paragraphs=False)
        for node in bill.findall("./cosponsors/item")
    ]
    subjects = [
        _text(node, "name")
        for node in bill.findall("./subjects/billSubjects/legislativeSubjects/item")
    ]

    return {
        "identifier": f"{bill_type.upper()}{number}",
        "title": _text(bill, "title"),
        "summary": clean_text(
            " ".join(_text(node, "text") for node in bill.findall("./summaries/billSummaries/item"))
        ),
        "introduced_on": _text(bill, "introducedDate"),
        "status": _text(bill, "./latestAction/text"),
        "chamber": "upper" if bill_type.lower().startswith("s") else "lower",
        "sponsors": [s for s in sponsors + cosponsors if s],
        "subjects": [s for s in subjects if s],
        "actions": actions,
        "versions": [],
    }


class FederalSpider(JurisdictionSpider):
    name = "us_bills"
    jurisdiction_code = "us"

    def congress_number(self) -> int:
        """Congress number for the configured biennium (117th = 2021-2022)."""
        start = int(str(self.session).split("-")[0])
        return (start - 1789) // 2 + 1

    def index_urls(self) -> list[str]:
        congress = self.congress_number()
        return [
            _BULK.format(base=self.jurisdiction.portal_url, congress=congress, chamber=chamber)
            for chamber in ("hr", "s", "hjres", "sjres", "hconres", "sconres")
        ]

    def parse_index(self, response: Any) -> Iterator[Any]:
        self.stats["index_pages"] += 1
        for href in response.css("a::attr(href)").getall():
            if not href.lower().endswith(".xml"):
                continue
            request = self.follow(response, href, self.parse_detail, kind="detail")
            if request is not None:
                yield request

    def parse_detail(self, response: Any) -> Iterator[Any]:
        self.stats["detail_pages"] += 1
        record = parse_bill_xml(response.body)
        item = self.base_item(record["identifier"], response.url)
        item.update({k: v for k, v in record.items() if k != "identifier"})
        self.stats["documents"] += 1
        yield item
