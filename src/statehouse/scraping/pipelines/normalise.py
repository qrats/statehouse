"""Normalisation pipeline stage."""

from __future__ import annotations

from typing import Any

from statehouse.core.clock import Clock
from statehouse.core.errors import ValidationError
from statehouse.core.models import Document
from statehouse.transform.normalize import normalise_document

__all__ = ["NormalisePipeline"]


class NormalisePipeline:
    """Turns loose spider dictionaries into canonical documents.

    A record that cannot be normalised is dropped and counted rather than
    raised: one malformed row on a portal that serves ten thousand should not
    end the run. The count feeds the quality gate, which is where "too many
    dropped" becomes a failure.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock
        self.normalised = 0
        self.dropped = 0
        self.errors: list[str] = []

    @classmethod
    def from_crawler(cls, crawler: Any) -> "NormalisePipeline":  # pragma: no cover
        return cls()

    def process_item(self, item: Any, spider: Any) -> Any:
        raw = dict(item)
        jurisdiction = raw.pop("jurisdiction", None) or getattr(spider, "jurisdiction_code", "")
        session = raw.pop("session", None) or getattr(spider, "session", "")
        try:
            document = normalise_document(
                raw,
                jurisdiction=jurisdiction,
                session=session,
                spider=getattr(spider, "name", None),
                clock=self._clock,
            )
        except ValidationError as exc:
            self.dropped += 1
            if len(self.errors) < 50:
                self.errors.append(str(exc))
            logger = getattr(spider, "logger", None)
            if logger is not None:
                logger.warning("dropping unnormalisable record: %s", exc)
            return None
        self.normalised += 1
        return document

    def stats(self) -> dict[str, int]:
        return {"normalised": self.normalised, "dropped": self.dropped}


def is_document(candidate: Any) -> bool:
    """Whether a pipeline item survived normalisation."""
    return isinstance(candidate, Document)
