"""Warehouse upserts.

The plan is built as data and executed separately. That separation is what
lets the upsert logic — which tables, in what order, with which conflict
targets — be tested without Postgres, and it is also what makes a dry run
possible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from statehouse.core.errors import StorageError
from statehouse.core.models import Document, to_jsonable
from statehouse.transform.diffing import DocumentDiff

__all__ = ["UpsertStatement", "UpsertPlan", "build_upsert_plan", "TABLE_ORDER"]

#: Parents before children. Reversing this is how you get a foreign-key error
#: at 3am.
TABLE_ORDER: tuple[str, ...] = (
    "documents",
    "document_versions",
    "document_actions",
    "document_sponsors",
    "document_subjects",
    "document_changes",
)

_CONFLICT_TARGETS: dict[str, tuple[str, ...]] = {
    "documents": ("document_id",),
    "document_versions": ("document_id", "semantic_hash"),
    "document_actions": ("document_id", "occurred_on", "description_hash"),
    "document_sponsors": ("document_id", "name"),
    "document_subjects": ("document_id", "subject"),
    "document_changes": ("document_id", "observed_at"),
}


@dataclass(frozen=True)
class UpsertStatement:
    """One table's worth of rows to write."""

    table: str
    rows: tuple[dict[str, Any], ...]
    conflict_target: tuple[str, ...]
    replace_children: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass
class UpsertPlan:
    """Everything one document's write touches, in execution order."""

    document_id: str
    statements: list[UpsertStatement] = field(default_factory=list)
    deletes: list[tuple[str, str]] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return sum(statement.row_count for statement in self.statements)

    def tables(self) -> list[str]:
        return [statement.table for statement in self.statements]

    def statement_for(self, table: str) -> UpsertStatement | None:
        for statement in self.statements:
            if statement.table == table:
                return statement
        return None


def _describe(document: Document) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "jurisdiction": document.jurisdiction,
        "session": document.session,
        "identifier": document.identifier,
        "citation": document.citation,
        "title": document.title,
        "summary": document.summary,
        "kind": document.kind.value,
        "chamber": document.chamber.value,
        "status": document.status.value,
        "introduced_on": document.introduced_on.isoformat() if document.introduced_on else None,
        "last_action_on": document.last_action_on.isoformat() if document.last_action_on else None,
        "metadata_fingerprint": document.metadata_fingerprint(),
        "source_url": document.source.url if document.source else None,
        "observed_at": to_jsonable(document.observed_at),
        "extras": to_jsonable(document.extras),
    }


def build_upsert_plan(document: Document, diff: DocumentDiff | None = None) -> UpsertPlan:
    """Build the write plan for one document.

    Child rows are written as a full replacement of the document's existing
    set rather than merged: a sponsor removed upstream has to disappear here
    too, and an incremental merge cannot express a removal. The replacement is
    expressed as a delete followed by an insert so an executor can run it in
    one transaction.
    """
    if not document.document_id:
        raise StorageError("document has no id", identifier=document.identifier)

    plan = UpsertPlan(document_id=document.document_id)
    plan.statements.append(
        UpsertStatement(
            table="documents",
            rows=(_describe(document),),
            conflict_target=_CONFLICT_TARGETS["documents"],
        )
    )

    version_rows = tuple(
        {
            "document_id": document.document_id,
            "label": version.label,
            "semantic_hash": version.semantic_hash,
            "byte_hash": version.byte_hash,
            "published_on": version.published_on.isoformat() if version.published_on else None,
            "length": version.length,
            "text": version.text,
        }
        for version in document.versions
    )
    if version_rows:
        plan.statements.append(
            UpsertStatement(
                table="document_versions",
                rows=version_rows,
                conflict_target=_CONFLICT_TARGETS["document_versions"],
            )
        )

    action_rows = tuple(
        {
            "document_id": document.document_id,
            "occurred_on": action.occurred_on.isoformat(),
            "sequence": action.sequence,
            "description": action.description,
            "description_hash": str(abs(hash(action.description.lower())) % (10**12)),
            "kind": action.kind.value,
            "chamber": action.chamber.value,
            "committee": action.committee,
            "resulting_status": action.resulting_status.value if action.resulting_status else None,
        }
        for action in document.ordered_actions()
    )
    if action_rows:
        plan.deletes.append(("document_actions", document.document_id))
        plan.statements.append(
            UpsertStatement(
                table="document_actions",
                rows=action_rows,
                conflict_target=_CONFLICT_TARGETS["document_actions"],
                replace_children=True,
            )
        )

    sponsor_rows = tuple(
        {
            "document_id": document.document_id,
            "name": sponsor.name,
            "role": sponsor.role.value,
            "party": sponsor.party,
            "district": sponsor.district,
            "chamber": sponsor.chamber.value,
            "position": index,
        }
        for index, sponsor in enumerate(document.sponsors)
    )
    if sponsor_rows:
        plan.deletes.append(("document_sponsors", document.document_id))
        plan.statements.append(
            UpsertStatement(
                table="document_sponsors",
                rows=sponsor_rows,
                conflict_target=_CONFLICT_TARGETS["document_sponsors"],
                replace_children=True,
            )
        )

    subject_rows = tuple(
        {"document_id": document.document_id, "subject": subject}
        for subject in document.subjects
    )
    if subject_rows:
        plan.deletes.append(("document_subjects", document.document_id))
        plan.statements.append(
            UpsertStatement(
                table="document_subjects",
                rows=subject_rows,
                conflict_target=_CONFLICT_TARGETS["document_subjects"],
                replace_children=True,
            )
        )

    if diff is not None and diff.is_change:
        plan.statements.append(
            UpsertStatement(
                table="document_changes",
                rows=(
                    {
                        "document_id": document.document_id,
                        "observed_at": to_jsonable(document.observed_at),
                        **diff.to_dict(),
                    },
                ),
                conflict_target=_CONFLICT_TARGETS["document_changes"],
            )
        )

    plan.statements.sort(key=lambda s: TABLE_ORDER.index(s.table))
    return plan


def plan_batch(pairs: Sequence[tuple[Document, DocumentDiff | None]]) -> list[UpsertPlan]:
    """Plans for a whole batch, in document-id order for a stable write order."""
    return sorted(
        (build_upsert_plan(document, diff) for document, diff in pairs),
        key=lambda plan: plan.document_id,
    )
