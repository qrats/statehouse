"""The glue the DAG tasks call.

Each function here is one task's worth of work, expressed so it can also be
run from a shell or a test. Nothing in this module imports Airflow.

State between tasks travels as a *batch key* rather than as the batch itself:
XCom is not a data channel, and a session's worth of bill text will not fit
in it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from statehouse.config.jurisdictions import Jurisdiction
from statehouse.config.settings import Settings, load_settings
from statehouse.core.clock import Clock, utc_now
from statehouse.core.models import Document, QualityFinding
from statehouse.load.memory import InMemorySink
from statehouse.load.search import SearchIndexer
from statehouse.orchestration.watermark import InMemoryWatermarkStore, WatermarkStore
from statehouse.quality.gate import QualityGate
from statehouse.transform.dedupe import dedupe_batch
from statehouse.transform.diffing import diff_documents
from statehouse.transform.normalize import normalise_document

__all__ = [
    "FetchOutcome",
    "TransformResult",
    "LoadResult",
    "BatchStore",
    "run_spider",
    "transform_batch",
    "load_batch",
    "watermark_store",
    "batch_store",
]


@dataclass
class FetchOutcome:
    """What a fetch task produced."""

    jurisdiction: str
    batch_key: str
    documents_seen: int = 0
    fetch_failures: int = 0
    observed_through: datetime | None = None


@dataclass
class TransformResult:
    """What a transform task produced."""

    batch_key: str
    document_count: int = 0
    passed: bool = True
    blocked_by: list[str] = field(default_factory=list)
    findings: list[QualityFinding] = field(default_factory=list)


@dataclass
class LoadResult:
    """What a load task produced."""

    written: int = 0
    unchanged: int = 0
    indexed: int = 0
    notable_changes: int = 0


class BatchStore:
    """Hands batches between tasks by key.

    The deployed implementation writes Parquet to the lake; this one keeps
    them in the process, which is what the local runner and the tests use.
    Both satisfy the same three methods.
    """

    def __init__(self) -> None:
        self._batches: dict[str, list[Document]] = {}

    def put(self, key: str, documents: Sequence[Document]) -> str:
        self._batches[key] = list(documents)
        return key

    def get(self, key: str) -> list[Document]:
        return list(self._batches.get(key, []))

    def drop(self, key: str) -> None:
        self._batches.pop(key, None)

    def __len__(self) -> int:
        return len(self._batches)


_BATCHES = BatchStore()
_WATERMARKS = InMemoryWatermarkStore()
_SINK = InMemorySink()


def batch_store() -> BatchStore:
    return _BATCHES


def watermark_store() -> WatermarkStore:
    return _WATERMARKS


def _batch_key(jurisdiction: str, moment: datetime) -> str:
    return f"{jurisdiction}/{moment.strftime('%Y%m%dT%H%M%S')}"


def run_spider(
    jurisdiction: Jurisdiction,
    *,
    settings: Settings | None = None,
    records: Sequence[dict[str, Any]] | None = None,
    session: str | None = None,
    clock: Clock | None = None,
) -> FetchOutcome:
    """Fetch a jurisdiction's current session.

    ``records`` short-circuits the crawl with pre-fetched rows, which is how
    the local runner replays an archived batch and how the tests avoid needing
    a network. Without it the Scrapy runner is invoked, which is imported
    lazily so that this module stays importable where Scrapy is not.
    """
    active = settings or load_settings()
    now = utc_now(clock)
    label = session or jurisdiction.session_label(now.year)

    if records is None:  # pragma: no cover - needs Scrapy and a network
        from statehouse.scraping.runner import crawl

        records = crawl(jurisdiction, session=label, settings=active)

    documents: list[Document] = []
    failures = 0
    for record in records:
        try:
            documents.append(
                normalise_document(
                    record, jurisdiction=jurisdiction.code, session=label, clock=clock
                )
            )
        except Exception:  # noqa: BLE001 - counted, not fatal
            failures += 1

    key = _batch_key(jurisdiction.code, now)
    _BATCHES.put(key, documents)
    return FetchOutcome(
        jurisdiction=jurisdiction.code,
        batch_key=key,
        documents_seen=len(documents),
        fetch_failures=failures,
        observed_through=now,
    )


def transform_batch(
    jurisdiction_code: str,
    batch_key: str,
    *,
    gate: QualityGate | None = None,
) -> TransformResult:
    """Deduplicate a batch and run the quality gate over it.

    The deduplicated batch replaces the original under the same key, so the
    load task never sees the duplicates.
    """
    documents = dedupe_batch(_BATCHES.get(batch_key))
    _BATCHES.put(batch_key, documents)
    decision = (gate or QualityGate(max_errors=0, max_error_rate=0.02)).evaluate(documents)
    return TransformResult(
        batch_key=batch_key,
        document_count=len(documents),
        passed=decision.passed,
        blocked_by=list(decision.blocked_by),
        findings=list(decision.findings),
    )


def load_batch(
    jurisdiction_code: str,
    batch_key: str,
    *,
    sink: InMemorySink | None = None,
    indexer: SearchIndexer | None = None,
) -> LoadResult:
    """Write a batch, emitting change records for anything that moved."""
    # ``is None`` rather than ``or``: an empty sink is falsy, and silently
    # swapping the caller's sink for the process-wide one hides every write.
    target = _SINK if sink is None else sink
    documents = _BATCHES.get(batch_key)
    result = LoadResult()
    changed: list[Document] = []

    for document in documents:
        previous = target.fetch(document.document_id)
        diff = diff_documents(previous, document)
        if not diff.is_change:
            result.unchanged += 1
            continue
        target.upsert(document, diff)
        result.written += 1
        if diff.notable:
            result.notable_changes += 1
        changed.append(document)

    if indexer is not None and changed:
        result.indexed = indexer.submit(changed)

    _BATCHES.drop(batch_key)
    return result
