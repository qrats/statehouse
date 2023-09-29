"""Collapsing duplicate observations.

One crawl reaches the same bill through the chamber index, the subject index
and the sponsor page. All three land in the same batch, each with a slightly
different amount of detail, and exactly one row has to come out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from statehouse.core.models import Action, Document
from statehouse.transform.sponsors import merge_sponsors

__all__ = ["merge_documents", "dedupe_batch", "merge_actions", "completeness_score"]


def completeness_score(document: Document) -> tuple[int, ...]:
    """How much a given observation knows, as a comparable tuple.

    Used to pick which of two observations leads a merge. Deliberately coarse:
    the point is to prefer the detail page over the index row, not to rank two
    detail pages against each other.
    """
    return (
        len(document.versions),
        len(document.actions),
        len(document.sponsors),
        len(document.subjects),
        1 if document.summary else 0,
        len(document.title),
    )


def merge_actions(left: Iterable[Action], right: Iterable[Action]) -> list[Action]:
    """Union two dockets on ``(date, lowercased description)``.

    Where both sides carry the same line, the one with the more specific
    chamber or a named committee wins. Result is in docket order.
    """
    merged: dict[tuple[object, str], Action] = {}
    for action in list(left) + list(right):
        key = (action.occurred_on, action.description.lower())
        current = merged.get(key)
        if current is None:
            merged[key] = action
            continue
        better_chamber = current.chamber.value == "unknown" and action.chamber.value != "unknown"
        better_committee = current.committee is None and action.committee is not None
        if better_chamber or better_committee:
            merged[key] = action
    return sorted(merged.values(), key=Action.sort_key)


def merge_documents(primary: Document, secondary: Document) -> Document:
    """Merge ``secondary`` into ``primary``, preferring ``primary``'s values.

    Blank fields on the primary are filled from the secondary; lists are
    unioned. Both must describe the same document id — merging across ids is a
    programming error, not a data condition, so it raises ``ValueError``.
    """
    if primary.document_id != secondary.document_id:
        raise ValueError("cannot merge documents with different ids")

    versions = list(primary.versions)
    seen_hashes = {version.semantic_hash for version in versions}
    for version in secondary.versions:
        if version.semantic_hash not in seen_hashes:
            seen_hashes.add(version.semantic_hash)
            versions.append(version)

    actions = merge_actions(primary.actions, secondary.actions)
    introduced = primary.introduced_on or secondary.introduced_on
    if actions:
        earliest = min(action.occurred_on for action in actions)
        introduced = min(introduced, earliest) if introduced else earliest

    last_action = max((a.occurred_on for a in actions), default=None) or (
        primary.last_action_on or secondary.last_action_on
    )

    return Document(
        jurisdiction=primary.jurisdiction,
        session=primary.session,
        identifier=primary.identifier,
        title=primary.title or secondary.title,
        kind=primary.kind if primary.kind.value != "unknown" else secondary.kind,
        chamber=primary.chamber if primary.chamber.value != "unknown" else secondary.chamber,
        status=primary.status if primary.status.value != "unknown" else secondary.status,
        introduced_on=introduced,
        last_action_on=last_action,
        summary=primary.summary or secondary.summary,
        subjects=sorted(set(primary.subjects) | set(secondary.subjects)),
        sponsors=merge_sponsors(primary.sponsors, secondary.sponsors),
        actions=actions,
        versions=versions,
        source=primary.source or secondary.source,
        observed_at=max(
            [d for d in (primary.observed_at, secondary.observed_at) if d is not None],
            default=None,
        ),
        extras={**secondary.extras, **primary.extras},
    )


def dedupe_batch(documents: Sequence[Document]) -> list[Document]:
    """Collapse a batch to one document per id.

    The most complete observation leads each merge; ties go to the one that
    appeared first, so the result is deterministic for a given input order.
    Output is sorted by document id.
    """
    grouped: dict[str, Document] = {}
    for document in documents:
        key = document.document_id
        current = grouped.get(key)
        if current is None:
            grouped[key] = document
            continue
        if completeness_score(document) > completeness_score(current):
            grouped[key] = merge_documents(document, current)
        else:
            grouped[key] = merge_documents(current, document)
    return [grouped[key] for key in sorted(grouped)]
