"""Persistence pipeline stage.

Deduplicates within the batch, diffs against what is already stored, and hands
the result to the loaders. The diff is done here rather than in the warehouse
because the change feed needs the classification, not just the new row.
"""

from __future__ import annotations

from typing import Any, Protocol

from statehouse.core.models import Document
from statehouse.transform.dedupe import dedupe_batch
from statehouse.transform.diffing import DocumentDiff, diff_documents

__all__ = ["PersistPipeline", "DocumentSink"]


class DocumentSink(Protocol):
    """Where persisted documents go."""

    def fetch(self, document_id: str) -> Document | None:  # pragma: no cover
        ...

    def upsert(self, document: Document, diff: DocumentDiff) -> None:  # pragma: no cover
        ...


class PersistPipeline:
    """Writes documents and emits change records.

    Buffers to ``batch_size`` so the deduplication has something to work with;
    a document seen through three index pages in one crawl must be written
    once.
    """

    def __init__(self, sink: DocumentSink | None = None, batch_size: int = 200) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.sink = sink
        self.batch_size = batch_size
        self._buffer: list[Document] = []
        self.written = 0
        self.unchanged = 0
        self.diffs: list[DocumentDiff] = []

    @classmethod
    def from_crawler(cls, crawler: Any) -> PersistPipeline:  # pragma: no cover
        return cls()

    def process_item(self, item: Any, spider: Any) -> Any:
        if not isinstance(item, Document):
            return item
        self._buffer.append(item)
        if len(self._buffer) >= self.batch_size:
            self.flush(spider)
        return item

    def flush(self, spider: Any = None) -> list[DocumentDiff]:
        """Dedupe, diff and write the buffer. Returns the diffs produced."""
        if not self._buffer or self.sink is None:
            self._buffer.clear()
            return []

        produced: list[DocumentDiff] = []
        for document in dedupe_batch(self._buffer):
            previous = self.sink.fetch(document.document_id)
            diff = diff_documents(previous, document)
            if not diff.is_change:
                self.unchanged += 1
                continue
            self.sink.upsert(document, diff)
            self.written += 1
            produced.append(diff)

        self.diffs.extend(produced)
        self._buffer.clear()
        return produced

    def close_spider(self, spider: Any) -> None:  # pragma: no cover - Scrapy hook
        self.flush(spider)

    @property
    def notable_diffs(self) -> list[DocumentDiff]:
        """The subset a subscriber would be told about."""
        return [diff for diff in self.diffs if diff.notable]

    def stats(self) -> dict[str, int]:
        return {
            "written": self.written,
            "unchanged": self.unchanged,
            "notable": len(self.notable_diffs),
        }
