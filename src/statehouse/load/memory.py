"""In-memory sink.

The reference implementation of :class:`~statehouse.scraping.pipelines.persist.DocumentSink`.
Used by the local runner, by the DAG tests, and as the thing the Postgres sink
is checked against.
"""

from __future__ import annotations

from collections.abc import Iterator

from statehouse.core.models import Document
from statehouse.load.warehouse import UpsertPlan, build_upsert_plan
from statehouse.transform.diffing import DocumentDiff

__all__ = ["InMemorySink"]


class InMemorySink:
    """Stores the latest version of each document and every diff seen."""

    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}
        self.diffs: list[DocumentDiff] = []
        self.plans: list[UpsertPlan] = []

    def fetch(self, document_id: str) -> Document | None:
        return self._documents.get(document_id)

    def upsert(self, document: Document, diff: DocumentDiff) -> None:
        self._documents[document.document_id] = document
        self.diffs.append(diff)
        self.plans.append(build_upsert_plan(document, diff))

    def all_documents(self) -> list[Document]:
        return [self._documents[key] for key in sorted(self._documents)]

    def by_jurisdiction(self, jurisdiction: str) -> list[Document]:
        wanted = jurisdiction.strip().lower()
        return [d for d in self.all_documents() if d.jurisdiction == wanted]

    def notable_diffs(self) -> list[DocumentDiff]:
        return [diff for diff in self.diffs if diff.notable]

    def rows_written(self) -> int:
        return sum(plan.row_count for plan in self.plans)

    def __len__(self) -> int:
        return len(self._documents)

    def __iter__(self) -> Iterator[Document]:
        return iter(self.all_documents())
