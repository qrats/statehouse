"""Getting readable text out of a fetched document.

Bill text arrives as HTML, as a PDF, and occasionally as an RTF nobody admits
to producing. The extractor decides which it has from the bytes rather than
the content type header, because several portals label everything
``text/html``.
"""

from __future__ import annotations

import re

from statehouse.core.errors import ParseError
from statehouse.utils.text import clean_text, strip_leading_enumerator, strip_tags

__all__ = ["guess_content_kind", "extract_body_text", "split_sections", "strip_line_numbers"]

_MAGIC = (
    (b"%PDF-", "pdf"),
    (b"{\\rtf", "rtf"),
    (b"PK\x03\x04", "zip"),
    (b"\xd0\xcf\x11\xe0", "doc"),
)

_HTML_HINT = re.compile(rb"(?i)<(?:html|body|div|table|p)\b")
_XML_HINT = re.compile(rb"^\s*<\?xml")

_LINE_NUMBER = re.compile(r"^\s{0,8}\d{1,3}\s{2,}")
_SECTION = re.compile(
    r"(?im)^\s*(SECTION\s+\d+[A-Z]?\.|Sec\.\s*\d+[A-Z]?\.|ARTICLE\s+[IVXLC]+\.?)\s*"
)


def guess_content_kind(payload: bytes) -> str:
    """Classify a payload as ``pdf``, ``rtf``, ``zip``, ``doc``, ``xml``,
    ``html`` or ``text`` from its leading bytes.

    Empty input is ``"text"``; guessing ``"html"`` for nothing produces
    confusing downstream errors.
    """
    head = (payload or b"")[:1024]
    if not head.strip():
        return "text"
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    if _XML_HINT.match(head):
        return "xml"
    if _HTML_HINT.search(head):
        return "html"
    return "text"


def strip_line_numbers(text: str) -> str:
    """Remove the printed line numbers legislative texts carry.

    Only strips a leading number followed by at least two spaces, which is the
    shape every jurisdiction uses; a numbered list item like ``"1. The
    department shall"`` keeps its number.
    """
    return "\n".join(_LINE_NUMBER.sub("", line) for line in (text or "").splitlines())


def extract_body_text(payload: bytes, *, kind: str | None = None) -> str:
    """Return the readable text of a document.

    HTML and XML go through tag stripping; plain text is cleaned; PDF, RTF and
    the binary Office formats raise :class:`~statehouse.core.errors.ParseError`
    because converting them needs a tool this package deliberately does not
    depend on, and the conversion is done by a separate task before the text
    reaches here.
    """
    resolved = kind or guess_content_kind(payload)
    if resolved in ("pdf", "rtf", "doc", "zip"):
        raise ParseError("binary document must be converted before extraction", kind=resolved)
    decoded = (payload or b"").decode("utf-8", errors="replace")
    if resolved in ("html", "xml"):
        decoded = strip_tags(decoded)
    return clean_text(strip_line_numbers(decoded))


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split bill text into ``(heading, body)`` pairs.

    Text before the first heading is returned under the heading
    ``"preamble"``. A document with no recognisable headings comes back as a
    single ``("body", text)`` pair rather than an empty list, so callers never
    have to special-case it.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return []
    matches = list(_SECTION.finditer(cleaned))
    if not matches:
        return [("body", cleaned)]

    sections: list[tuple[str, str]] = []
    preamble = cleaned[: matches[0].start()].strip()
    if preamble:
        sections.append(("preamble", preamble))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        heading = match.group(1).strip().rstrip(".")
        body = strip_leading_enumerator(cleaned[start:end].strip())
        if body:
            sections.append((heading, body))
    return sections
