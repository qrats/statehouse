"""The CLI surface and the task-level runner."""

from __future__ import annotations

import json
from datetime import date

import pytest

from statehouse.cli import EXIT_GATE_BLOCKED, EXIT_OK, build_parser, main
from statehouse.config.jurisdictions import default_registry
from statehouse.core.enums import BillStatus
from statehouse.load.memory import InMemorySink
from statehouse.load.search import SearchIndexer
from statehouse.runner import (
    BatchStore,
    batch_store,
    load_batch,
    run_spider,
    transform_batch,
    watermark_store,
)
from statehouse.version import VERSION, version_tuple

RECORDS = [
    {
        "identifier": "HB 1",
        "title": "An act relating to municipal broadband",
        "url": "https://legislature.example.gov/bill/HB1",
        "actions": [{"date": "2024-01-05", "description": "Introduced"}],
        "sponsors": "Rep. Jane Smith (D-14)",
        "subjects": ["Technology"],
        "versions": [{"label": "as introduced", "text": "The department shall act."}],
    },
    {
        "identifier": "SB 2",
        "title": "An act relating to rural energy credits",
        "url": "https://legislature.example.gov/bill/SB2",
        "actions": [
            {"date": "2024-01-06", "description": "Introduced"},
            {"date": "2024-02-06", "description": "Referred to Committee on Energy"},
        ],
        "sponsors": "Sen. Bob Jones (R)",
        "subjects": ["Energy"],
    },
]


class TestVersion:
    def test_the_version_is_a_dotted_string(self):
        assert VERSION.count(".") == 2

    def test_the_version_tuple_is_comparable(self):
        assert version_tuple() >= (0, 1, 0)


class TestParser:
    def test_a_subcommand_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_the_version_subcommand_parses(self):
        assert build_parser().parse_args(["version"]).command == "version"

    def test_backfill_takes_a_jurisdiction(self):
        assert build_parser().parse_args(["backfill", "ca"]).jurisdiction == "ca"

    def test_check_requires_a_jurisdiction(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["check", "f.json", "--session", "2024"])


