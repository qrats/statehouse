"""Sponsors, subjects, normalisation and deduplication."""

from __future__ import annotations

from datetime import date

import pytest

from statehouse.core.enums import BillStatus, Chamber, DocumentKind, SponsorRole
from statehouse.core.errors import ValidationError
from statehouse.core.models import DocumentVersion, Sponsor
from statehouse.transform.dedupe import (
    completeness_score,
    dedupe_batch,
    merge_actions,
    merge_documents,
)
from statehouse.transform.normalize import (
    infer_chamber,
    normalise_action,
    normalise_document,
    normalise_versions,
)
from statehouse.transform.sponsors import merge_sponsors, parse_sponsor, parse_sponsor_list
from statehouse.transform.subjects import normalise_subject, normalise_subjects, subject_aliases

from .conftest import make_action, make_document


class TestSponsorParsing:
    def test_a_plain_name_parses(self):
        assert parse_sponsor("Jane Smith").name == "Jane Smith"

    def test_a_title_is_stripped(self):
        assert parse_sponsor("Rep. Jane Smith").name == "Jane Smith"

    def test_a_title_sets_the_chamber(self):
        assert parse_sponsor("Sen. Jane Smith").chamber is Chamber.UPPER

    def test_party_and_district_parse_together(self):
        sponsor = parse_sponsor("Smith, Jane (R-14)")
        assert sponsor.party == "R" and sponsor.district == "14"

    def test_a_bare_party_parses(self):
        assert parse_sponsor("Jane Smith (D)").party == "D"

    def test_a_spelled_out_party_normalises(self):
        assert parse_sponsor("Jane Smith (Democrat)").party == "D"

    def test_a_district_word_parses(self):
        assert parse_sponsor("Jane Smith, District 22").district == "22"

    def test_a_committee_is_recognised(self):
        assert parse_sponsor("Committee on Ways and Means").role is SponsorRole.COMMITTEE

    def test_a_trailing_role_is_honoured(self):
        assert parse_sponsor("Jane Smith - primary").role is SponsorRole.PRIMARY

    @pytest.mark.parametrize("raw", ["", "  ", "--", "None", "n/a", "vacant"])
    def test_placeholders_parse_to_nothing(self, raw):
        assert parse_sponsor(raw) is None

    def test_the_name_is_titlecased(self):
        assert parse_sponsor("SMITH, JANE").name == "Smith, Jane"

    def test_a_list_promotes_the_first_to_primary(self):
        sponsors = parse_sponsor_list("Smith; Jones; Lee")
        assert sponsors[0].role is SponsorRole.PRIMARY
        assert sponsors[1].role is SponsorRole.COSPONSOR

    def test_a_list_without_promotion_leaves_roles_unknown(self):
        assert parse_sponsor_list("Smith; Jones", primary_first=False)[0].role is SponsorRole.UNKNOWN

    def test_an_already_split_list_is_accepted(self):
        assert len(parse_sponsor_list(["Smith", "Jones"])) == 2

    def test_placeholders_are_dropped_from_a_list(self):
        assert len(parse_sponsor_list("Smith; --; Jones")) == 2

    def test_a_default_chamber_is_applied(self):
        assert parse_sponsor_list("Smith", default_chamber=Chamber.UPPER)[0].chamber is Chamber.UPPER


class TestSponsorMerge:
    def test_blanks_are_filled_from_the_incoming_entry(self):
        merged = merge_sponsors(
            [Sponsor(name="Jane Smith")], [Sponsor(name="Jane Smith", party="R", district="14")]
        )
        assert merged[0].party == "R" and merged[0].district == "14"

    def test_a_known_value_is_not_overwritten(self):
        merged = merge_sponsors(
            [Sponsor(name="Jane Smith", party="D")], [Sponsor(name="Jane Smith", party="R")]
        )
        assert merged[0].party == "D"

    def test_a_primary_is_never_demoted(self):
        merged = merge_sponsors(
            [Sponsor(name="Jane Smith", role=SponsorRole.PRIMARY)],
            [Sponsor(name="Jane Smith", role=SponsorRole.COSPONSOR)],
        )
        assert merged[0].role is SponsorRole.PRIMARY

    def test_an_unknown_role_is_upgraded(self):
        merged = merge_sponsors(
            [Sponsor(name="Jane Smith")],
            [Sponsor(name="Jane Smith", role=SponsorRole.COSPONSOR)],
        )
        assert merged[0].role is SponsorRole.COSPONSOR

    def test_matching_is_case_insensitive(self):
        merged = merge_sponsors([Sponsor(name="Jane Smith")], [Sponsor(name="jane smith")])
        assert len(merged) == 1

    def test_newcomers_are_appended_in_order(self):
        merged = merge_sponsors([Sponsor(name="A B")], [Sponsor(name="C D"), Sponsor(name="E F")])
        assert [s.name for s in merged] == ["A B", "C D", "E F"]


