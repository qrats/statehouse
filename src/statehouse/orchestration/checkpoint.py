"""Checkpoints: resuming a crawl that died in the middle.

A full-session crawl of a large state is tens of thousands of pages and does
not fit in one task attempt. The checkpoint records enough to pick up where
the previous attempt stopped without re-fetching what already landed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from statehouse.core.clock import Clock, ensure_utc, isoformat, utc_now
from statehouse.core.errors import CheckpointCorrupt

__all__ = ["Checkpoint", "CheckpointStore", "InMemoryCheckpointStore"]

_SCHEMA_VERSION = 2


@dataclass
class Checkpoint:
    """A resumable position within one crawl."""

    jurisdiction: str
    stream: str
    cursor: str = ""
    completed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    attempt: int = 0
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.jurisdiction = (self.jurisdiction or "").strip().lower()
        self.stream = (self.stream or "default").strip() or "default"
        if self.attempt < 0:
            raise ValueError("attempt cannot be negative")
        if self.updated_at is not None:
            self.updated_at = ensure_utc(self.updated_at)
        self.completed = list(dict.fromkeys(self.completed))
        self.pending = [
            item for item in dict.fromkeys(self.pending) if item not in set(self.completed)
        ]

    @property
    def key(self) -> str:
        return f"{self.jurisdiction}:{self.stream}"

    @property
    def remaining(self) -> int:
        return len(self.pending)

    @property
    def exhausted(self) -> bool:
        return not self.pending

    def mark_done(self, unit: str, *, clock: Clock | None = None) -> Checkpoint:
        """Record ``unit`` as completed and drop it from pending.

        Marking something done twice is a no-op rather than an error: a
        retried task legitimately reprocesses the tail of its previous run.
        """
        if unit not in self.completed:
            self.completed.append(unit)
        self.pending = [item for item in self.pending if item != unit]
        self.updated_at = utc_now(clock)
        return self

    def enqueue(self, units: Iterable[str]) -> Checkpoint:
        """Add work, skipping anything already done or queued."""
        known = set(self.completed) | set(self.pending)
        for unit in units:
            if unit and unit not in known:
                known.add(unit)
                self.pending.append(unit)
        return self

    def next_unit(self) -> str | None:
        return self.pending[0] if self.pending else None

    def to_json(self) -> str:
        payload = {
            "schema": _SCHEMA_VERSION,
            "jurisdiction": self.jurisdiction,
            "stream": self.stream,
            "cursor": self.cursor,
            "completed": self.completed,
            "pending": self.pending,
            "attempt": self.attempt,
            "updated_at": isoformat(self.updated_at) if self.updated_at else None,
            "metadata": self.metadata,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> Checkpoint:
        """Decode a checkpoint, rejecting anything unusable.

        A schema from the future is refused rather than guessed at; an older
        schema is upgraded, because a rolling deploy will read checkpoints
        written by the previous version.
        """
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise CheckpointCorrupt("checkpoint is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CheckpointCorrupt("checkpoint is not an object")
        schema = payload.get("schema", 1)
        if not isinstance(schema, int) or schema > _SCHEMA_VERSION:
            raise CheckpointCorrupt("checkpoint schema is newer than this build", schema=schema)
        jurisdiction = payload.get("jurisdiction")
        if not jurisdiction:
            raise CheckpointCorrupt("checkpoint has no jurisdiction")
        updated_raw = payload.get("updated_at")
        updated: datetime | None = None
        if updated_raw:
            try:
                updated = ensure_utc(
                    datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
                )
            except ValueError as exc:
                raise CheckpointCorrupt("checkpoint has an unreadable timestamp") from exc
        return cls(
            jurisdiction=str(jurisdiction),
            stream=str(payload.get("stream") or "default"),
            cursor=str(payload.get("cursor") or ""),
            completed=[str(x) for x in payload.get("completed") or []],
            pending=[str(x) for x in payload.get("pending") or []],
            attempt=int(payload.get("attempt") or 0),
            updated_at=updated,
            metadata=dict(payload.get("metadata") or {}),
        )


class CheckpointStore(Protocol):
    """Persistence for checkpoints."""

    def load(self, jurisdiction: str, stream: str) -> Checkpoint | None:  # pragma: no cover
        ...

    def save(self, checkpoint: Checkpoint) -> Checkpoint:  # pragma: no cover
        ...

    def clear(self, jurisdiction: str, stream: str) -> None:  # pragma: no cover
        ...


class InMemoryCheckpointStore(CheckpointStore):
    """Reference implementation. Serialises through JSON so it catches the
    same encoding bugs the real store would."""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def load(self, jurisdiction: str, stream: str = "default") -> Checkpoint | None:
        raw = self._items.get(f"{jurisdiction.strip().lower()}:{stream}")
        return Checkpoint.from_json(raw) if raw else None

    def save(self, checkpoint: Checkpoint) -> Checkpoint:
        self._items[checkpoint.key] = checkpoint.to_json()
        return checkpoint

    def clear(self, jurisdiction: str, stream: str = "default") -> None:
        self._items.pop(f"{jurisdiction.strip().lower()}:{stream}", None)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Checkpoint]:
        return iter(Checkpoint.from_json(self._items[key]) for key in sorted(self._items))
