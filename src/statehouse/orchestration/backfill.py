"""Backfill planning.

A backfill is not "run the incremental job with older dates". It has to be
chunked so a failure costs one chunk rather than a week, ordered so the most
useful data lands first, and budgeted so it does not starve the incremental
runs sharing the same politeness allowance.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from statehouse.config.jurisdictions import Jurisdiction
from statehouse.core.errors import ValidationError
from statehouse.utils.dates import chunk_range, session_years

__all__ = ["BackfillSlice", "BackfillPlan", "BackfillPlanner"]


@dataclass(frozen=True)
class BackfillSlice:
    """One unit of backfill work: a session and a date window inside it."""

    jurisdiction: str
    session: str
    start: date
    end: date
    priority: int = 0

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValidationError(
                "backfill slice ends before it starts",
                jurisdiction=self.jurisdiction,
                session=self.session,
            )

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def label(self) -> str:
        return f"{self.jurisdiction}/{self.session}/{self.start.isoformat()}"


@dataclass
class BackfillPlan:
    """An ordered set of slices, with the totals a scheduler needs."""

    jurisdiction: str
    slices: list[BackfillSlice] = field(default_factory=list)
    skipped_sessions: list[str] = field(default_factory=list)

    @property
    def total_days(self) -> int:
        return sum(item.days for item in self.slices)

    @property
    def sessions(self) -> list[str]:
        seen: list[str] = []
        for item in self.slices:
            if item.session not in seen:
                seen.append(item.session)
        return seen

    def take(self, count: int) -> list[BackfillSlice]:
        """The first ``count`` slices, which is what one DAG run will attempt."""
        if count < 0:
            raise ValueError("count cannot be negative")
        return self.slices[:count]

    def without(self, labels: Iterable[str]) -> BackfillPlan:
        """Drop slices already completed, by label."""
        done = set(labels)
        return BackfillPlan(
            jurisdiction=self.jurisdiction,
            slices=[item for item in self.slices if item.label not in done],
            skipped_sessions=list(self.skipped_sessions),
        )


@dataclass
class BackfillPlanner:
    """Turns "catch this jurisdiction up from 2015" into runnable slices.

    ``chunk_days`` bounds one slice. ``newest_first`` orders sessions from the
    most recent backwards, which is almost always what you want: recent
    sessions are what clients query, and an interrupted backfill has then
    delivered the useful half.
    """

    chunk_days: int = 30
    newest_first: bool = True
    max_slices: int | None = None

    def __post_init__(self) -> None:
        if self.chunk_days <= 0:
            raise ValueError("chunk_days must be positive")
        if self.max_slices is not None and self.max_slices <= 0:
            raise ValueError("max_slices must be positive when set")

    def plan(
        self,
        jurisdiction: Jurisdiction,
        *,
        through: date,
        from_year: int | None = None,
        completed_labels: Sequence[str] = (),
    ) -> BackfillPlan:
        """Build the plan for one jurisdiction up to ``through``.

        Sessions before the jurisdiction's ``backfill_from_year`` are skipped
        and named in ``skipped_sessions`` so the reason a gap exists is
        visible. A session whose window has not opened yet contributes
        nothing. Slices carry a priority equal to the session's start year, so
        a scheduler merging plans across jurisdictions can interleave them
        sensibly.
        """
        start_year = max(int(from_year or jurisdiction.backfill_from_year), 1900)
        biennial = "{biennium}" in jurisdiction.session_pattern
        years = session_years(start_year, through.year, biennial=biennial)

        plan = BackfillPlan(jurisdiction=jurisdiction.code)
        if not years:
            return plan

        if start_year > jurisdiction.backfill_from_year:
            for skipped in session_years(
                jurisdiction.backfill_from_year, start_year - 1, biennial=biennial
            ):
                plan.skipped_sessions.append(jurisdiction.session_label(skipped))

        ordered = sorted(years, reverse=self.newest_first)
        done = set(completed_labels)

        for year in ordered:
            session = jurisdiction.session_label(year)
            window_start = date(year, 1, 1)
            window_end = date(year + 1, 12, 31) if biennial else date(year, 12, 31)
            if window_end > through:
                window_end = through
            if window_end < window_start:
                continue
            for start, end in chunk_range(window_start, window_end, self.chunk_days):
                candidate = BackfillSlice(
                    jurisdiction=jurisdiction.code,
                    session=session,
                    start=start,
                    end=end,
                    priority=year,
                )
                if candidate.label in done:
                    continue
                plan.slices.append(candidate)
                if self.max_slices is not None and len(plan.slices) >= self.max_slices:
                    return plan
        return plan

    def plan_many(
        self,
        jurisdictions: Iterable[Jurisdiction],
        *,
        through: date,
        completed_labels: Sequence[str] = (),
    ) -> list[BackfillSlice]:
        """Interleave plans across jurisdictions, highest priority first.

        Interleaving matters: running one state to completion before starting
        the next means the last state is a month stale by the time it begins.
        Ties break on jurisdiction code and then start date, so the order is
        stable for a given input set.
        """
        merged: list[BackfillSlice] = []
        for jurisdiction in jurisdictions:
            merged.extend(
                self.plan(jurisdiction, through=through, completed_labels=completed_labels).slices
            )
        return sorted(merged, key=lambda s: (-s.priority, s.jurisdiction, s.start))
