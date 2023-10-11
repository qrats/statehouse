"""Deciding what to run next.

The incremental DAG wakes up every fifteen minutes and cannot run fifty
jurisdictions at once: the browser pool is small, and several portals will
block us if we do. This module picks the subset worth running now.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from statehouse.config.jurisdictions import Jurisdiction
from statehouse.core.clock import Clock, utc_now
from statehouse.core.enums import FetchMethod
from statehouse.core.models import Watermark
from statehouse.orchestration.watermark import lag_seconds

__all__ = ["ScheduleCandidate", "SchedulePlan", "IngestScheduler"]


@dataclass(frozen=True)
class ScheduleCandidate:
    """One jurisdiction considered for this cycle."""

    jurisdiction: Jurisdiction
    lag_seconds: float | None
    score: float
    reason: str

    @property
    def code(self) -> str:
        return self.jurisdiction.code


@dataclass
class SchedulePlan:
    """What to run, and what was left out and why."""

    selected: list[ScheduleCandidate]
    deferred: list[ScheduleCandidate]

    @property
    def codes(self) -> list[str]:
        return [candidate.code for candidate in self.selected]

    @property
    def browser_slots_used(self) -> int:
        return sum(
            1 for candidate in self.selected if candidate.jurisdiction.method is FetchMethod.BROWSER
        )


@dataclass
class IngestScheduler:
    """Selects jurisdictions by staleness, subject to capacity limits.

    ``max_parallel`` caps the whole cycle; ``max_browser`` caps the subset
    that needs a real browser, which is the scarce resource. A jurisdiction
    that has never been ingested outranks every stale one — an empty stream is
    worse than a late one.
    """

    max_parallel: int = 8
    max_browser: int = 2
    min_lag_seconds: float = 900.0

    def __post_init__(self) -> None:
        if self.max_parallel <= 0:
            raise ValueError("max_parallel must be positive")
        if self.max_browser < 0:
            raise ValueError("max_browser cannot be negative")
        if self.min_lag_seconds < 0:
            raise ValueError("min_lag_seconds cannot be negative")

    def score(self, jurisdiction: Jurisdiction, watermark: Watermark | None, now: datetime) -> ScheduleCandidate:
        """Score one jurisdiction. Higher is more urgent."""
        lag = lag_seconds(watermark, now)
        if lag is None:
            return ScheduleCandidate(jurisdiction, None, float("inf"), "never ingested")
        if lag < self.min_lag_seconds:
            return ScheduleCandidate(jurisdiction, lag, 0.0, "within freshness target")
        weight = 1.0
        if "large" in jurisdiction.tags:
            weight = 1.5
        if jurisdiction.is_federal:
            weight = 2.0
        return ScheduleCandidate(jurisdiction, lag, lag * weight, "stale")

    def plan(
        self,
        jurisdictions: Iterable[Jurisdiction],
        watermarks: Sequence[Watermark],
        *,
        stream: str = "default",
        clock: Clock | None = None,
    ) -> SchedulePlan:
        """Build this cycle's plan.

        Disabled jurisdictions never appear. Candidates scoring zero are
        deferred rather than dropped, so the DAG can log why a jurisdiction
        was skipped instead of leaving an unexplained gap.
        """
        now = utc_now(clock)
        index = {w.key: w for w in watermarks}
        scored = [
            self.score(jurisdiction, index.get(f"{jurisdiction.code}:{stream}"), now)
            for jurisdiction in jurisdictions
            if jurisdiction.enabled
        ]
        scored.sort(key=lambda c: (-c.score, c.code))

        selected: list[ScheduleCandidate] = []
        deferred: list[ScheduleCandidate] = []
        browser_used = 0

        for candidate in scored:
            if candidate.score <= 0.0:
                deferred.append(candidate)
                continue
            if len(selected) >= self.max_parallel:
                deferred.append(candidate)
                continue
            if candidate.jurisdiction.method is FetchMethod.BROWSER:
                if browser_used >= self.max_browser:
                    deferred.append(candidate)
                    continue
                browser_used += 1
            selected.append(candidate)

        return SchedulePlan(selected=selected, deferred=deferred)
