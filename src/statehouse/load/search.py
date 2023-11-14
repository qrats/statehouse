"""Search indexing.

Clients query by keyword with negation, by jurisdiction, by subject and by
date range, so the index document is denormalised: everything a result card
shows has to come back from one hit.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from statehouse.core.models import Document, to_jsonable
from statehouse.utils.text import truncate

__all__ = ["index_document", "bulk_actions", "SearchIndexer", "INDEX_SETTINGS"]

#: Bill text runs to hundreds of kilobytes; the index keeps a leading window
#: for highlighting and the warehouse keeps the whole thing.
TEXT_WINDOW = 60_000

INDEX_SETTINGS: dict[str, Any] = {
    "settings": {
        "index": {"number_of_shards": 3, "number_of_replicas": 1, "refresh_interval": "30s"},
        "analysis": {
            "analyzer": {
                "legislative": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "english_stop", "english_stemmer"],
                }
            },
            "filter": {
                "english_stop": {"type": "stop", "stopwords": "_english_"},
                "english_stemmer": {"type": "stemmer", "language": "english"},
            },
        },
    },
    "mappings": {
        "properties": {
            "document_id": {"type": "keyword"},
            "jurisdiction": {"type": "keyword"},
            "session": {"type": "keyword"},
            "identifier": {"type": "keyword"},
            "citation": {"type": "keyword"},
            "title": {"type": "text", "analyzer": "legislative"},
            "summary": {"type": "text", "analyzer": "legislative"},
            "body": {"type": "text", "analyzer": "legislative"},
            "status": {"type": "keyword"},
            "chamber": {"type": "keyword"},
            "kind": {"type": "keyword"},
            "subjects": {"type": "keyword"},
            "sponsor_names": {"type": "keyword"},
            "introduced_on": {"type": "date"},
            "last_action_on": {"type": "date"},
            "observed_at": {"type": "date"},
            "action_count": {"type": "integer"},
        }
    },
}


def index_document(document: Document) -> dict[str, Any]:
    """The indexed representation of a document.

    Body text is the latest version truncated to :data:`TEXT_WINDOW`; a
    document with no version indexes an empty body rather than being skipped,
    because metadata-only search still has to find it.
    """
    latest = document.latest_version
    return {
        "document_id": document.document_id,
        "jurisdiction": document.jurisdiction,
        "session": document.session,
        "identifier": document.identifier,
        "citation": document.citation,
        "title": document.title,
        "summary": document.summary,
        "body": truncate(latest.text, TEXT_WINDOW) if latest else "",
        "status": document.status.value,
        "chamber": document.chamber.value,
        "kind": document.kind.value,
        "subjects": list(document.subjects),
        "sponsor_names": [sponsor.name for sponsor in document.sponsors],
        "introduced_on": document.introduced_on.isoformat() if document.introduced_on else None,
        "last_action_on": document.last_action_on.isoformat() if document.last_action_on else None,
        "observed_at": to_jsonable(document.observed_at),
        "action_count": len(document.actions),
    }


def bulk_actions(documents: Iterable[Document], index: str) -> list[dict[str, Any]]:
    """Bulk-API action/source pairs, flattened into one list.

    Uses ``index`` rather than ``create`` so a re-ingest overwrites; the
    document id is the deterministic warehouse id, which is what makes that
    safe.
    """
    payload: list[dict[str, Any]] = []
    for document in documents:
        payload.append({"index": {"_index": index, "_id": document.document_id}})
        payload.append(index_document(document))
    return payload


@dataclass
class SearchIndexer:
    """Batches documents into bulk requests.

    ``client`` is anything with a ``bulk(body)`` method. Left as ``None`` the
    indexer collects batches without sending them, which is what the dry-run
    path and the tests use.
    """

    index: str
    client: Any = None
    batch_size: int = 250
    sent: list[list[dict[str, Any]]] = field(default_factory=list)
    failures: int = 0

    def __post_init__(self) -> None:
        if not self.index:
            raise ValueError("SearchIndexer requires an index name")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")

    def submit(self, documents: Sequence[Document]) -> int:
        """Index ``documents``, returning how many were accepted.

        A failed batch is counted and skipped rather than retried here: the
        retry policy belongs to the caller, which knows whether the run has
        time left.
        """
        accepted = 0
        for start in range(0, len(documents), self.batch_size):
            chunk = documents[start : start + self.batch_size]
            body = bulk_actions(chunk, self.index)
            self.sent.append(body)
            if self.client is None:
                accepted += len(chunk)
                continue
            try:
                self.client.bulk(body=body)
                accepted += len(chunk)
            except Exception:  # noqa: BLE001 - reported through the counter
                self.failures += 1
        return accepted

    @property
    def batches(self) -> int:
        return len(self.sent)
