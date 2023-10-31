"""Quality pipeline stage.

Buffers documents so the checks that need the whole batch — duplicate ids,
error rate — have one to look at. Flushing on a size threshold rather than at
close keeps memory bounded on a full-session crawl.
"""

from __future__ import annotations

from typing import Any

from statehouse.core.models import Document, QualityFinding
from statehouse.quality.gate import GateDecision, QualityGate

__all__ = ["QualityPipeline"]


class QualityPipeline:
    """Runs the gate over batches as they accumulate."""

    def __init__(self, gate: QualityGate | None = None, batch_size: int = 500) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.gate = gate or QualityGate(max_errors=0, max_error_rate=0.02)
        self.batch_size = batch_size
        self._buffer: list[Document] = []
        self.findings: list[QualityFinding] = []
        self.decisions: list[GateDecision] = []
        self.blocked_batches = 0

    @classmethod
    def from_crawler(cls, crawler: Any) -> "QualityPipeline":  # pragma: no cover
        return cls()

    def process_item(self, item: Any, spider: Any) -> Any:
        if not isinstance(item, Document):
            return item
        self._buffer.append(item)
        if len(self._buffer) >= self.batch_size:
            self.flush(spider)
        return item

    def flush(self, spider: Any = None) -> GateDecision | None:
        """Evaluate and clear the buffer. Returns ``None`` when it was empty."""
        if not self._buffer:
            return None
        decision = self.gate.evaluate(self._buffer)
        self.decisions.append(decision)
        self.findings.extend(decision.findings)
        if not decision.passed:
            self.blocked_batches += 1
            logger = getattr(spider, "logger", None)
            if logger is not None:
                logger.error(
                    "quality gate blocked a batch jurisdiction=%s blocked_by=%s",
                    getattr(spider, "jurisdiction_code", "?"),
                    ",".join(decision.blocked_by),
                )
        self._buffer.clear()
        return decision

    def close_spider(self, spider: Any) -> None:  # pragma: no cover - Scrapy hook
        self.flush(spider)

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    @property
    def clean(self) -> bool:
        return self.blocked_batches == 0
