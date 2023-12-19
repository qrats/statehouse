"""Watermarks, checkpoints, backfill planning, runs and scheduling."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from statehouse.config.jurisdictions import Jurisdiction
from statehouse.core.enums import FetchMethod, RunState, Severity
from statehouse.core.errors import (
    CheckpointCorrupt,
    StatehouseError,
    ValidationError,
    WatermarkConflict,
)
from statehouse.core.models import QualityFinding, Watermark
from statehouse.orchestration.backfill import BackfillPlanner, BackfillSlice
from statehouse.orchestration.checkpoint import Checkpoint, InMemoryCheckpointStore
from statehouse.orchestration.runs import RunRegistry
from statehouse.orchestration.scheduler import IngestScheduler
from statehouse.orchestration.watermark import (
    DEFAULT_SAFETY_LAG,
    InMemoryWatermarkStore,
    advance,
    lag_seconds,
)

UTC = timezone.utc
NOW = datetime(2024, 3, 15, 12, 0, tzinfo=UTC)


class TestWatermarkStore:
    def test_an_unknown_key_reads_as_none(self):
        assert InMemoryWatermarkStore().get("ca", "default") is None

    def test_a_write_is_readable(self):
        store = InMemoryWatermarkStore()
        store.put(Watermark(jurisdiction="ca", stream="default", revision=1))
        assert store.get("ca", "default").revision == 1

    def test_lookup_is_case_insensitive(self):
        store = InMemoryWatermarkStore([Watermark(jurisdiction="ca", stream="default")])
        assert store.get("CA", "default") is not None

    def test_a_stale_write_is_refused(self):
        store = InMemoryWatermarkStore()
        store.put(Watermark(jurisdiction="ca", stream="default", revision=3))
        with pytest.raises(WatermarkConflict):
            store.put(Watermark(jurisdiction="ca", stream="default", revision=3))

    def test_a_newer_revision_is_accepted(self):
        store = InMemoryWatermarkStore()
        store.put(Watermark(jurisdiction="ca", stream="default", revision=1))
        assert store.put(Watermark(jurisdiction="ca", stream="default", revision=2)).revision == 2

    def test_listing_is_sorted_by_key(self):
        store = InMemoryWatermarkStore(
            [
                Watermark(jurisdiction="tx", stream="default"),
                Watermark(jurisdiction="ca", stream="default"),
            ]
        )
        assert [w.jurisdiction for w in store.list_all()] == ["ca", "tx"]


class TestAdvance:
    def test_a_first_advance_starts_at_revision_one(self, clock):
        assert advance(None, jurisdiction="ca", clock=clock).revision == 1

    def test_the_revision_increments(self, clock):
        first = advance(None, jurisdiction="ca", clock=clock)
        assert advance(first, jurisdiction="ca", clock=clock).revision == 2

    def test_the_safety_lag_is_subtracted(self, clock):
        result = advance(None, jurisdiction="ca", observed_through=NOW, clock=clock)
        assert result.observed_through == NOW - DEFAULT_SAFETY_LAG

    def test_the_lag_is_configurable(self, clock):
        result = advance(
            None,
            jurisdiction="ca",
            observed_through=NOW,
            safety_lag=timedelta(minutes=1),
            clock=clock,
        )
        assert result.observed_through == NOW - timedelta(minutes=1)

    def test_the_boundary_never_moves_backwards(self, clock):
        current = advance(None, jurisdiction="ca", observed_through=NOW, clock=clock)
        rewound = advance(
            current,
            jurisdiction="ca",
            observed_through=NOW - timedelta(days=1),
            clock=clock,
        )
        assert rewound.observed_through == current.observed_through

    def test_a_rewind_still_bumps_the_revision(self, clock):
        current = advance(None, jurisdiction="ca", observed_through=NOW, clock=clock)
        rewound = advance(
            current, jurisdiction="ca", observed_through=NOW - timedelta(days=1), clock=clock
        )
        assert rewound.revision == current.revision + 1

    def test_omitting_the_boundary_keeps_it(self, clock):
        current = advance(None, jurisdiction="ca", observed_through=NOW, clock=clock)
        moved = advance(current, jurisdiction="ca", position="page-4", clock=clock)
        assert moved.observed_through == current.observed_through and moved.position == "page-4"

    def test_the_position_is_carried_forward_when_not_given(self, clock):
        current = advance(None, jurisdiction="ca", position="page-2", clock=clock)
        assert advance(current, jurisdiction="ca", clock=clock).position == "page-2"

    def test_lag_is_none_for_an_unset_watermark(self):
        assert lag_seconds(None, NOW) is None

    def test_lag_is_measured_in_seconds(self):
        mark = Watermark(
            jurisdiction="ca", stream="default", observed_through=NOW - timedelta(hours=2)
        )
        assert lag_seconds(mark, NOW) == 7200.0

    def test_lag_never_goes_negative(self):
        mark = Watermark(
            jurisdiction="ca", stream="default", observed_through=NOW + timedelta(hours=2)
        )
        assert lag_seconds(mark, NOW) == 0.0


class TestCheckpoint:
    def test_completed_units_are_removed_from_pending(self):
        checkpoint = Checkpoint(jurisdiction="ca", stream="s", pending=["a", "b"])
        checkpoint.mark_done("a")
        assert checkpoint.pending == ["b"] and checkpoint.completed == ["a"]

    def test_marking_the_same_unit_twice_is_a_no_op(self):
        checkpoint = Checkpoint(jurisdiction="ca", stream="s", pending=["a"])
        checkpoint.mark_done("a")
        checkpoint.mark_done("a")
        assert checkpoint.completed == ["a"]

    def test_enqueue_skips_completed_work(self):
        checkpoint = Checkpoint(jurisdiction="ca", stream="s", completed=["a"])
        checkpoint.enqueue(["a", "b"])
        assert checkpoint.pending == ["b"]

    def test_enqueue_deduplicates(self):
        checkpoint = Checkpoint(jurisdiction="ca", stream="s")
        checkpoint.enqueue(["a", "a", "b"])
        assert checkpoint.pending == ["a", "b"]

    def test_construction_removes_already_completed_pending_units(self):
        checkpoint = Checkpoint(jurisdiction="ca", stream="s", completed=["a"], pending=["a", "b"])
        assert checkpoint.pending == ["b"]

    def test_the_next_unit_is_the_head_of_the_queue(self):
        assert Checkpoint(jurisdiction="ca", stream="s", pending=["a", "b"]).next_unit() == "a"

    def test_an_empty_queue_has_no_next_unit(self):
        assert Checkpoint(jurisdiction="ca", stream="s").next_unit() is None

    def test_exhaustion_is_reported(self):
        assert Checkpoint(jurisdiction="ca", stream="s").exhausted

    def test_a_negative_attempt_is_rejected(self):
        with pytest.raises(ValueError):
            Checkpoint(jurisdiction="ca", stream="s", attempt=-1)

    def test_a_checkpoint_round_trips_through_json(self):
        original = Checkpoint(
            jurisdiction="ca", stream="s", cursor="c", pending=["a"], completed=["b"], attempt=2
        )
        restored = Checkpoint.from_json(original.to_json())
        assert restored.pending == ["a"] and restored.completed == ["b"] and restored.attempt == 2

    def test_invalid_json_is_rejected(self):
        with pytest.raises(CheckpointCorrupt):
            Checkpoint.from_json("{not json")

    def test_a_non_object_payload_is_rejected(self):
        with pytest.raises(CheckpointCorrupt):
            Checkpoint.from_json("[1, 2]")

    def test_a_payload_without_a_jurisdiction_is_rejected(self):
        with pytest.raises(CheckpointCorrupt):
            Checkpoint.from_json('{"schema": 2, "stream": "s"}')

    def test_a_future_schema_is_refused(self):
        with pytest.raises(CheckpointCorrupt):
            Checkpoint.from_json('{"schema": 99, "jurisdiction": "ca"}')

    def test_an_older_schema_is_accepted(self):
        assert Checkpoint.from_json('{"schema": 1, "jurisdiction": "ca"}').jurisdiction == "ca"

    def test_an_unreadable_timestamp_is_rejected(self):
        with pytest.raises(CheckpointCorrupt):
            Checkpoint.from_json('{"schema": 2, "jurisdiction": "ca", "updated_at": "soon"}')

    def test_the_store_round_trips(self):
        store = InMemoryCheckpointStore()
        store.save(Checkpoint(jurisdiction="ca", stream="s", pending=["a"]))
        assert store.load("ca", "s").pending == ["a"]

    def test_the_store_forgets_on_clear(self):
        store = InMemoryCheckpointStore()
        store.save(Checkpoint(jurisdiction="ca", stream="s"))
        store.clear("ca", "s")
        assert store.load("ca", "s") is None


class TestBackfillPlanner:
    def test_a_biennial_plan_covers_every_session(self, sample_jurisdiction):
        plan = BackfillPlanner(chunk_days=180).plan(
            sample_jurisdiction, through=date(2024, 6, 30), from_year=2019
        )
        assert plan.sessions == ["2023-2024", "2021-2022", "2019-2020"]

    def test_the_newest_session_comes_first_by_default(self, sample_jurisdiction):
        plan = BackfillPlanner(chunk_days=365).plan(
            sample_jurisdiction, through=date(2024, 6, 30), from_year=2021
        )
        assert plan.slices[0].session == "2023-2024"

    def test_oldest_first_can_be_asked_for(self, sample_jurisdiction):
        planner = BackfillPlanner(chunk_days=365, newest_first=False)
        plan = planner.plan(sample_jurisdiction, through=date(2024, 6, 30), from_year=2021)
        assert plan.slices[0].session == "2021-2022"

    def test_no_slice_runs_past_the_through_date(self, sample_jurisdiction):
        plan = BackfillPlanner(chunk_days=30).plan(
            sample_jurisdiction, through=date(2024, 6, 30), from_year=2023
        )
        assert max(item.end for item in plan.slices) <= date(2024, 6, 30)

    def test_slices_do_not_overlap(self, sample_jurisdiction):
        plan = BackfillPlanner(chunk_days=30).plan(
            sample_jurisdiction, through=date(2024, 6, 30), from_year=2023
        )
        by_session = [s for s in plan.slices if s.session == "2023-2024"]
        ordered = sorted(by_session, key=lambda s: s.start)
        assert all(b.start > a.end for a, b in zip(ordered, ordered[1:], strict=False))

    def test_skipped_sessions_are_named(self, sample_jurisdiction):
        plan = BackfillPlanner().plan(
            sample_jurisdiction, through=date(2024, 6, 30), from_year=2023
        )
        assert "2019-2020" in plan.skipped_sessions

    def test_completed_labels_are_excluded(self, sample_jurisdiction):
        planner = BackfillPlanner(chunk_days=90)
        plan = planner.plan(sample_jurisdiction, through=date(2024, 6, 30), from_year=2023)
        done = [plan.slices[0].label]
        trimmed = planner.plan(
            sample_jurisdiction, through=date(2024, 6, 30), from_year=2023, completed_labels=done
        )
        assert len(trimmed.slices) == len(plan.slices) - 1

    def test_max_slices_bounds_the_plan(self, sample_jurisdiction):
        planner = BackfillPlanner(chunk_days=7, max_slices=5)
        plan = planner.plan(sample_jurisdiction, through=date(2024, 6, 30), from_year=2019)
        assert len(plan.slices) == 5

    def test_the_total_day_count_is_reported(self, sample_jurisdiction):
        plan = BackfillPlanner(chunk_days=30).plan(
            sample_jurisdiction, through=date(2023, 1, 31), from_year=2023
        )
        assert plan.total_days == 31

    def test_take_bounds_what_a_run_attempts(self, sample_jurisdiction):
        plan = BackfillPlanner(chunk_days=7).plan(
            sample_jurisdiction, through=date(2024, 6, 30), from_year=2023
        )
        assert len(plan.take(3)) == 3

    def test_take_rejects_a_negative_count(self, sample_jurisdiction):
        plan = BackfillPlanner().plan(sample_jurisdiction, through=date(2024, 1, 31))
        with pytest.raises(ValueError):
            plan.take(-1)

    def test_without_drops_completed_slices(self, sample_jurisdiction):
        plan = BackfillPlanner(chunk_days=30).plan(
            sample_jurisdiction, through=date(2023, 6, 30), from_year=2023
        )
        trimmed = plan.without([plan.slices[0].label])
        assert len(trimmed.slices) == len(plan.slices) - 1

    def test_a_non_positive_chunk_size_is_rejected(self):
        with pytest.raises(ValueError):
            BackfillPlanner(chunk_days=0)

    def test_an_inverted_slice_is_rejected(self):
        with pytest.raises(ValidationError):
            BackfillSlice(
                jurisdiction="ca", session="2024", start=date(2024, 2, 1), end=date(2024, 1, 1)
            )

    def test_planning_many_interleaves_by_priority(self, sample_jurisdiction):
        other = Jurisdiction(
            code="yy",
            name="Other",
            timezone="UTC",
            portal_url="https://y.test",
            adapter="other",
            session_pattern="{year}",
            backfill_from_year=2023,
        )
        merged = BackfillPlanner(chunk_days=365).plan_many(
            [sample_jurisdiction, other], through=date(2024, 6, 30)
        )
        assert merged[0].priority >= merged[-1].priority

    def test_planning_many_is_deterministic(self, sample_jurisdiction):
        planner = BackfillPlanner(chunk_days=180)
        first = planner.plan_many([sample_jurisdiction], through=date(2024, 6, 30))
        second = planner.plan_many([sample_jurisdiction], through=date(2024, 6, 30))
        assert [s.label for s in first] == [s.label for s in second]


class TestRunRegistry:
    def test_starting_a_run_opens_it(self, clock):
        registry = RunRegistry(clock)
        assert registry.start("r1", "ca").state is RunState.RUNNING

    def test_starting_twice_returns_the_same_run(self, clock):
        registry = RunRegistry(clock)
        first = registry.start("r1", "ca")
        assert registry.start("r1", "ca") is first

    def test_an_unknown_run_raises(self, clock):
        with pytest.raises(StatehouseError):
            RunRegistry(clock).get("nope")

    def test_counters_accumulate(self, clock):
        registry = RunRegistry(clock)
        registry.start("r1", "ca")
        registry.record("r1", seen=10, written=8)
        registry.record("r1", seen=5, written=1)
        run = registry.get("r1")
        assert run.documents_seen == 15 and run.documents_written == 9

    def test_a_clean_run_closes_as_succeeded(self, clock):
        registry = RunRegistry(clock)
        registry.start("r1", "ca")
        assert registry.finish("r1", RunState.SUCCEEDED).state is RunState.SUCCEEDED

    def test_blocking_findings_downgrade_success_to_partial(self, clock):
        registry = RunRegistry(clock)
        registry.start("r1", "ca")
        registry.record(
            "r1", findings=[QualityFinding(check="c", severity=Severity.ERROR, message="m")]
        )
        assert registry.finish("r1", RunState.SUCCEEDED).state is RunState.PARTIAL

    def test_an_error_is_recorded_on_the_run(self, clock):
        registry = RunRegistry(clock)
        registry.start("r1", "ca")
        run = registry.finish("r1", RunState.FAILED, StatehouseError("boom", host="x"))
        assert run.error["message"] == "boom"

    def test_a_plain_exception_is_recorded_too(self, clock):
        registry = RunRegistry(clock)
        registry.start("r1", "ca")
        run = registry.finish("r1", RunState.FAILED, ValueError("bad"))
        assert run.error["code"] == "ValueError"

    def test_runs_can_be_filtered_by_jurisdiction(self, clock):
        registry = RunRegistry(clock)
        registry.start("r1", "ca")
        registry.start("r2", "tx")
        assert [r.run_id for r in registry.list_runs(jurisdiction="tx")] == ["r2"]

    def test_runs_can_be_filtered_by_state(self, clock):
        registry = RunRegistry(clock)
        registry.start("r1", "ca")
        registry.start("r2", "ca")
        registry.finish("r2", RunState.FAILED)
        assert [r.run_id for r in registry.list_runs(state=RunState.FAILED)] == ["r2"]

    def test_summaries_count_by_state(self, clock):
        registry = RunRegistry(clock)
        registry.start("r1", "ca")
        registry.finish("r1", RunState.SUCCEEDED)
        assert registry.summarise()["succeeded"] == 1

    def test_consecutive_failures_counts_back_only_to_the_last_success(self, clock):
        registry = RunRegistry(clock)
        for index, state in enumerate(
            [RunState.FAILED, RunState.SUCCEEDED, RunState.FAILED, RunState.FAILED]
        ):
            registry.start(f"r{index}", "ca")
            registry.finish(f"r{index}", state)
            clock.advance(minutes=1)
        assert registry.consecutive_failures("ca") == 2

    def test_a_recent_success_clears_the_streak(self, clock):
        registry = RunRegistry(clock)
        for index, state in enumerate([RunState.FAILED, RunState.FAILED, RunState.SUCCEEDED]):
            registry.start(f"r{index}", "ca")
            registry.finish(f"r{index}", state)
            clock.advance(minutes=1)
        assert registry.consecutive_failures("ca") == 0

    def test_consecutive_failures_counts_a_current_streak(self, clock):
        registry = RunRegistry(clock)
        for index in range(3):
            registry.start(f"r{index}", "ca")
            registry.finish(f"r{index}", RunState.FAILED)
            clock.advance(minutes=1)
        assert registry.consecutive_failures("ca") == 3


class TestScheduler:
    def _jurisdiction(self, code: str, method: FetchMethod = FetchMethod.HTTP, **kwargs):
        return Jurisdiction(
            code=code,
            name=code,
            timezone="UTC",
            portal_url=f"https://{code}.test",
            adapter=code,
            method=method,
            **kwargs,
        )

    def test_a_never_ingested_jurisdiction_is_selected_first(self, clock):
        fresh = self._jurisdiction("aa")
        stale = self._jurisdiction("bb")
        marks = [
            Watermark(
                jurisdiction="bb",
                stream="default",
                observed_through=NOW - timedelta(days=2),
            )
        ]
        plan = IngestScheduler().plan([fresh, stale], marks, clock=clock)
        assert plan.codes[0] == "aa"

    def test_a_fresh_jurisdiction_is_deferred(self, clock):
        entry = self._jurisdiction("aa")
        marks = [
            Watermark(
                jurisdiction="aa", stream="default", observed_through=NOW - timedelta(minutes=1)
            )
        ]
        plan = IngestScheduler().plan(
            [
                entry,
            ],
            marks,
            clock=clock,
        )
        assert plan.codes == [] and plan.deferred[0].reason == "within freshness target"

    def test_disabled_jurisdictions_never_appear(self, clock):
        entry = self._jurisdiction("aa", enabled=False)
        plan = IngestScheduler().plan([entry], [], clock=clock)
        assert plan.codes == [] and plan.deferred == []

    def test_the_parallel_cap_is_respected(self, clock):
        entries = [self._jurisdiction(f"j{n}") for n in range(6)]
        plan = IngestScheduler(max_parallel=2).plan(entries, [], clock=clock)
        assert len(plan.selected) == 2

    def test_browser_slots_are_capped_separately(self, clock):
        entries = [self._jurisdiction(f"b{n}", FetchMethod.BROWSER) for n in range(4)]
        plan = IngestScheduler(max_parallel=10, max_browser=1).plan(entries, [], clock=clock)
        assert plan.browser_slots_used == 1

    def test_a_deferred_browser_jurisdiction_is_explained(self, clock):
        entries = [self._jurisdiction(f"b{n}", FetchMethod.BROWSER) for n in range(3)]
        plan = IngestScheduler(max_browser=1).plan(entries, [], clock=clock)
        assert len(plan.deferred) == 2

    def test_http_jurisdictions_are_not_limited_by_the_browser_cap(self, clock):
        entries = [self._jurisdiction(f"h{n}") for n in range(3)]
        plan = IngestScheduler(max_browser=0).plan(entries, [], clock=clock)
        assert len(plan.selected) == 3

    def test_a_larger_lag_outranks_a_smaller_one(self, clock):
        entries = [self._jurisdiction("aa"), self._jurisdiction("bb")]
        marks = [
            Watermark(
                jurisdiction="aa", stream="default", observed_through=NOW - timedelta(hours=2)
            ),
            Watermark(
                jurisdiction="bb", stream="default", observed_through=NOW - timedelta(days=2)
            ),
        ]
        assert IngestScheduler().plan(entries, marks, clock=clock).codes[0] == "bb"

    def test_the_federal_source_is_weighted_up(self, clock):
        federal = self._jurisdiction("us")
        state = self._jurisdiction("aa")
        marks = [
            Watermark(
                jurisdiction=code, stream="default", observed_through=NOW - timedelta(hours=6)
            )
            for code in ("us", "aa")
        ]
        assert IngestScheduler().plan([state, federal], marks, clock=clock).codes[0] == "us"

    def test_a_non_positive_parallel_cap_is_rejected(self):
        with pytest.raises(ValueError):
            IngestScheduler(max_parallel=0)

    def test_ordering_is_deterministic_for_equal_scores(self, clock):
        entries = [self._jurisdiction("bb"), self._jurisdiction("aa")]
        assert IngestScheduler().plan(entries, [], clock=clock).codes == ["aa", "bb"]
