"""Content fingerprints.

Two jobs here, and they are not the same job:

``content_digest`` answers "are these the same bytes?" and is used to skip
re-processing an unchanged fetch.

``semantic_digest`` answers "is this the same document to a reader?" and is
used for change detection. It ignores whitespace runs, page furniture and
anything the source regenerates on every render (timestamps, session ids,
"printed on" lines), because portals routinely serve byte-different HTML for a
document nobody has touched in a year.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping

__all__ = [
    "content_digest",
    "semantic_digest",
    "field_digest",
    "normalise_for_digest",
    "short_digest",
    "VOLATILE_PATTERNS",
]

#: Fragments that change on every render and must not count as a document edit.
VOLATILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bprinted\s+on\b[^\n]{0,60}"),
    re.compile(r"(?i)\bgenerated\s+(?:at|on)\b[^\n]{0,60}"),
    re.compile(r"(?i)\blast\s+updated\b[^\n]{0,60}"),
    re.compile(r"(?i)\bretrieved\s+on\b[^\n]{0,60}"),
    re.compile(r"(?i)\bsession[_\- ]?id\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bcsrf[_\- ]?token\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bpage\s+\d+\s+of\s+\d+"),
    re.compile(r"\b[0-9a-fA-F]{32,64}\b"),
)

_WHITESPACE = re.compile(r"\s+")
_SOFT_HYPHEN = "­"


def _coerce(payload: str | bytes) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def content_digest(payload: str | bytes) -> str:
    """SHA-256 of the exact bytes, hex encoded.

    Text is encoded UTF-8 first so that a str and its UTF-8 bytes agree.
    """
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def normalise_for_digest(payload: str | bytes) -> str:
    """Reduce a document to the part a reader would call its content.

    Unicode is NFKC folded, soft hyphens dropped, every volatile fragment
    removed, whitespace runs collapsed to a single space, and the result
    lowercased and stripped. The output is deliberately lossy: it exists to be
    hashed, not read.
    """
    text = unicodedata.normalize("NFKC", _coerce(payload))
    text = text.replace(_SOFT_HYPHEN, "")
    for pattern in VOLATILE_PATTERNS:
        text = pattern.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip().lower()


def semantic_digest(payload: str | bytes) -> str:
    """SHA-256 over :func:`normalise_for_digest`.

    Stable across re-renders of an unchanged document; changes as soon as a
    word does.
    """
    return hashlib.sha256(normalise_for_digest(payload).encode("utf-8")).hexdigest()


def field_digest(values: Mapping[str, object], fields: Iterable[str] | None = None) -> str:
    """Digest a subset of a record's fields, order-independently.

    Used to detect metadata-only edits: a title correction or a newly attached
    committee should register as a change even when the bill text is untouched.
    ``None`` and the empty string are treated as the same absent value so that
    a source switching between them does not manufacture a diff.
    """
    keys = sorted(fields) if fields is not None else sorted(values)
    parts: list[str] = []
    for key in keys:
        raw = values.get(key)
        if raw is None or raw == "":
            rendered = ""
        elif isinstance(raw, (list, tuple)):
            rendered = "|".join(sorted(str(item) for item in raw))
        else:
            rendered = str(raw)
        parts.append(f"{key}={_WHITESPACE.sub(' ', rendered).strip()}")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def short_digest(digest: str, length: int = 12) -> str:
    """First ``length`` characters of a digest, for logs and run labels."""
    if length <= 0:
        raise ValueError("length must be positive")
    return digest[:length]
