"""Watermarks: how far each stream has been ingested.

The rule that makes incremental ingest safe is that a watermark only ever
moves forward, and only after the data behind it has actually landed. Two
concurrent runs of the same jurisdiction — which Airflow will happily give you
if a task times out and is retried while the original is still going — must
not be able to interleave into a position that skips documents.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from typing import Protocol

from statehouse.core.clock import Clock, ensure_utc, utc_now
from statehouse.core.errors import WatermarkConflict
from statehouse.core.models import Watermark

__all__ = ["WatermarkStore", "InMemoryWatermarkStore", "advance", "lag_seconds"]

#: How far back a watermark is deliberately held from the newest observation.
#: Sources publish out of order for a few minutes after a floor session; the
#: overlap costs a little re-fetching and buys us not losing documents.
DEFAULT_SAFETY_LAG = timedelta(minutes=15)


class WatermarkStore(Protocol):
    """Persistence for watermarks."""

    def get(self, jurisdiction: str, stream: str) -> Watermark | None:  # pragma: no cover
        ...

    def put(self, watermark: Watermark) -> Watermark:  # pragma: no cover
        ...

    def list_all(self) -> list[Watermark]:  # pragma: no cover
        ...


class InMemoryWatermarkStore(WatermarkStore):
    """Reference implementation, used by tests and the local runner.

    Enforces optimistic concurrency on ``revision``: a write built from a stale
    read raises :class:`~statehouse.core.errors.WatermarkConflict` instead of
    silently winning.
    """

    def __init__(self, initial: Iterable[Watermark] = ()) -> None:
        self._items: dict[str, Watermark] = {}
        for watermark in initial:
            self._items[watermark.key] = watermark

    def get(self, jurisdiction: str, stream: str = "default") -> Watermark | None:
        return self._items.get(f"{jurisdiction.strip().lower()}:{stream}")

    def put(self, watermark: Watermark) -> Watermark:
        current = self._items.get(watermark.key)
        if current is not None and watermark.revision <= current.revision:
            raise WatermarkConflict(
                "watermark write is based on a stale read",
                key=watermark.key,
                incoming_revision=watermark.revision,
                stored_revision=current.revision,
            )
        self._items[watermark.key] = watermark
        return watermark

    def list_all(self) -> list[Watermark]:
        return [self._items[key] for key in sorted(self._items)]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Watermark]:
        return iter(self.list_all())


def advance(
    current: Watermark | None,
    *,
    jurisdiction: str,
    stream: str = "default",
    position: str = "",
    observed_through: datetime | None = None,
    safety_lag: timedelta = DEFAULT_SAFETY_LAG,
    clock: Clock | None = None,
) -> Watermark:
    """Produce the watermark that should replace ``current``.

    ``observed_through`` is the newest moment the run is confident about; the
    stored value is that minus ``safety_lag``. The result never moves
    backwards: if the computed value is older than what is already stored, the
    stored timestamp is kept and only the revision and ``updated_at`` change,
    so a short catch-up run cannot rewind a long one.

    Passing no ``observed_through`` records progress without moving the time
    boundary, which is what a position-only cursor (a page token) needs.
    """
    now = utc_now(clock)
    previous_through = current.observed_through if current else None
    candidate = previous_through

    if observed_through is not None:
        lagged = ensure_utc(observed_through) - safety_lag
        candidate = lagged if previous_through is None else max(previous_through, lagged)

    return Watermark(
        jurisdiction=jurisdiction,
        stream=stream,
        position=position or (current.position if current else ""),
        observed_through=candidate,
        updated_at=now,
        revision=(current.revision + 1) if current else 1,
    )


def lag_seconds(watermark: Watermark | None, now: datetime) -> float | None:
    """How far behind the watermark is, in seconds.

    ``None`` when the watermark has never been set, which callers report
    differently from "behind by a lot".
    """
    if watermark is None or watermark.observed_through is None:
        return None
    return max(0.0, (ensure_utc(now) - watermark.observed_through).total_seconds())
