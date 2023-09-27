"""Change detection between two observations of the same document.

Downstream consumers subscribe to a change feed, so this is the layer that
decides whether an alert fires. Getting it wrong is expensive in both
directions: a false positive wakes a lobbyist at 6am over a whitespace edit, a
false negative means a substitute amendment goes unnoticed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from statehouse.core.enums import BillStatus, ChangeKind
from statehouse.core.models import Document
from statehouse.transform.status import TERMINAL_STATUSES, status_rank

__all__ = [
    "DocumentDiff",
    "diff_documents",
    "text_similarity",
    "changed_fields",
]

_TRACKED_FIELDS = (
    "title",
    "summary",
    "status",
    "chamber",
    "kind",
    "introduced_on",
    "last_action_on",
    "subjects",
    "sponsors",
)


@dataclass
class DocumentDiff:
    """What changed between a previous and a current observation."""

    document_id: str
    kind: ChangeKind
    fields: list[str] = field(default_factory=list)
    previous_status: BillStatus | None = None
    current_status: BillStatus | None = None
    text_similarity: float | None = None
    new_actions: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def is_change(self) -> bool:
        return self.kind is not ChangeKind.UNCHANGED

    @property
    def notable(self) -> bool:
        """True for changes worth pushing to a subscriber.

        Metadata-only edits are recorded but not pushed; everything else is.
        """
        return self.kind not in (ChangeKind.UNCHANGED, ChangeKind.METADATA_ONLY)

    def to_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "kind": self.kind.value,
            "fields": list(self.fields),
            "previous_status": self.previous_status.value if self.previous_status else None,
            "current_status": self.current_status.value if self.current_status else None,
            "text_similarity": self.text_similarity,
            "new_actions": self.new_actions,
            "notes": list(self.notes),
        }


def _field_value(document: Document, name: str) -> object:
    value = getattr(document, name)
    if name == "sponsors":
        return tuple(sorted(f"{s.role.value}:{s.name.lower()}" for s in value))
    if name == "subjects":
        return tuple(sorted(s.lower() for s in value))
    if hasattr(value, "value"):
        return value.value
    if value is None:
        return None
    return value


def changed_fields(previous: Document, current: Document) -> list[str]:
    """Names of the tracked fields whose values differ, in a stable order."""
    return [
        name
        for name in _TRACKED_FIELDS
        if _field_value(previous, name) != _field_value(current, name)
    ]


def text_similarity(left: str, right: str) -> float:
    """Similarity of two document texts in ``[0.0, 1.0]``.

    Two empty texts are identical (1.0); one empty and one not are completely
    dissimilar (0.0). Comparison is on whitespace-normalised tokens, so a
    reflowed paragraph does not read as a rewrite.
    """
    left_tokens = (left or "").split()
    right_tokens = (right or "").split()
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    matcher = difflib.SequenceMatcher(None, left_tokens, right_tokens, autojunk=False)
    return round(matcher.ratio(), 6)


def diff_documents(
    previous: Document | None,
    current: Document,
    *,
    substitution_threshold: float = 0.6,
) -> DocumentDiff:
    """Classify the change from ``previous`` to ``current``.

    With no previous observation the change is :attr:`ChangeKind.NEW`.

    Otherwise, in priority order: a move to ``withdrawn`` is
    :attr:`ChangeKind.WITHDRAWN`; a latest-version text whose similarity to
    the previous latest version falls below ``substitution_threshold`` is
    :attr:`ChangeKind.SUPERSEDED`, because that is a substitute rather than an
    edit; any other text change is :attr:`ChangeKind.TEXT_REVISED`; a status
    move is :attr:`ChangeKind.STATUS_ADVANCED` or
    :attr:`ChangeKind.STATUS_REGRESSED` by progression rank; any remaining
    field change is :attr:`ChangeKind.METADATA_ONLY`; and identical
    observations are :attr:`ChangeKind.UNCHANGED`.

    A status move *out of* a terminal status is always a regression regardless
    of rank — that pattern is nearly always a portal correcting a mistake, and
    it needs a human to look.
    """
    if previous is None:
        return DocumentDiff(
            document_id=current.document_id,
            kind=ChangeKind.NEW,
            current_status=current.status,
            new_actions=len(current.actions),
        )

    fields = changed_fields(previous, current)
    previous_version = previous.latest_version
    current_version = current.latest_version
    similarity: float | None = None
    if previous_version is not None or current_version is not None:
        similarity = text_similarity(
            previous_version.text if previous_version else "",
            current_version.text if current_version else "",
        )

    previous_actions = {(a.occurred_on, a.description.lower()) for a in previous.actions}
    new_actions = sum(
        1 for a in current.actions if (a.occurred_on, a.description.lower()) not in previous_actions
    )

    diff = DocumentDiff(
        document_id=current.document_id,
        kind=ChangeKind.UNCHANGED,
        fields=fields,
        previous_status=previous.status,
        current_status=current.status,
        text_similarity=similarity,
        new_actions=new_actions,
    )

    if current.status is BillStatus.WITHDRAWN and previous.status is not BillStatus.WITHDRAWN:
        diff.kind = ChangeKind.WITHDRAWN
        return diff

    text_changed = (
        previous_version is not None
        and current_version is not None
        and previous_version.semantic_hash != current_version.semantic_hash
    ) or (previous_version is None) != (current_version is None)

    if text_changed:
        if similarity is not None and similarity < substitution_threshold:
            diff.kind = ChangeKind.SUPERSEDED
            diff.notes.append("text similarity below substitution threshold")
        else:
            diff.kind = ChangeKind.TEXT_REVISED
        return diff

    if previous.status is not current.status:
        if previous.status in TERMINAL_STATUSES:
            diff.kind = ChangeKind.STATUS_REGRESSED
            diff.notes.append("moved out of a terminal status")
        elif status_rank(current.status) > status_rank(previous.status):
            diff.kind = ChangeKind.STATUS_ADVANCED
        else:
            diff.kind = ChangeKind.STATUS_REGRESSED
        return diff

    if fields or new_actions:
        diff.kind = ChangeKind.METADATA_ONLY
        return diff

    return diff
