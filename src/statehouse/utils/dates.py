"""Date parsing for portal output.

Every jurisdiction writes dates differently, several write them ambiguously,
and a handful write them wrong. Parsing returns ``None`` rather than raising
for unrecognised input: a missing action date is a quality finding, not a
crashed spider.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from statehouse.core.clock import UTC, ensure_utc

__all__ = [
    "parse_date",
    "parse_datetime",
    "day_range",
    "chunk_range",
    "session_years",
    "clamp_date",
    "MONTHS",
]

MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_US = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$")
_LONG = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")
_LONG_REVERSED = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_TIME = re.compile(r"[T ](\d{1,2}):(\d{2})(?::(\d{2}))?")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _expand_year(value: int) -> int:
    """Two-digit years: 70-99 are 19xx, everything else 20xx."""
    if value >= 100:
        return value
    return 1900 + value if value >= 70 else 2000 + value


def parse_date(raw: object) -> date | None:
    """Parse a date out of whatever a portal produced.

    Understands ISO, US ``m/d/y``, ``YYYYMMDD``, ``January 5, 2024`` and
    ``5 January 2024``. Numeric ``d/m/y`` is *not* attempted: no source in the
    registry uses it, and guessing would silently corrupt February dates.
    Returns ``None`` for anything else, including the empty string.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = " ".join(str(raw).split())
    if not text:
        return None
    text = text.split(" at ")[0].strip().rstrip(".")
    # Strip a trailing time component if one came along.
    time_match = _TIME.search(text)
    if time_match:
        text = text[: time_match.start()].strip()

    match = _ISO.match(text)
    if match:
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = _COMPACT.match(text)
    if match:
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = _US.match(text)
    if match:
        return _safe_date(
            _expand_year(int(match.group(3))), int(match.group(1)), int(match.group(2))
        )
    match = _LONG.match(text)
    if match:
        month = MONTHS.get(match.group(1).lower())
        if month:
            return _safe_date(int(match.group(3)), month, int(match.group(2)))
    match = _LONG_REVERSED.match(text)
    if match:
        month = MONTHS.get(match.group(2).lower())
        if month:
            return _safe_date(int(match.group(3)), month, int(match.group(1)))
    return None


def parse_datetime(raw: object) -> datetime | None:
    """Parse a timestamp, returning an aware UTC datetime.

    A value with no time component becomes midnight UTC on that date.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return ensure_utc(raw)
    text = " ".join(str(raw).split())
    if not text:
        return None
    normalised = text.replace("Z", "+00:00")
    try:
        return ensure_utc(datetime.fromisoformat(normalised))
    except ValueError:
        pass
    day = parse_date(text)
    if day is None:
        return None
    time_match = _TIME.search(text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        second = int(time_match.group(3) or 0)
        if 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60:
            return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def day_range(start: date, end: date) -> list[date]:
    """Inclusive list of days from ``start`` to ``end``.

    An inverted range yields an empty list rather than raising: backfill
    planners hand this whatever the watermark says, and an empty plan is the
    right answer for "nothing to do".
    """
    if end < start:
        return []
    span = (end - start).days
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def chunk_range(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Split an inclusive range into consecutive chunks of at most ``chunk_days``.

    The final chunk is short rather than overshooting ``end``. Raises
    ``ValueError`` for a non-positive chunk size; an inverted range is empty.
    """
    if chunk_days <= 0:
        raise ValueError("chunk_days must be positive")
    if end < start:
        return []
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=chunk_days - 1), end)
        chunks.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return chunks


def session_years(start_year: int, end_year: int, *, biennial: bool = False) -> list[int]:
    """Years to iterate when backfilling.

    Biennial legislatures are anchored on odd years, so a biennial range
    starting in an even year begins at the odd year before it.
    """
    if end_year < start_year:
        return []
    if not biennial:
        return list(range(start_year, end_year + 1))
    first = start_year if start_year % 2 == 1 else start_year - 1
    return [year for year in range(first, end_year + 1, 2)]


def clamp_date(value: date, lower: date | None = None, upper: date | None = None) -> date:
    """Clamp ``value`` into ``[lower, upper]``, ignoring bounds left as ``None``."""
    if lower is not None and value < lower:
        return lower
    if upper is not None and value > upper:
        return upper
    return value
