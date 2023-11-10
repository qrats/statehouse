"""HTML table extraction.

Half the dockets in the registry are tables, several of them written before
``<thead>`` existed, and a few of them nested. This is a deliberately small
regex-based reader rather than a DOM parse: it runs on fragments the spider
already selected, and it has to survive unclosed tags, which lxml's recovery
handles by silently restructuring the table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from statehouse.utils.text import strip_tags

__all__ = ["HtmlTable", "parse_tables", "first_table_with_headers"]

_TABLE = re.compile(r"(?is)<table\b[^>]*>(.*?)</table>")
_ROW = re.compile(r"(?is)<tr\b[^>]*>(.*?)(?=<tr\b|</table>|$)")
_CELL = re.compile(r"(?is)<(t[hd])\b([^>]*)>(.*?)(?=<t[hd]\b|</tr>|$)")
_COLSPAN = re.compile(r"(?i)colspan\s*=\s*[\"']?(\d+)")


@dataclass
class HtmlTable:
    """A table reduced to header names and text rows."""

    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)

    @property
    def width(self) -> int:
        return max([len(self.headers)] + [len(row) for row in self.rows], default=0)

    def __len__(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> list[str]:
        """Every value under ``name``, matched case-insensitively.

        Returns an empty list for an unknown header rather than raising:
        portals rename columns without warning and the caller decides whether
        that is fatal.
        """
        wanted = name.strip().lower()
        try:
            index = [h.lower() for h in self.headers].index(wanted)
        except ValueError:
            return []
        return [row[index] if index < len(row) else "" for row in self.rows]

    def records(self) -> list[dict[str, str]]:
        """Rows as dictionaries keyed on the headers.

        Cells beyond the header count are collected under ``"_extra"`` joined
        by a space, so a stray column never silently drops data.
        """
        out: list[dict[str, str]] = []
        for row in self.rows:
            record: dict[str, str] = {}
            for index, header in enumerate(self.headers):
                record[header] = row[index] if index < len(row) else ""
            if len(row) > len(self.headers):
                record["_extra"] = " ".join(row[len(self.headers) :])
            out.append(record)
        return out


def _cells(row_html: str) -> tuple[list[str], bool]:
    values: list[str] = []
    header_row = False
    for tag, attrs, body in _CELL.findall(row_html):
        text = strip_tags(body).replace("\n", " ").strip()
        span = _COLSPAN.search(attrs)
        values.append(text)
        if span:
            values.extend([""] * max(0, int(span.group(1)) - 1))
        if tag.lower() == "th":
            header_row = True
    return values, header_row


def parse_tables(markup: str) -> list[HtmlTable]:
    """Extract every top-level table in ``markup``.

    The first row is treated as headers when it uses ``<th>``; otherwise the
    table has no headers and every row is data. Empty rows are dropped.
    """
    tables: list[HtmlTable] = []
    for body in _TABLE.findall(markup or ""):
        table = HtmlTable()
        for row_html in _ROW.findall(body):
            values, is_header = _cells(row_html)
            if not any(value for value in values):
                continue
            if is_header and not table.headers and not table.rows:
                table.headers = values
            else:
                table.rows.append(values)
        if table.headers or table.rows:
            tables.append(table)
    return tables


def first_table_with_headers(markup: str, required: list[str]) -> HtmlTable | None:
    """The first table carrying every header in ``required``.

    Matching is case-insensitive and by containment, so ``"date"`` finds
    ``"Action Date"``. Returns ``None`` when nothing matches.
    """
    wanted = [name.strip().lower() for name in required if name.strip()]
    if not wanted:
        return None
    for table in parse_tables(markup):
        available = [header.lower() for header in table.headers]
        if all(any(name in header for header in available) for name in wanted):
            return table
    return None
