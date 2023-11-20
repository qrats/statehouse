"""Warehouse plans, the lake, search indexing, logging, metrics and alerts."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from statehouse.core.enums import BillStatus, RunState, Severity
from statehouse.core.errors import StorageError
from statehouse.core.models import DocumentVersion, RawFetch, Sponsor, Watermark
from statehouse.load.lake import InMemoryObjectStore, LakeWriter, manifest_key
from statehouse.load.memory import InMemorySink
from statehouse.load.search import (
    INDEX_SETTINGS,
    TEXT_WINDOW,
    SearchIndexer,
    bulk_actions,
    index_document,
)
from statehouse.load.warehouse import TABLE_ORDER, build_upsert_plan, plan_batch
from statehouse.observability.alerts import AlertRules, evaluate_alerts
from statehouse.observability.logging import JsonFormatter, bind, configure_logging, get_logger
from statehouse.observability.metrics import MetricsRegistry, timer
from statehouse.orchestration.runs import RunRegistry
from statehouse.transform.diffing import diff_documents

from .conftest import make_action, make_document

UTC = timezone.utc
NOW = datetime(2024, 3, 15, 12, 0, tzinfo=UTC)


def _rich_document():
    return make_document(
        subjects=["health", "energy"],
        sponsors=[Sponsor(name="Jane Smith", role="primary"), Sponsor(name="Bob Jones")],
        versions=[DocumentVersion(label="as introduced", text="The department shall act.")],
        actions=[make_action(), make_action("2024-02-01", "Reported favorably")],
    )


class TestUpsertPlan:
    def test_the_documents_table_is_always_written(self):
        assert build_upsert_plan(make_document()).statement_for("documents") is not None

    def test_child_tables_appear_only_when_populated(self):
        plan = build_upsert_plan(make_document(versions=[], subjects=[]))
        assert plan.statement_for("document_versions") is None

    def test_a_rich_document_touches_every_child_table(self):
        tables = build_upsert_plan(_rich_document()).tables()
        assert {"document_versions", "document_actions", "document_sponsors", "document_subjects"} <= set(tables)

    def test_parents_are_written_before_children(self):
        tables = build_upsert_plan(_rich_document()).tables()
        assert tables == sorted(tables, key=TABLE_ORDER.index)

    def test_child_rows_are_replaced_rather_than_merged(self):
        plan = build_upsert_plan(_rich_document())
        assert ("document_sponsors", plan.document_id) in plan.deletes

    def test_the_documents_row_carries_the_citation(self):
        rows = build_upsert_plan(make_document()).statement_for("documents").rows
        assert rows[0]["citation"] == "zz-2023-2024-HB1"

    def test_action_rows_are_in_docket_order(self):
        plan = build_upsert_plan(_rich_document())
        dates = [row["occurred_on"] for row in plan.statement_for("document_actions").rows]
        assert dates == sorted(dates)

    def test_sponsor_rows_keep_their_position(self):
        rows = build_upsert_plan(_rich_document()).statement_for("document_sponsors").rows
        assert [row["position"] for row in rows] == [0, 1]

    def test_a_change_record_is_written_when_a_diff_is_supplied(self):
        diff = diff_documents(None, make_document())
        assert build_upsert_plan(make_document(), diff).statement_for("document_changes")

    def test_no_change_record_is_written_for_an_unchanged_diff(self):
        diff = diff_documents(make_document(), make_document())
        assert build_upsert_plan(make_document(), diff).statement_for("document_changes") is None

    def test_the_row_count_covers_every_statement(self):
        assert build_upsert_plan(_rich_document()).row_count >= 6

    def test_the_conflict_target_for_documents_is_the_id(self):
        statement = build_upsert_plan(make_document()).statement_for("documents")
        assert statement.conflict_target == ("document_id",)

    def test_a_batch_plans_in_id_order(self):
        plans = plan_batch([(make_document("HB2"), None), (make_document("HB1"), None)])
        assert [p.document_id for p in plans] == sorted(p.document_id for p in plans)


class TestLake:
    def test_a_fetch_is_stored_under_its_archive_key(self, source):
        store = InMemoryObjectStore()
        writer = LakeWriter(store)
        key = writer.write(RawFetch(source=source, body=b"payload"))
        assert store.exists(key)

    def test_identical_bytes_are_written_once(self, source):
        store = InMemoryObjectStore()
        writer = LakeWriter(store)
        writer.write(RawFetch(source=source, body=b"payload"))
        writer.write(RawFetch(source=source, body=b"payload"))
        assert writer.written == 1 and writer.skipped == 1

    def test_different_bytes_get_different_keys(self, source):
        writer = LakeWriter(InMemoryObjectStore())
        first = writer.write(RawFetch(source=source, body=b"one"))
        second = writer.write(RawFetch(source=source, body=b"two"))
        assert first != second

    def test_metadata_records_the_source(self, source):
        store = InMemoryObjectStore()
        key = LakeWriter(store).write(RawFetch(source=source, body=b"payload"))
        assert store.metadata[key]["jurisdiction"] == "zz"

    def test_an_empty_body_is_refused(self, source):
        with pytest.raises(StorageError):
            LakeWriter(InMemoryObjectStore()).write(RawFetch(source=source, body=b""))

    def test_a_custom_prefix_is_applied(self, source):
        writer = LakeWriter(InMemoryObjectStore(), prefix="quarantine")
        assert writer.write(RawFetch(source=source, body=b"x")).startswith("quarantine/")

    def test_manifest_keys_are_partitioned(self):
        assert manifest_key("CA", "2024-03-15", "run1") == "manifests/ca/2024-03-15/run1.json"

    def test_stats_report_what_happened(self, source):
        writer = LakeWriter(InMemoryObjectStore())
        writer.write(RawFetch(source=source, body=b"payload"))
        assert writer.stats() == {"written": 1, "skipped": 0, "objects": 1}


class TestSearchIndexing:
    def test_the_indexed_document_carries_the_citation(self):
        assert index_document(make_document())["citation"] == "zz-2023-2024-HB1"

    def test_the_body_comes_from_the_latest_version(self):
        assert "department" in index_document(_rich_document())["body"]

    def test_a_document_without_text_indexes_an_empty_body(self):
        assert index_document(make_document(versions=[]))["body"] == ""

    def test_long_text_is_truncated_to_the_window(self):
        long_doc = make_document(
            versions=[DocumentVersion(label="v", text="word " * (TEXT_WINDOW // 2))]
        )
        assert len(index_document(long_doc)["body"]) <= TEXT_WINDOW

    def test_sponsor_names_are_indexed_as_keywords(self):
        assert index_document(_rich_document())["sponsor_names"] == ["Jane Smith", "Bob Jones"]

    def test_the_action_count_is_indexed(self):
        assert index_document(_rich_document())["action_count"] == 2

    def test_bulk_actions_pair_a_header_with_a_source(self):
        payload = bulk_actions([make_document()], "idx")
        assert len(payload) == 2 and payload[0]["index"]["_index"] == "idx"

    def test_the_bulk_id_is_the_document_id(self):
        document = make_document()
        payload = bulk_actions([document], "idx")
        assert payload[0]["index"]["_id"] == document.document_id

    def test_the_mapping_declares_the_analyser(self):
        assert INDEX_SETTINGS["mappings"]["properties"]["title"]["analyzer"] == "legislative"

    def test_the_indexer_batches(self):
        indexer = SearchIndexer(index="idx", batch_size=2)
        indexer.submit([make_document(f"HB{n}") for n in range(1, 6)])
        assert indexer.batches == 3

    def test_the_indexer_reports_how_many_it_accepted(self):
        indexer = SearchIndexer(index="idx")
        assert indexer.submit([make_document("HB1"), make_document("HB2")]) == 2

    def test_a_failing_client_is_counted_not_raised(self):
        class Broken:
            def bulk(self, body):
                raise RuntimeError("no")

        indexer = SearchIndexer(index="idx", client=Broken())
        assert indexer.submit([make_document()]) == 0 and indexer.failures == 1

    def test_an_index_name_is_required(self):
        with pytest.raises(ValueError):
            SearchIndexer(index="")


class TestInMemorySink:
    def test_an_upsert_is_readable(self):
        sink = InMemorySink()
        document = make_document()
        sink.upsert(document, diff_documents(None, document))
        assert sink.fetch(document.document_id) is document

    def test_an_unknown_id_reads_as_nothing(self):
        assert InMemorySink().fetch("nope") is None

    def test_documents_can_be_filtered_by_jurisdiction(self):
        sink = InMemorySink()
        for code in ("zz", "yy"):
            document = make_document(jurisdiction=code)
            sink.upsert(document, diff_documents(None, document))
        assert len(sink.by_jurisdiction("yy")) == 1

    def test_notable_diffs_are_separated(self):
        sink = InMemorySink()
        first = make_document()
        sink.upsert(first, diff_documents(None, first))
        second = make_document(title="A slightly revised title")
        sink.upsert(second, diff_documents(first, second))
        assert len(sink.notable_diffs()) == 1

    def test_rows_written_accumulates(self):
        sink = InMemorySink()
        document = _rich_document()
        sink.upsert(document, diff_documents(None, document))
        assert sink.rows_written() > 0


class TestLogging:
    def test_records_serialise_as_json(self):
        record = logging.LogRecord("n", logging.INFO, "p", 1, "hello", None, None)
        assert json.loads(JsonFormatter().format(record))["message"] == "hello"

    def test_extra_context_becomes_a_top_level_key(self):
        record = logging.LogRecord("n", logging.INFO, "p", 1, "hello", None, None)
        record.jurisdiction = "ca"
        assert json.loads(JsonFormatter().format(record))["jurisdiction"] == "ca"

    def test_the_service_name_is_included(self):
        record = logging.LogRecord("n", logging.INFO, "p", 1, "hello", None, None)
        assert json.loads(JsonFormatter(service="s").format(record))["service"] == "s"

    def test_the_level_is_lowercased(self):
        record = logging.LogRecord("n", logging.WARNING, "p", 1, "hi", None, None)
        assert json.loads(JsonFormatter().format(record))["level"] == "warning"

    def test_configuring_twice_leaves_one_handler(self):
        configure_logging("INFO")
        configure_logging("INFO")
        assert len(logging.getLogger().handlers) == 1

    def test_a_bound_logger_keeps_its_context(self):
        adapter = get_logger("t", jurisdiction="ca")
        assert adapter.extra["jurisdiction"] == "ca"

    def test_binding_merges_context(self):
        adapter = bind(get_logger("t", a=1), b=2)
        assert adapter.extra == {"a": 1, "b": 2}

    def test_the_plain_formatter_is_selectable(self):
        configure_logging("INFO", fmt="text")
        assert not isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)


class TestMetrics:
    def test_counters_accumulate(self):
        registry = MetricsRegistry()
        registry.increment("pages")
        assert registry.increment("pages") == 2.0

    def test_labels_separate_series(self):
        registry = MetricsRegistry()
        registry.increment("pages", jurisdiction="ca")
        registry.increment("pages", jurisdiction="tx")
        assert len(registry.counters) == 2

    def test_a_negative_increment_is_rejected(self):
        with pytest.raises(ValueError):
            MetricsRegistry().increment("pages", -1)

    def test_gauges_replace_rather_than_add(self):
        registry = MetricsRegistry()
        registry.gauge("lag", 10)
        assert registry.gauge("lag", 4) == 4.0

    def test_percentiles_use_the_nearest_rank(self):
        registry = MetricsRegistry()
        for value in (1, 2, 3, 4, 5):
            registry.observe("latency", value)
        assert registry.percentile("latency", 0.5) == 3

    def test_a_percentile_of_nothing_is_none(self):
        assert MetricsRegistry().percentile("latency", 0.5) is None

    def test_a_fraction_outside_the_range_is_rejected(self):
        with pytest.raises(ValueError):
            MetricsRegistry().observe("l", 1) or MetricsRegistry().percentile("l", 0.0)

    def test_the_snapshot_covers_every_kind(self):
        registry = MetricsRegistry()
        registry.increment("a")
        registry.gauge("b", 1)
        registry.observe("c", 1)
        kinds = {sample.kind for sample in registry.snapshot()}
        assert kinds == {"counter", "gauge", "summary"}

    def test_resetting_clears_everything(self):
        registry = MetricsRegistry()
        registry.increment("a")
        registry.reset()
        assert registry.snapshot() == []

    def test_the_timer_records_an_observation(self):
        registry = MetricsRegistry()
        ticks = iter([0.0, 2.5])
        with timer(registry, "duration", clock=lambda: next(ticks)):
            pass
        assert registry.observations["duration"] == [2.5]


class TestAlerts:
    def _watermark(self, code: str, age: timedelta) -> Watermark:
        return Watermark(jurisdiction=code, stream="default", observed_through=NOW - age)

    def test_a_fresh_jurisdiction_raises_nothing(self, registry, clock):
        marks = [self._watermark(code, timedelta(minutes=5)) for code in registry]
        assert evaluate_alerts(registry.enabled(), marks, RunRegistry(), clock=clock) == []

    def test_a_missing_watermark_is_critical(self, registry, clock):
        alerts = evaluate_alerts([registry["ca"]], [], RunRegistry(), clock=clock)
        assert alerts[0].rule == "never_ingested" and alerts[0].severity is Severity.CRITICAL

    def test_never_ingested_can_be_downgraded_to_silence(self, registry, clock):
        rules = AlertRules(never_ingested_is_critical=False)
        assert evaluate_alerts([registry["ca"]], [], RunRegistry(), rules=rules, clock=clock) == []

    def test_moderate_staleness_warns(self, registry, clock):
        marks = [self._watermark("ca", timedelta(hours=8))]
        alerts = evaluate_alerts([registry["ca"]], marks, RunRegistry(), clock=clock)
        assert alerts[0].severity is Severity.WARN

    def test_severe_staleness_is_critical(self, registry, clock):
        marks = [self._watermark("ca", timedelta(days=2))]
        alerts = evaluate_alerts([registry["ca"]], marks, RunRegistry(), clock=clock)
        assert alerts[0].severity is Severity.CRITICAL

    def test_disabled_jurisdictions_are_never_alerted_on(self, registry, clock):
        assert evaluate_alerts([registry["mo"]], [], RunRegistry(), clock=clock) == []

    def test_repeated_failures_warn(self, registry, clock):
        runs = RunRegistry(clock)
        for index in range(2):
            runs.start(f"r{index}", "ca")
            runs.finish(f"r{index}", RunState.FAILED)
            clock.advance(minutes=1)
        marks = [self._watermark("ca", timedelta(minutes=1))]
        alerts = evaluate_alerts([registry["ca"]], marks, runs, clock=clock)
        assert any(alert.rule == "consecutive_failures" for alert in alerts)

    def test_many_failures_are_critical(self, registry, clock):
        runs = RunRegistry(clock)
        for index in range(5):
            runs.start(f"r{index}", "ca")
            runs.finish(f"r{index}", RunState.FAILED)
            clock.advance(minutes=1)
        marks = [self._watermark("ca", timedelta(minutes=1))]
        alerts = evaluate_alerts([registry["ca"]], marks, runs, clock=clock)
        assert alerts[0].severity is Severity.CRITICAL

    def test_alerts_are_ordered_worst_first(self, registry, clock):
        marks = [self._watermark("ca", timedelta(hours=8)), self._watermark("tx", timedelta(days=3))]
        alerts = evaluate_alerts(
            [registry["ca"], registry["tx"]], marks, RunRegistry(), clock=clock
        )
        assert alerts[0].subject == "tx"

    def test_an_alert_serialises(self, registry, clock):
        alerts = evaluate_alerts([registry["ca"]], [], RunRegistry(), clock=clock)
        assert alerts[0].to_dict()["severity"] == "critical"

    def test_inverted_thresholds_are_rejected(self):
        with pytest.raises(ValueError):
            AlertRules(stale_warn_seconds=100, stale_critical_seconds=10)

    def test_inverted_failure_counts_are_rejected(self):
        with pytest.raises(ValueError):
            AlertRules(consecutive_failure_warn=5, consecutive_failure_critical=2)
