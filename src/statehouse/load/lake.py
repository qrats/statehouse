"""The raw lake.

Object keys are the contract here. They are partitioned by jurisdiction and
day so a reprocess can be scoped, and named by content digest so writing the
same bytes twice is idempotent.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from statehouse.core.errors import StorageError
from statehouse.core.models import RawFetch

__all__ = ["ObjectStore", "InMemoryObjectStore", "LakeWriter", "manifest_key"]


class ObjectStore(Protocol):
    """The subset of an object store this package needs."""

    def put(self, key: str, body: bytes, metadata: dict[str, str]) -> None:  # pragma: no cover
        ...

    def exists(self, key: str) -> bool:  # pragma: no cover
        ...


class InMemoryObjectStore(ObjectStore):
    """Reference store. Records writes so a test can assert on keys."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.put_calls = 0

    def put(self, key: str, body: bytes, metadata: dict[str, str]) -> None:
        self.put_calls += 1
        self.objects[key] = body
        self.metadata[key] = dict(metadata)

    def exists(self, key: str) -> bool:
        return key in self.objects

    def __len__(self) -> int:
        return len(self.objects)

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self.objects))


def manifest_key(jurisdiction: str, day: str, run_id: str) -> str:
    """Key for a run's manifest, which lists every object it wrote."""
    return f"manifests/{jurisdiction.lower()}/{day}/{run_id}.json"


@dataclass
class LakeWriter:
    """Writes raw fetches, skipping bytes already stored.

    The skip is what makes a re-run cheap: a crawl that re-fetches an
    unchanged page writes nothing, and the counters say so.
    """

    store: ObjectStore
    prefix: str = "raw"
    written: int = 0
    skipped: int = 0
    keys: list[str] = field(default_factory=list)

    def key_for(self, fetch: RawFetch) -> str:
        base = fetch.archive_key()
        if self.prefix and self.prefix != "raw":
            return base.replace("raw/", f"{self.prefix}/", 1)
        return base

    def write(self, fetch: RawFetch) -> str:
        """Store one fetch and return its key."""
        if not fetch.body:
            raise StorageError("refusing to archive an empty body", url=fetch.source.url)
        key = self.key_for(fetch)
        self.keys.append(key)
        if self.store.exists(key):
            self.skipped += 1
            return key
        self.store.put(
            key,
            fetch.body,
            {
                "jurisdiction": fetch.source.jurisdiction,
                "url": fetch.source.url,
                "digest": fetch.digest,
                "content-type": fetch.content_type,
                "method": fetch.source.method.value,
                "spider": fetch.source.spider or "",
                "status": str(fetch.source.http_status or ""),
            },
        )
        self.written += 1
        return key

    def stats(self) -> dict[str, Any]:
        return {"written": self.written, "skipped": self.skipped, "objects": len(self.keys)}
