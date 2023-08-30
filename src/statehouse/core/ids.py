"""Stable identifiers.

Every id in the warehouse is derived, never generated. A re-scrape of the same
document has to land on the same primary key or the whole incremental story
falls apart, and that rules out uuid4.
"""

from __future__ import annotations

import hashlib
import re
import uuid

__all__ = [
    "NAMESPACE",
    "document_id",
    "run_id",
    "slugify",
    "canonical_citation",
    "parse_bill_number",
]

#: Fixed UUID5 namespace. Changing it renumbers the warehouse, so don't.
NAMESPACE = uuid.UUID("5f1b6f6a-2d1c-4f2a-9a1a-6b6f2c1d4e88")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_BILL_NUMBER = re.compile(
    r"""^\s*
    (?P<prefix>[A-Za-z]{1,6})      # HB, SB, SJR, ACR, HCR ...
    [\s\.\-_]*
    (?P<number>\d{1,6})
    (?:[\s\.\-_]*(?P<suffix>[A-Za-z]{1,3}))?   # trailing revision letter
    \s*$""",
    re.VERBOSE,
)


def slugify(value: str, *, max_length: int = 80) -> str:
    """Lowercase, hyphen-separated, ASCII-safe slug.

    Empty or punctuation-only input yields ``"unknown"`` rather than an empty
    string, because slugs end up in URLs and object keys.
    """
    folded = _NON_ALNUM.sub("-", value.strip().lower()).strip("-")
    if not folded:
        return "unknown"
    if len(folded) > max_length:
        folded = folded[:max_length].rstrip("-")
    return folded


def parse_bill_number(raw: str) -> tuple[str, int, str] | None:
    """Split a bill designator into ``(prefix, number, suffix)``.

    ``"hb1234"`` and ``"HB 1234-A"`` parse. The prefix is upper-cased and the
    suffix upper-cased or empty. Returns ``None`` when the string is not a bill
    designator at all, which is a normal outcome: portals put resolution titles
    and docket labels in the same column.
    """
    match = _BILL_NUMBER.match(raw or "")
    if not match:
        return None
    prefix = match.group("prefix").upper()
    number = int(match.group("number"))
    suffix = (match.group("suffix") or "").upper()
    return prefix, number, suffix


def canonical_citation(jurisdiction: str, session: str, bill_number: str) -> str:
    """Human-facing citation, e.g. ``"ca-2023-2024-AB1234"``.

    Falls back to a slug of the raw designator when it does not parse, so a
    document is never citation-less.
    """
    parsed = parse_bill_number(bill_number)
    if parsed is None:
        tail = slugify(bill_number, max_length=24).upper()
    else:
        prefix, number, suffix = parsed
        tail = f"{prefix}{number}{suffix}"
    return f"{jurisdiction.lower()}-{slugify(session, max_length=24)}-{tail}"


def document_id(jurisdiction: str, session: str, bill_number: str, *, kind: str = "bill") -> str:
    """Deterministic UUID5 for a document, as a hyphenated string.

    Derived from the canonical citation and the document kind, so the same
    bill observed through two different portal pages collapses to one row.
    """
    name = f"{kind.lower()}::{canonical_citation(jurisdiction, session, bill_number)}"
    return str(uuid.uuid5(NAMESPACE, name))


def run_id(jurisdiction: str, dag_run_id: str, task: str) -> str:
    """Deterministic id for one ingest run.

    Airflow retries a task with the same ``dag_run_id``; deriving the run id
    from it means a retry updates the existing run record instead of opening a
    second one, which is what makes "how many times did this fail" answerable.
    """
    seed = f"{jurisdiction.lower()}|{dag_run_id}|{task}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]
