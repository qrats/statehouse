"""Run bookkeeping.

"Did last night's ingest work?" has to be answerable without reading logs, and
"how many times has Georgia failed this week?" has to be answerable at all.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from statehouse.core.clock import Clock, ensure_utc, utc_now
from statehouse.core.enums import RunState
from statehouse.core.errors import StatehouseError
from statehouse.core.models import IngestRun, QualityFinding

__all__ = ["RunRegistry", "RunSummary"]


class RunSummary(dict):
    """Aggregate view over a set of runs. A plain dict so it serialises."""

    @property
    def healthy(self) -> bool:
        return bool(self.get("succeeded", 0)) and not self.get("failed", 0)


class RunRegistry:
    """In-memory registry of ingest runs.

    The production deployment persists these to Postgres; the interface is the
    same, and keeping a working in-memory implementation means the DAG logic
    is testable without a database.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._runs: dict[str, IngestRun] = {}
        self._clock = clock

    def start(self, run_id: str, jurisdiction: str, **metadata: object) -> IngestRun:
        """Open a run, or return the existing one if it is already open.

        Idempotent on ``run_id`` because Airflow retries a task with the same
        run id and a second open would double-count the jurisdiction.
        """
        existing = self._runs.get(run_id)
        if existing is not None:
            return existing
        run = IngestRun(
            run_id=run_id,
            jurisdiction=jurisdiction,
            started_at=utc_now(self._clock),
            state=RunState.RUNNING,
            metadata=dict(metadata),
        )
        self._runs[run_id] = run
        return run

    def get(self, run_id: str) -> IngestRun:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise StatehouseError("no such run", run_id=run_id) from exc

    def record(
        self,
        run_id: str,
        *,
        seen: int = 0,
        written: int = 0,
        skipped: int = 0,
        fetch_failures: int = 0,
        findings: Iterable[QualityFinding] = (),
    ) -> IngestRun:
        """Accumulate counters onto an open run."""
        run = self.get(run_id)
        run.documents_seen += seen
        run.documents_written += written
        run.documents_skipped += skipped
        run.fetch_failures += fetch_failures
        run.findings.extend(findings)
        return run

    def finish(
        self, run_id: str, state: RunState | str, error: BaseException | None = None
    ) -> IngestRun:
        """Close a run.

        A run that wrote something but also hit blocking findings closes as
        ``partial`` rather than ``succeeded``, even when the caller asked for
        success: partially-loaded data that nobody flagged is how a warehouse
        quietly goes wrong.
        """
        run = self.get(run_id)
        resolved = RunState.parse(state, RunState.FAILED)
        if resolved is RunState.SUCCEEDED and run.blocking_findings:
            resolved = RunState.PARTIAL
        run.finish(resolved, utc_now(self._clock))
        if error is not None:
            run.error = (
                error.as_dict()
                if isinstance(error, StatehouseError)
                else {"code": type(error).__name__, "message": str(error), "context": {}}
            )
        return run

    def list_runs(
        self,
        *,
        jurisdiction: str | None = None,
        state: RunState | str | None = None,
        since: datetime | None = None,
    ) -> list[IngestRun]:
        """Runs matching the filters, newest first."""
        wanted_state = RunState.parse(state, RunState.PENDING) if state is not None else None
        cutoff = ensure_utc(since) if since is not None else None
        selected = [
            run
            for run in self._runs.values()
            if (jurisdiction is None or run.jurisdiction == jurisdiction.strip().lower())
            and (wanted_state is None or run.state is wanted_state)
            and (cutoff is None or run.started_at >= cutoff)
        ]
        return sorted(selected, key=lambda r: (r.started_at, r.run_id), reverse=True)

    def summarise(self, *, window: timedelta | None = None) -> RunSummary:
        """Counts by state over the recent window (default: everything)."""
        cutoff = None
        if window is not None:
            cutoff = utc_now(self._clock) - window
        summary = RunSummary({state.value: 0 for state in RunState})
        summary["documents_written"] = 0
        for run in self._runs.values():
            if cutoff is not None and run.started_at < cutoff:
                continue
            summary[run.state.value] += 1
            summary["documents_written"] += run.documents_written
        return summary

    def consecutive_failures(self, jurisdiction: str) -> int:
        """How many runs in a row have failed for this jurisdiction.

        Counted from the newest backwards and stopped by the first
        non-failure, which is what an alerting rule wants: three failures then
        a success is not an incident.
        """
        count = 0
        for run in self.list_runs(jurisdiction=jurisdiction):
            if not run.state.terminal:
                continue
            if run.state is RunState.FAILED:
                count += 1
            else:
                break
        return count

    def __len__(self) -> int:
        return len(self._runs)
