"""Portal record -> canonical :class:`Document`.

Spiders emit loose dictionaries because that is what a scraped page is. This
module is the single place where a loose dictionary becomes something the
warehouse will accept, and where every "the source did something odd" decision
is made once instead of per adapter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from statehouse.core.clock import Clock, utc_now
from statehouse.core.enums import BillStatus, Chamber, DocumentKind, FetchMethod
from statehouse.core.errors import ValidationError
from statehouse.core.models import Action, Document, DocumentVersion, SourceRef
from statehouse.transform.sponsors import parse_sponsor_list
from statehouse.transform.status import classify_action, derive_status, status_from_action
from statehouse.transform.subjects import normalise_subjects
from statehouse.utils.dates import parse_date
from statehouse.utils.text import clean_text, truncate
from statehouse.utils.urls import canonical_url

__all__ = ["normalise_document", "normalise_action", "normalise_versions", "infer_chamber"]

_CHAMBER_BY_PREFIX = {
    "H": Chamber.LOWER,
    "A": Chamber.LOWER,
    "AB": Chamber.LOWER,
    "HB": Chamber.LOWER,
    "HR": Chamber.LOWER,
    "HJR": Chamber.LOWER,
    "HCR": Chamber.LOWER,
    "S": Chamber.UPPER,
    "SB": Chamber.UPPER,
    "SR": Chamber.UPPER,
    "SJR": Chamber.UPPER,
    "SCR": Chamber.UPPER,
    "SF": Chamber.UPPER,
    "HF": Chamber.LOWER,
}

_KIND_BY_PREFIX = {
    "HR": DocumentKind.RESOLUTION,
    "SR": DocumentKind.RESOLUTION,
    "HJR": DocumentKind.RESOLUTION,
    "SJR": DocumentKind.RESOLUTION,
    "HCR": DocumentKind.RESOLUTION,
    "SCR": DocumentKind.RESOLUTION,
    "ACR": DocumentKind.RESOLUTION,
}

_SUMMARY_LIMIT = 2000


def infer_chamber(identifier: str, *, default: Chamber = Chamber.UNKNOWN) -> Chamber:
    """Guess the originating chamber from a bill designator.

    Every jurisdiction in the registry prefixes with H/A for the lower body
    and S for the upper. Ambiguous or unprefixed designators return ``default``.
    """
    letters = "".join(ch for ch in (identifier or "").upper() if ch.isalpha())
    if not letters:
        return default
    for width in (3, 2, 1):
        candidate = letters[:width]
        if candidate in _CHAMBER_BY_PREFIX:
            return _CHAMBER_BY_PREFIX[candidate]
    return default


def _infer_kind(identifier: str, declared: object, default: DocumentKind) -> DocumentKind:
    if declared:
        return DocumentKind.parse(declared, default)
    letters = "".join(ch for ch in (identifier or "").upper() if ch.isalpha())
    for width in (3, 2):
        if letters[:width] in _KIND_BY_PREFIX:
            return _KIND_BY_PREFIX[letters[:width]]
    return default


def normalise_action(raw: Mapping[str, Any], *, sequence: int = 0) -> Action | None:
    """Build an :class:`Action` from a scraped docket row.

    A row with no parseable date is dropped: an undated action cannot be
    ordered, and an unordered docket produces a wrong status, which is worse
    than a short one. The caller counts drops and raises a finding.
    """
    occurred = parse_date(raw.get("date") or raw.get("occurred_on") or raw.get("action_date"))
    if occurred is None:
        return None
    description = clean_text(
        str(raw.get("description") or raw.get("action") or raw.get("text") or ""),
        keep_paragraphs=False,
    )
    if not description:
        return None
    chamber = Chamber.parse(raw.get("chamber"), Chamber.UNKNOWN)
    return Action(
        occurred_on=occurred,
        description=description,
        kind=classify_action(description),
        chamber=chamber,
        committee=clean_text(str(raw.get("committee") or ""), keep_paragraphs=False) or None,
        resulting_status=status_from_action(description, chamber=chamber),
        sequence=int(raw.get("sequence", sequence) or sequence),
    )


def normalise_versions(
    raw_versions: Sequence[Mapping[str, Any]] | None,
    *,
    source: SourceRef | None = None,
) -> list[DocumentVersion]:
    """Build the version list, dropping entries with no text.

    Versions are returned in published-date order with undated ones last, in
    the order the source listed them.
    """
    built: list[tuple[int, DocumentVersion]] = []
    for index, entry in enumerate(raw_versions or []):
        text = entry.get("text")
        if text is None:
            continue
        cleaned = clean_text(str(text))
        if not cleaned:
            continue
        built.append(
            (
                index,
                DocumentVersion(
                    label=str(entry.get("label") or entry.get("name") or f"version {index + 1}"),
                    text=cleaned,
                    published_on=parse_date(entry.get("published_on") or entry.get("date")),
                    source=source,
                ),
            )
        )
    dated = sorted(
        (item for item in built if item[1].published_on is not None),
        key=lambda item: (item[1].published_on, item[0]),  # type: ignore[arg-type,return-value]
    )
    undated = [item for item in built if item[1].published_on is None]
    return [version for _index, version in dated] + [version for _index, version in undated]


def normalise_document(
    raw: Mapping[str, Any],
    *,
    jurisdiction: str,
    session: str,
    spider: str | None = None,
    clock: Clock | None = None,
) -> Document:
    """Turn one scraped record into a canonical :class:`Document`.

    ``raw`` must carry an ``identifier``; everything else is optional and
    filled in as far as the source allows. The status is *derived from the
    docket* and only falls back to the source's own status field when the
    docket implies nothing, because portal status fields go stale.

    Raises :class:`~statehouse.core.errors.ValidationError` when the record has
    no usable identifier.
    """
    identifier = clean_text(str(raw.get("identifier") or raw.get("bill_number") or ""), keep_paragraphs=False)
    if not identifier:
        raise ValidationError("record has no identifier", jurisdiction=jurisdiction)

    url = canonical_url(str(raw.get("url") or raw.get("source_url") or ""))
    source = (
        SourceRef(
            jurisdiction=jurisdiction,
            url=url,
            method=FetchMethod.parse(raw.get("fetch_method"), FetchMethod.HTTP),
            fetched_at=utc_now(clock),
            http_status=raw.get("http_status"),
            spider=spider,
        )
        if url
        else None
    )

    chamber = Chamber.parse(raw.get("chamber"), Chamber.UNKNOWN)
    if chamber is Chamber.UNKNOWN:
        chamber = infer_chamber(identifier)

    actions: list[Action] = []
    for index, row in enumerate(raw.get("actions") or []):
        action = normalise_action(row, sequence=index)
        if action is not None:
            actions.append(action)

    declared_status = BillStatus.parse(raw.get("status"), BillStatus.UNKNOWN)
    status = derive_status(actions, origin=chamber, fallback=declared_status)

    subjects, _unmapped = normalise_subjects(raw.get("subjects") or [])

    introduced = parse_date(raw.get("introduced_on") or raw.get("introduced"))
    if introduced is None and actions:
        introduced = min(action.occurred_on for action in actions)
    last_action: date | None = max((a.occurred_on for a in actions), default=None)
    if last_action is None:
        last_action = parse_date(raw.get("last_action_on"))
    if introduced and last_action and last_action < introduced:
        introduced = last_action

    return Document(
        jurisdiction=jurisdiction,
        session=session,
        identifier=identifier,
        title=clean_text(str(raw.get("title") or ""), keep_paragraphs=False),
        kind=_infer_kind(identifier, raw.get("kind"), DocumentKind.BILL),
        chamber=chamber,
        status=status,
        introduced_on=introduced,
        last_action_on=last_action,
        summary=truncate(clean_text(str(raw.get("summary") or "")), _SUMMARY_LIMIT),
        subjects=subjects,
        sponsors=parse_sponsor_list(raw.get("sponsors") or [], default_chamber=chamber),
        actions=actions,
        versions=normalise_versions(raw.get("versions"), source=source),
        source=source,
        observed_at=utc_now(clock),
        extras={k: v for k, v in (raw.get("extras") or {}).items()},
    )
