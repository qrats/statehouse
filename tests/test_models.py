"""Canonical record construction and validation."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from statehouse.core.enums import BillStatus, Chamber, FetchMethod, RunState, Severity, SponsorRole
from statehouse.core.errors import ValidationError
from statehouse.core.models import (
    Action,
    Document,
    DocumentVersion,
    IngestRun,
    QualityFinding,
    RawFetch,
    SourceRef,
    Sponsor,
    Watermark,
    to_jsonable,
)

from .conftest import make_action, make_document

UTC = timezone.utc


class TestSourceRef:
    def test_jurisdiction_is_lowercased(self, ):
        assert SourceRef(jurisdiction="CA", url="https://x.test/a").jurisdiction == "ca"

    def test_a_url_is_required(self):
        with pytest.raises(ValidationError):
            SourceRef(jurisdiction="ca", url="")

    def test_a_jurisdiction_is_required(self):
        with pytest.raises(ValidationError):
            SourceRef(jurisdiction="", url="https://x.test/a")

    def test_fetched_at_is_normalised_to_utc(self):
        ref = SourceRef(jurisdiction="ca", url="https://x.test/a", fetched_at=datetime(2024, 1, 1))
        assert ref.fetched_at.tzinfo is not None


class TestRawFetch:
    def test_a_digest_is_computed_when_absent(self, source):
        assert RawFetch(source=source, body=b"payload").digest

    def test_a_string_body_is_encoded(self, source):
        assert RawFetch(source=source, body="payload").size_bytes == 7

    def test_headers_are_lowercased(self, source):
        fetch = RawFetch(source=source, body=b"x", headers={"Content-Type": "text/html"})
        assert "content-type" in fetch.headers

    def test_the_archive_key_is_partitioned_by_day(self, source):
        key = RawFetch(source=source, body=b"payload").archive_key()
        assert key.startswith("raw/zz/2024-03-15/")

    def test_identical_bytes_share_an_archive_key(self, source):
        left = RawFetch(source=source, body=b"same")
        right = RawFetch(source=source, body=b"same")
        assert left.archive_key() == right.archive_key()


class TestSponsor:
    def test_whitespace_is_collapsed(self):
        assert Sponsor(name="  Jane   Smith ").name == "Jane Smith"

    def test_a_name_is_required(self):
        with pytest.raises(ValidationError):
            Sponsor(name="   ")

    def test_party_is_upper_cased_and_bounded(self):
        assert Sponsor(name="Jane Smith", party="democrat").party == "DEMO"

    def test_an_unknown_role_falls_back(self):
        assert Sponsor(name="Jane Smith", role="nonsense").role is SponsorRole.UNKNOWN


class TestAction:
    def test_a_datetime_is_reduced_to_a_date(self):
        assert Action(occurred_on=datetime(2024, 1, 5, 9), description="Filed").occurred_on == date(
            2024, 1, 5
        )

    def test_a_description_is_required(self):
        with pytest.raises(ValidationError):
            Action(occurred_on=date(2024, 1, 5), description="  ")

    def test_a_negative_sequence_is_rejected(self):
        with pytest.raises(ValidationError):
            Action(occurred_on=date(2024, 1, 5), description="Filed", sequence=-1)

    def test_sort_key_orders_by_date_then_sequence(self):
        early = make_action("2024-01-01", "A", sequence=1)
        late = make_action("2024-01-01", "B", sequence=0)
        assert sorted([early, late], key=Action.sort_key)[0] is late


class TestDocumentVersion:
    def test_hashes_are_derived_from_the_text(self):
        version = DocumentVersion(label="intro", text="body")
        assert version.semantic_hash and version.byte_hash

    def test_an_unlabelled_version_gets_a_placeholder(self):
        assert DocumentVersion(label="", text="body").label == "unlabelled"

    def test_length_reports_the_text_size(self):
        assert DocumentVersion(label="a", text="abcd").length == 4


class TestDocument:
    def test_subjects_are_sorted_and_deduplicated(self):
        doc = make_document(subjects=["health", "Health", "energy"])
        assert doc.subjects == ["energy", "health"]

    def test_blank_subjects_are_dropped(self):
        assert make_document(subjects=["", "  ", "energy"]).subjects == ["energy"]

    def test_an_identifier_is_required(self):
        with pytest.raises(ValidationError):
            make_document(identifier="")

    def test_a_session_is_required(self):
        with pytest.raises(ValidationError):
            make_document(session="  ")

    def test_last_action_before_introduction_is_rejected(self):
        with pytest.raises(ValidationError):
            make_document(introduced_on=date(2024, 3, 1), last_action_on=date(2024, 1, 1))

    def test_the_id_is_stable_across_equal_documents(self):
        assert make_document().document_id == make_document().document_id

    def test_the_citation_reflects_jurisdiction_and_session(self):
        assert make_document().citation == "zz-2023-2024-HB1"

    def test_latest_version_prefers_the_newest_published(self):
        doc = make_document(
            versions=[
                DocumentVersion(label="a", text="old", published_on=date(2024, 1, 1)),
                DocumentVersion(label="b", text="new", published_on=date(2024, 2, 1)),
            ]
        )
        assert doc.latest_version.label == "b"

    def test_latest_version_falls_back_to_the_last_listed(self):
        doc = make_document(
            versions=[DocumentVersion(label="a", text="x"), DocumentVersion(label="b", text="y")]
        )
        assert doc.latest_version.label == "b"

    def test_latest_version_is_none_without_versions(self):
        assert make_document(versions=[]).latest_version is None

    def test_metadata_fingerprint_moves_with_the_title(self):
        assert make_document().metadata_fingerprint() != make_document(
            title="A different title entirely"
        ).metadata_fingerprint()

    def test_metadata_fingerprint_ignores_the_docket(self):
        with_actions = make_document(actions=[make_action(), make_action("2024-02-02", "Reported")])
        assert with_actions.metadata_fingerprint() == make_document().metadata_fingerprint()

    def test_with_status_returns_a_new_document(self):
        original = make_document()
        moved = original.with_status(BillStatus.ENACTED)
        assert original.status is BillStatus.INTRODUCED and moved.status is BillStatus.ENACTED

    def test_to_dict_carries_the_derived_fields(self):
        payload = make_document().to_dict()
        assert payload["document_id"] and payload["citation"]

    def test_ordered_actions_sorts_by_date(self):
        doc = make_document(
            actions=[make_action("2024-03-01", "Later"), make_action("2024-01-01", "Earlier")]
        )
        assert doc.ordered_actions()[0].description == "Earlier"


class TestQualityFinding:
    def test_error_and_above_are_blocking(self):
        assert QualityFinding(check="c", severity=Severity.ERROR, message="m").blocking

    def test_warn_is_not_blocking(self):
        assert not QualityFinding(check="c", severity=Severity.WARN, message="m").blocking

    def test_a_check_name_is_required(self):
        with pytest.raises(ValidationError):
            QualityFinding(check="", severity=Severity.WARN, message="m")


class TestIngestRun:
    def test_duration_is_none_until_it_finishes(self):
        run = IngestRun(run_id="r", jurisdiction="ca", started_at=datetime(2024, 1, 1, tzinfo=UTC))
        assert run.duration_seconds is None

    def test_duration_is_measured_once_finished(self):
        run = IngestRun(run_id="r", jurisdiction="ca", started_at=datetime(2024, 1, 1, tzinfo=UTC))
        run.finish(RunState.SUCCEEDED, datetime(2024, 1, 1, 0, 1, tzinfo=UTC))
        assert run.duration_seconds == 60.0

    def test_blocking_findings_are_filtered(self):
        run = IngestRun(
            run_id="r",
            jurisdiction="ca",
            started_at=datetime(2024, 1, 1, tzinfo=UTC),
            findings=[
                QualityFinding(check="a", severity=Severity.WARN, message="m"),
                QualityFinding(check="b", severity=Severity.ERROR, message="m"),
            ],
        )
        assert [f.check for f in run.blocking_findings] == ["b"]

    def test_a_run_id_is_required(self):
        with pytest.raises(ValidationError):
            IngestRun(run_id="", jurisdiction="ca", started_at=datetime(2024, 1, 1, tzinfo=UTC))


class TestWatermark:
    def test_the_key_joins_jurisdiction_and_stream(self):
        assert Watermark(jurisdiction="CA", stream="bills").key == "ca:bills"

    def test_an_unset_watermark_is_stale(self):
        mark = Watermark(jurisdiction="ca", stream="bills")
        assert mark.is_stale(datetime(2024, 1, 1, tzinfo=UTC), 3600)

    def test_a_recent_watermark_is_fresh(self):
        mark = Watermark(
            jurisdiction="ca", stream="bills", observed_through=datetime(2024, 1, 1, tzinfo=UTC)
        )
        assert not mark.is_stale(datetime(2024, 1, 1, 0, 30, tzinfo=UTC), 3600)

    def test_an_old_watermark_is_stale(self):
        mark = Watermark(
            jurisdiction="ca", stream="bills", observed_through=datetime(2024, 1, 1, tzinfo=UTC)
        )
        assert mark.is_stale(datetime(2024, 1, 2, tzinfo=UTC), 3600)

    def test_a_negative_revision_is_rejected(self):
        with pytest.raises(ValidationError):
            Watermark(jurisdiction="ca", stream="bills", revision=-1)

    def test_a_blank_stream_defaults(self):
        assert Watermark(jurisdiction="ca", stream="").stream == "default"


class TestSerialisation:
    def test_enums_serialise_to_their_values(self):
        assert to_jsonable(Chamber.UPPER) == "upper"

    def test_datetimes_serialise_to_iso_z(self):
        assert to_jsonable(datetime(2024, 1, 1, tzinfo=UTC)) == "2024-01-01T00:00:00Z"

    def test_dates_serialise_to_iso(self):
        assert to_jsonable(date(2024, 1, 1)) == "2024-01-01"

    def test_nested_dataclasses_serialise(self):
        payload = to_jsonable(make_document())
        assert payload["sponsors"][0]["name"] == "Jane Smith"

    def test_a_document_round_trips_through_json_types(self):
        payload = Document.to_dict(make_document())
        assert isinstance(payload["subjects"], list)
        assert payload["kind"] == "bill"

    def test_fetch_method_serialises(self):
        assert to_jsonable(FetchMethod.API) == "api"