class TestSubjects:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Health", "health"),
            ("PUBLIC HEALTH", "health"),
            ("Motor Vehicles", "transportation"),
            ("Campaign Finance", "elections"),
            ("Workers Compensation", "labor"),
            ("HEALTH--MEDICAID--ELIGIBILITY", "health"),
        ],
    )
    def test_source_vocabulary_maps_onto_canonical_tags(self, raw, expected):
        assert normalise_subject(raw) == expected

    def test_an_unmappable_subject_returns_nothing(self):
        assert normalise_subject("Zorbing") is None

    def test_an_empty_subject_returns_nothing(self):
        assert normalise_subject("   ") is None

    def test_a_cell_splits_and_maps(self):
        canonical, unmapped = normalise_subjects("Health; Motor Vehicles; Zorbing")
        assert canonical == ["health", "transportation"] and unmapped == ["Zorbing"]

    def test_canonical_tags_are_deduplicated(self):
        canonical, _ = normalise_subjects("Health; Public Health")
        assert canonical == ["health"]

    def test_aliases_are_discoverable(self):
        assert "medicaid" in subject_aliases("health")

    def test_an_unknown_canonical_has_no_aliases(self):
        assert subject_aliases("nonsense") == ()


class TestNormaliseAction:
    def test_a_docket_row_becomes_an_action(self):
        action = normalise_action({"date": "1/5/2024", "description": "Introduced"})
        assert action.occurred_on == date(2024, 1, 5)

    def test_an_undated_row_is_dropped(self):
        assert normalise_action({"description": "Introduced"}) is None

    def test_a_row_with_no_description_is_dropped(self):
        assert normalise_action({"date": "2024-01-05", "description": "  "}) is None

    def test_alternative_key_names_are_accepted(self):
        assert normalise_action({"action_date": "2024-01-05", "action": "Filed"}) is not None

    def test_the_sequence_defaults_to_the_row_index(self):
        assert normalise_action({"date": "2024-01-05", "description": "Filed"}, sequence=4).sequence == 4


class TestNormaliseVersions:
    def test_versions_are_ordered_by_publication(self):
        versions = normalise_versions(
            [
                {"label": "b", "text": "later", "published_on": "2024-02-01"},
                {"label": "a", "text": "earlier", "published_on": "2024-01-01"},
            ]
        )
        assert [v.label for v in versions] == ["a", "b"]

    def test_undated_versions_come_last_in_source_order(self):
        versions = normalise_versions(
            [
                {"label": "u1", "text": "x"},
                {"label": "d", "text": "y", "published_on": "2024-01-01"},
                {"label": "u2", "text": "z"},
            ]
        )
        assert [v.label for v in versions] == ["d", "u1", "u2"]

    def test_versions_without_text_are_dropped(self):
        assert normalise_versions([{"label": "a"}, {"label": "b", "text": "  "}]) == []

    def test_no_versions_yields_an_empty_list(self):
        assert normalise_versions(None) == []


class TestInferChamber:
    @pytest.mark.parametrize(
        "identifier,expected",
        [
            ("HB1234", Chamber.LOWER),
            ("SB1234", Chamber.UPPER),
            ("AB99", Chamber.LOWER),
            ("SJR7", Chamber.UPPER),
            ("HCR3", Chamber.LOWER),
            ("SF120", Chamber.UPPER),
        ],
    )
    def test_prefixes_resolve(self, identifier, expected):
        assert infer_chamber(identifier) is expected

    def test_an_unprefixed_designator_uses_the_default(self):
        assert infer_chamber("1234", default=Chamber.JOINT) is Chamber.JOINT

    def test_an_empty_designator_uses_the_default(self):
        assert infer_chamber("") is Chamber.UNKNOWN


