"""Text cleaning for legislative source material.

Portals emit HTML written by hand in 2004, PDFs converted by four different
tools, and the occasional Word export. The helpers here are the ones that
earned their place by being needed in more than one adapter.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable, Iterator

__all__ = [
    "collapse_whitespace",
    "strip_tags",
    "clean_text",
    "titlecase_name",
    "split_list",
    "truncate",
    "dedent_block",
    "iter_sentences",
    "strip_leading_enumerator",
    "normalise_quotes",
]

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_OR_STYLE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_BLOCK_BOUNDARY = re.compile(r"(?i)</?(p|div|br|li|tr|h[1-6]|table|section)\b[^>]*>")
_WS = re.compile(r"[ \t   ]+")
_BLANKS = re.compile(r"\n{3,}")
_ENUMERATOR = re.compile(
    r"^\s*(?:\(?[a-zA-Z0-9ivxIVX]{1,5}[\)\.]|SECTION\s+\d+\.|Sec\.\s*\d+\.)\s+"
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")

_QUOTE_MAP = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    " ": " ",
}

_LOWER_PARTICLES = {"de", "del", "der", "van", "von", "da", "di", "la", "le", "du", "of", "the"}
_ALWAYS_UPPER = {"ii", "iii", "iv", "jr", "sr", "md", "phd", "dds", "cpa", "usa", "us"}


def normalise_quotes(value: str) -> str:
    """Fold typographic punctuation to ASCII equivalents."""
    return "".join(_QUOTE_MAP.get(ch, ch) for ch in value)


def collapse_whitespace(value: str, *, keep_paragraphs: bool = False) -> str:
    """Collapse runs of spaces; optionally keep paragraph breaks.

    With ``keep_paragraphs`` the result retains single newlines between
    paragraphs and never more than one blank line, which is what the text
    extractors want before they hand a document to the differ.
    """
    if value is None:
        return ""
    text = _WS.sub(" ", value)
    if not keep_paragraphs:
        return " ".join(text.split())
    lines = [line.strip() for line in text.splitlines()]
    joined = "\n".join(lines)
    return _BLANKS.sub("\n\n", joined).strip()


def strip_tags(markup: str) -> str:
    """Remove HTML markup, preserving block boundaries as newlines.

    Not a parser and not trying to be: this runs on fragments that have
    already been selected out of a page, where pulling in lxml for a table
    cell is not worth the import.
    """
    if not markup:
        return ""
    text = _SCRIPT_OR_STYLE.sub(" ", markup)
    text = _BLOCK_BOUNDARY.sub("\n", text)
    text = _TAG.sub(" ", text)
    return collapse_whitespace(html.unescape(text), keep_paragraphs=True)


def clean_text(value: str, *, keep_paragraphs: bool = True) -> str:
    """The standard cleanup applied to anything destined for the warehouse."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = normalise_quotes(text)
    text = text.replace("­", "").replace("\r\n", "\n").replace("\r", "\n")
    return collapse_whitespace(text, keep_paragraphs=keep_paragraphs)


def titlecase_name(value: str) -> str:
    """Title-case a person's name without mangling the usual exceptions.

    ``"MCDONALD, JAMES P."`` becomes ``"McDonald, James P."``; particles stay
    lowercase unless they lead; suffixes and initials stay upper.
    """
    cleaned = clean_text(value, keep_paragraphs=False)
    if not cleaned:
        return ""
    out: list[str] = []
    for index, word in enumerate(cleaned.split(" ")):
        bare = word.strip(",.")
        trailing = word[len(bare) :]
        lowered = bare.lower()
        if not bare:
            out.append(word)
        elif lowered in _ALWAYS_UPPER:
            out.append(bare.upper() + trailing)
        elif lowered in _LOWER_PARTICLES and index > 0:
            out.append(lowered + trailing)
        elif len(bare) <= 2 and bare.isalpha() and bare.isupper():
            out.append(bare + trailing)
        elif lowered.startswith("mc") and len(bare) > 2:
            out.append("Mc" + bare[2:].capitalize() + trailing)
        elif lowered.startswith("o'") and len(bare) > 2:
            out.append("O'" + bare[2:].capitalize() + trailing)
        elif "-" in bare:
            out.append("-".join(part.capitalize() for part in bare.split("-")) + trailing)
        else:
            out.append(bare.capitalize() + trailing)
    return " ".join(out)


def split_list(value: str, *, separators: Iterable[str] = (";", ",", "|", "\n")) -> list[str]:
    """Split a delimited cell into trimmed, non-empty, de-duplicated parts.

    Order is preserved — sponsor order carries meaning.
    """
    if not value:
        return []
    text = value
    for sep in separators:
        text = text.replace(sep, "\x1f")
    seen: set[str] = set()
    parts: list[str] = []
    for chunk in text.split("\x1f"):
        cleaned = collapse_whitespace(chunk)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            parts.append(cleaned)
    return parts


def truncate(value: str, limit: int, *, suffix: str = "…") -> str:
    """Trim to ``limit`` characters on a word boundary where possible."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(value) <= limit:
        return value
    window = value[: limit - len(suffix)]
    space = window.rfind(" ")
    if space > limit // 2:
        window = window[:space]
    return window.rstrip() + suffix


def dedent_block(value: str) -> str:
    """Remove the common leading indentation from a block of text."""
    lines = value.splitlines()
    widths = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    if not widths:
        return value.strip()
    cut = min(widths)
    return "\n".join(line[cut:] if line.strip() else "" for line in lines).strip("\n")


def iter_sentences(value: str) -> Iterator[str]:
    """Yield sentences from cleaned prose. Good enough for summaries."""
    for chunk in _SENTENCE.split(collapse_whitespace(value)):
        candidate = chunk.strip()
        if candidate:
            yield candidate


def strip_leading_enumerator(line: str) -> str:
    """Drop a leading ``(a)``, ``1.``, ``SECTION 3.`` and friends."""
    return _ENUMERATOR.sub("", line, count=1).strip()