class TestCli:
    def test_version_prints_the_version(self, capsys):
        assert main(["version"]) == EXIT_OK
        assert VERSION in capsys.readouterr().out

    def test_version_can_be_machine_readable(self, capsys):
        main(["--json", "version"])
        assert json.loads(capsys.readouterr().out)["version"] == VERSION

    def test_jurisdictions_lists_every_entry(self, capsys):
        assert main(["jurisdictions"]) == EXIT_OK
        assert len(capsys.readouterr().out.strip().splitlines()) == len(default_registry())

    def test_jurisdictions_can_be_filtered_to_enabled(self, capsys):
        main(["--json", "jurisdictions", "--enabled-only"])
        payload = json.loads(capsys.readouterr().out)
        assert all(row["enabled"] for row in payload)

    def test_jurisdictions_can_be_filtered_by_tag(self, capsys):
        main(["--json", "jurisdictions", "--tag", "api"])
        payload = json.loads(capsys.readouterr().out)
        assert payload and all("api" in row["tags"] for row in payload)

    def test_an_unmatched_tag_says_so(self, capsys):
        main(["jurisdictions", "--tag", "nonexistent"])
        assert "no jurisdictions matched" in capsys.readouterr().out

    def test_plan_selects_never_ingested_jurisdictions(self, capsys):
        main(["--json", "plan"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["selected"]

    def test_plan_respects_the_parallel_cap(self, capsys):
        main(["--json", "plan", "--max-parallel", "3"])
        assert len(json.loads(capsys.readouterr().out)["selected"]) == 3

    def test_plan_respects_the_browser_cap(self, capsys):
        main(["--json", "plan", "--max-parallel", "50", "--max-browser", "1"])
        assert json.loads(capsys.readouterr().out)["browser_slots_used"] <= 1

    def test_backfill_prints_slices(self, capsys):
        main(["--json", "backfill", "ca", "--through", "2024-06-30", "--from-year", "2023"])
        assert json.loads(capsys.readouterr().out)["slices"]

    def test_backfill_honours_a_limit(self, capsys):
        main(["--json", "backfill", "ca", "--through", "2024-06-30", "--limit", "2"])
        assert len(json.loads(capsys.readouterr().out)["slices"]) == 2

    def test_backfill_names_skipped_sessions(self, capsys):
        main(["--json", "backfill", "ca", "--through", "2024-06-30", "--from-year", "2023"])
        assert json.loads(capsys.readouterr().out)["skipped_sessions"]

    def test_an_unknown_jurisdiction_exits_non_zero(self, capsys):
        assert main(["backfill", "nowhere"]) != EXIT_OK

    def test_check_passes_a_clean_batch(self, tmp_path, capsys):
        path = tmp_path / "records.json"
        path.write_text(json.dumps(RECORDS), encoding="utf-8")
        code = main(["--json", "check", str(path), "--jurisdiction", "zz", "--session", "2023-2024"])
        assert code == EXIT_OK and json.loads(capsys.readouterr().out)["passed"]

    def test_check_blocks_a_bad_batch(self, tmp_path, capsys):
        path = tmp_path / "records.json"
        path.write_text(json.dumps([{"identifier": "H", "title": ""}]), encoding="utf-8")
        code = main(["check", str(path), "--jurisdiction", "zz", "--session", "2023-2024"])
        assert code == EXIT_GATE_BLOCKED

    def test_check_reports_unnormalisable_records(self, tmp_path, capsys):
        path = tmp_path / "records.json"
        path.write_text(json.dumps([{"title": "no identifier"}]), encoding="utf-8")
        main(["--json", "check", str(path), "--jurisdiction", "zz", "--session", "2023-2024"])
        assert json.loads(capsys.readouterr().out)["unnormalisable"]

    def test_check_accepts_a_single_object(self, tmp_path, capsys):
        path = tmp_path / "record.json"
        path.write_text(json.dumps(RECORDS[0]), encoding="utf-8")
        main(["--json", "check", str(path), "--jurisdiction", "zz", "--session", "2023-2024"])
        assert json.loads(capsys.readouterr().out)["documents"] == 1

    def test_a_missing_file_exits_non_zero(self):
        assert main(["check", "/nowhere/x.json", "--jurisdiction", "zz", "--session", "2024"]) != EXIT_OK


class TestBatchStore:
    def test_a_batch_round_trips(self):
        from .conftest import make_document

        store = BatchStore()
        store.put("k", [make_document()])
        assert len(store.get("k")) == 1

    def test_an_unknown_key_reads_as_empty(self):
        assert BatchStore().get("nope") == []

    def test_dropping_removes_a_batch(self):
        from .conftest import make_document

        store = BatchStore()
        store.put("k", [make_document()])
        store.drop("k")
        assert len(store) == 0


class TestRunner:
    def test_records_become_documents(self, sample_jurisdiction, clock):
        outcome = run_spider(sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock)
        assert outcome.documents_seen == 2

    def test_the_batch_is_retrievable_by_key(self, sample_jurisdiction, clock):
        outcome = run_spider(sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock)
        assert len(batch_store().get(outcome.batch_key)) == 2

    def test_unusable_records_are_counted_not_fatal(self, sample_jurisdiction, clock):
        outcome = run_spider(
            sample_jurisdiction,
            records=[*RECORDS, {"title": "no identifier"}],
            session="2023-2024",
            clock=clock,
        )
        assert outcome.documents_seen == 2 and outcome.fetch_failures == 1

    def test_the_observation_boundary_comes_from_the_clock(self, sample_jurisdiction, clock):
        outcome = run_spider(sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock)
        assert outcome.observed_through == clock.now()

    def test_the_session_defaults_to_the_current_one(self, sample_jurisdiction, clock):
        outcome = run_spider(sample_jurisdiction, records=RECORDS, clock=clock)
        documents = batch_store().get(outcome.batch_key)
        assert documents[0].session == sample_jurisdiction.session_label(clock.now().year)

    def test_transform_deduplicates(self, sample_jurisdiction, clock):
        outcome = run_spider(
            sample_jurisdiction, records=RECORDS + RECORDS, session="2023-2024", clock=clock
        )
        assert transform_batch("zz", outcome.batch_key).document_count == 2

    def test_transform_reports_the_gate_verdict(self, sample_jurisdiction, clock):
        outcome = run_spider(sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock)
        assert transform_batch("zz", outcome.batch_key).passed

    def test_transform_blocks_a_bad_batch(self, sample_jurisdiction, clock):
        bad = [{"identifier": "HB 1", "title": ""}]
        outcome = run_spider(sample_jurisdiction, records=bad, session="2023-2024", clock=clock)
        assert not transform_batch("zz", outcome.batch_key).passed

    def test_loading_writes_new_documents(self, sample_jurisdiction, clock):
        sink = InMemorySink()
        outcome = run_spider(sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock)
        transform_batch("zz", outcome.batch_key)
        assert load_batch("zz", outcome.batch_key, sink=sink).written == 2

    def test_a_second_identical_load_writes_nothing(self, sample_jurisdiction, clock):
        sink = InMemorySink()
        for _ in range(2):
            outcome = run_spider(
                sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock
            )
            transform_batch("zz", outcome.batch_key)
            result = load_batch("zz", outcome.batch_key, sink=sink)
        assert result.written == 0 and result.unchanged == 2

    def test_a_status_change_is_written_and_counted_as_notable(self, sample_jurisdiction, clock):
        sink = InMemorySink()
        first = run_spider(sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock)
        transform_batch("zz", first.batch_key)
        load_batch("zz", first.batch_key, sink=sink)

        moved = json.loads(json.dumps(RECORDS))
        moved[0]["actions"].append({"date": "2024-03-01", "description": "Reported favorably"})
        second = run_spider(sample_jurisdiction, records=moved, session="2023-2024", clock=clock)
        transform_batch("zz", second.batch_key)
        result = load_batch("zz", second.batch_key, sink=sink)
        assert result.written == 1 and result.notable_changes == 1

    def test_the_stored_status_reflects_the_docket(self, sample_jurisdiction, clock):
        sink = InMemorySink()
        outcome = run_spider(sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock)
        transform_batch("zz", outcome.batch_key)
        load_batch("zz", outcome.batch_key, sink=sink)
        statuses = {document.status for document in sink.all_documents()}
        assert BillStatus.IN_COMMITTEE in statuses

    def test_changed_documents_are_indexed(self, sample_jurisdiction, clock):
        indexer = SearchIndexer(index="idx")
        outcome = run_spider(sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock)
        transform_batch("zz", outcome.batch_key)
        result = load_batch("zz", outcome.batch_key, sink=InMemorySink(), indexer=indexer)
        assert result.indexed == 2

    def test_the_batch_is_dropped_after_loading(self, sample_jurisdiction, clock):
        outcome = run_spider(sample_jurisdiction, records=RECORDS, session="2023-2024", clock=clock)
        transform_batch("zz", outcome.batch_key)
        load_batch("zz", outcome.batch_key, sink=InMemorySink())
        assert batch_store().get(outcome.batch_key) == []

    def test_the_watermark_store_is_reachable(self):
        assert watermark_store() is watermark_store()