class TestNormaliseDocument:
    def _raw(self, **overrides):
        base = {
            "identifier": "HB 42",
            "title": "  An act relating to broadband  ",
            "summary": "Requires the department to act.",
            "url": "https://x.test/bill/42?utm_source=n",
            "subjects": ["Health", "Zorbing"],
            "sponsors": "Rep. Jane Smith (D-14); Sen. Bob Jones (R)",
            "actions": [
                {"date": "2024-01-05", "description": "Introduced"},
                {"date": "2024-02-01", "description": "Referred to Committee on Health"},
            ],
            "versions": [{"label": "as introduced", "text": "The department shall act."}],
        }
        base.update(overrides)
        return base

    def test_a_record_without_an_identifier_is_rejected(self, clock):
        with pytest.raises(ValidationError):
            normalise_document({}, jurisdiction="zz", session="2024", clock=clock)

    def test_the_title_is_cleaned(self, clock):
        doc = normalise_document(self._raw(), jurisdiction="zz", session="2024", clock=clock)
        assert doc.title == "An act relating to broadband"

    def test_the_source_url_is_canonicalised(self, clock):
        doc = normalise_document(self._raw(), jurisdiction="zz", session="2024", clock=clock)
        assert doc.source.url == "https://x.test/bill/42"

    def test_the_chamber_is_inferred_from_the_identifier(self, clock):
        doc = normalise_document(self._raw(), jurisdiction="zz", session="2024", clock=clock)
        assert doc.chamber is Chamber.LOWER

    def test_the_status_is_derived_from_the_docket(self, clock):
        doc = normalise_document(
            self._raw(status="introduced"), jurisdiction="zz", session="2024", clock=clock
        )
        assert doc.status is BillStatus.IN_COMMITTEE

    def test_the_declared_status_is_a_fallback_only(self, clock):
        doc = normalise_document(
            self._raw(actions=[], status="enacted"),
            jurisdiction="zz",
            session="2024",
            clock=clock,
        )
        assert doc.status is BillStatus.ENACTED

    def test_unmappable_subjects_are_left_out(self, clock):
        doc = normalise_document(self._raw(), jurisdiction="zz", session="2024", clock=clock)
        assert doc.subjects == ["health"]

    def test_sponsors_are_parsed_and_ordered(self, clock):
        doc = normalise_document(self._raw(), jurisdiction="zz", session="2024", clock=clock)
        assert doc.sponsors[0].role is SponsorRole.PRIMARY
        assert doc.sponsors[1].name == "Bob Jones"
        assert doc.sponsors[1].chamber is Chamber.UPPER

    def test_the_introduction_date_falls_back_to_the_earliest_action(self, clock):
        doc = normalise_document(self._raw(), jurisdiction="zz", session="2024", clock=clock)
        assert doc.introduced_on == date(2024, 1, 5)

    def test_the_last_action_date_is_the_latest_action(self, clock):
        doc = normalise_document(self._raw(), jurisdiction="zz", session="2024", clock=clock)
        assert doc.last_action_on == date(2024, 2, 1)

    def test_a_resolution_prefix_sets_the_kind(self, clock):
        doc = normalise_document(
            self._raw(identifier="HJR 5"), jurisdiction="zz", session="2024", clock=clock
        )
        assert doc.kind is DocumentKind.RESOLUTION

    def test_an_introduction_after_the_last_action_is_pulled_back(self, clock):
        doc = normalise_document(
            self._raw(introduced_on="2024-06-01"), jurisdiction="zz", session="2024", clock=clock
        )
        assert doc.introduced_on <= doc.last_action_on

    def test_the_observation_time_comes_from_the_clock(self, clock):
        doc = normalise_document(self._raw(), jurisdiction="zz", session="2024", clock=clock)
        assert doc.observed_at == clock.now()

    def test_a_record_with_no_url_has_no_source(self, clock):
        doc = normalise_document(
            self._raw(url=""), jurisdiction="zz", session="2024", clock=clock
        )
        assert doc.source is None

    def test_extras_are_carried_through(self, clock):
        doc = normalise_document(
            self._raw(extras={"bill_id": "abc"}), jurisdiction="zz", session="2024", clock=clock
        )
        assert doc.extras == {"bill_id": "abc"}


class TestDedupe:
    def test_the_richer_observation_scores_higher(self):
        thin = make_document(actions=[], sponsors=[])
        rich = make_document()
        assert completeness_score(rich) > completeness_score(thin)

    def test_actions_union_on_date_and_description(self):
        merged = merge_actions(
            [make_action("2024-01-01", "Introduced")],
            [make_action("2024-01-01", "introduced"), make_action("2024-02-01", "Reported")],
        )
        assert len(merged) == 2

    def test_a_merged_action_keeps_the_more_specific_chamber(self):
        merged = merge_actions(
            [make_action("2024-01-01", "Introduced")],
            [make_action("2024-01-01", "Introduced", chamber=Chamber.LOWER)],
        )
        assert merged[0].chamber is Chamber.LOWER

    def test_merging_across_ids_is_a_programming_error(self):
        with pytest.raises(ValueError):
            merge_documents(make_document("HB1"), make_document("HB2"))

    def test_a_blank_field_is_filled_from_the_secondary(self):
        primary = make_document(summary="")
        secondary = make_document(summary="A summary")
        assert merge_documents(primary, secondary).summary == "A summary"

    def test_subjects_are_unioned(self):
        merged = merge_documents(
            make_document(subjects=["health"]), make_document(subjects=["energy"])
        )
        assert merged.subjects == ["energy", "health"]

    def test_versions_are_unioned_on_their_semantic_hash(self):
        merged = merge_documents(
            make_document(versions=[DocumentVersion(label="a", text="one")]),
            make_document(versions=[DocumentVersion(label="b", text="one")]),
        )
        assert len(merged.versions) == 1

    def test_a_batch_collapses_to_one_row_per_id(self):
        batch = [make_document(), make_document(summary="richer"), make_document("HB2")]
        assert len(dedupe_batch(batch)) == 2

    def test_the_batch_result_is_sorted_by_id(self):
        result = dedupe_batch([make_document("HB2"), make_document("HB1")])
        assert [d.document_id for d in result] == sorted(d.document_id for d in result)

    def test_deduplication_is_order_independent(self):
        thin = make_document(actions=[], sponsors=[])
        rich = make_document()
        assert dedupe_batch([thin, rich])[0].sponsors == dedupe_batch([rich, thin])[0].sponsors

    def test_an_empty_batch_deduplicates_to_nothing(self):
        assert dedupe_batch([]) == []
