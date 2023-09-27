"""Change detection."""

from __future__ import annotations

from datetime import date

from statehouse.core.enums import BillStatus, ChangeKind
from statehouse.core.models import DocumentVersion
from statehouse.transform.diffing import changed_fields, diff_documents, text_similarity

from .conftest import make_action, make_document

LONG_TEXT = (
    "The department shall establish a municipal broadband programme and shall "
    "report annually to the legislature on its progress, cost and coverage."
)
EDITED_TEXT = LONG_TEXT.replace("annually", "quarterly")
REPLACEMENT_TEXT = (
    "Nothing in this chapter authorises a political subdivision to provide "
    "retail telecommunications service to end users."
)


def _versioned(text: str, label: str = "current", day: str = "2024-01-10"):
    return make_document(
        versions=[
            DocumentVersion(label=label, text=text, published_on=date.fromisoformat(day))
        ]
    )


class TestTextSimilarity:
    def test_identical_text_is_one(self):
        assert text_similarity(LONG_TEXT, LONG_TEXT) == 1.0

    def test_two_empty_texts_are_identical(self):
        assert text_similarity("", "") == 1.0

    def test_one_empty_text_is_completely_dissimilar(self):
        assert text_similarity(LONG_TEXT, "") == 0.0

    def test_reflowed_whitespace_does_not_count(self):
        assert text_similarity(LONG_TEXT, LONG_TEXT.replace(" ", "\n  ")) == 1.0

    def test_a_small_edit_stays_close_to_one(self):
        assert text_similarity(LONG_TEXT, EDITED_TEXT) > 0.9

    def test_a_replacement_scores_low(self):
        assert text_similarity(LONG_TEXT, REPLACEMENT_TEXT) < 0.5


class TestChangedFields:
    def test_an_unchanged_document_reports_nothing(self):
        assert changed_fields(make_document(), make_document()) == []

    def test_a_title_change_is_reported(self):
        assert "title" in changed_fields(make_document(), make_document(title="Something else"))

    def test_a_status_change_is_reported(self):
        assert "status" in changed_fields(
            make_document(), make_document(status=BillStatus.REPORTED)
        )

    def test_sponsor_order_alone_is_not_a_change(self):
        from statehouse.core.models import Sponsor

        left = make_document(sponsors=[Sponsor(name="A B"), Sponsor(name="C D")])
        right = make_document(sponsors=[Sponsor(name="C D"), Sponsor(name="A B")])
        assert "sponsors" not in changed_fields(left, right)

    def test_subject_case_alone_is_not_a_change(self):
        left = make_document(subjects=["health"])
        right = make_document(subjects=["Health"])
        assert "subjects" not in changed_fields(left, right)


class TestDiffDocuments:
    def test_no_previous_observation_is_new(self):
        diff = diff_documents(None, make_document())
        assert diff.kind is ChangeKind.NEW and diff.is_change

    def test_a_new_document_counts_its_actions(self):
        assert diff_documents(None, make_document()).new_actions == 1

    def test_two_identical_observations_are_unchanged(self):
        diff = diff_documents(make_document(), make_document())
        assert diff.kind is ChangeKind.UNCHANGED and not diff.is_change

    def test_an_unchanged_diff_is_not_notable(self):
        assert not diff_documents(make_document(), make_document()).notable

    def test_a_title_edit_is_metadata_only(self):
        diff = diff_documents(make_document(), make_document(title="A revised title here"))
        assert diff.kind is ChangeKind.METADATA_ONLY

    def test_metadata_only_changes_are_not_pushed(self):
        assert not diff_documents(make_document(), make_document(title="Revised title")).notable

    def test_a_new_docket_line_alone_is_metadata_only(self):
        previous = make_document()
        current = make_document(
            actions=[make_action(), make_action("2024-02-02", "Motion to reconsider tabled")]
        )
        diff = diff_documents(previous, current)
        assert diff.kind is ChangeKind.METADATA_ONLY and diff.new_actions == 1

    def test_a_status_advance_is_reported(self):
        diff = diff_documents(make_document(), make_document(status=BillStatus.REPORTED))
        assert diff.kind is ChangeKind.STATUS_ADVANCED

    def test_a_status_advance_is_notable(self):
        assert diff_documents(make_document(), make_document(status=BillStatus.REPORTED)).notable

    def test_a_status_regression_is_reported(self):
        diff = diff_documents(
            make_document(status=BillStatus.REPORTED), make_document(status=BillStatus.INTRODUCED)
        )
        assert diff.kind is ChangeKind.STATUS_REGRESSED

    def test_leaving_a_terminal_status_is_always_a_regression(self):
        diff = diff_documents(
            make_document(status=BillStatus.ENACTED), make_document(status=BillStatus.SIGNED)
        )
        assert diff.kind is ChangeKind.STATUS_REGRESSED

    def test_leaving_a_terminal_status_is_annotated(self):
        diff = diff_documents(
            make_document(status=BillStatus.DEAD), make_document(status=BillStatus.IN_COMMITTEE)
        )
        assert any("terminal" in note for note in diff.notes)

    def test_a_move_to_withdrawn_is_its_own_kind(self):
        diff = diff_documents(make_document(), make_document(status=BillStatus.WITHDRAWN))
        assert diff.kind is ChangeKind.WITHDRAWN

    def test_withdrawal_takes_priority_over_a_text_change(self):
        previous = _versioned(LONG_TEXT)
        current = _versioned(REPLACEMENT_TEXT)
        current.status = BillStatus.WITHDRAWN
        assert diff_documents(previous, current).kind is ChangeKind.WITHDRAWN

    def test_a_small_text_edit_is_a_revision(self):
        diff = diff_documents(_versioned(LONG_TEXT), _versioned(EDITED_TEXT))
        assert diff.kind is ChangeKind.TEXT_REVISED

    def test_a_wholesale_replacement_is_a_substitution(self):
        diff = diff_documents(_versioned(LONG_TEXT), _versioned(REPLACEMENT_TEXT))
        assert diff.kind is ChangeKind.SUPERSEDED

    def test_the_substitution_threshold_is_configurable(self):
        diff = diff_documents(
            _versioned(LONG_TEXT), _versioned(EDITED_TEXT), substitution_threshold=0.99
        )
        assert diff.kind is ChangeKind.SUPERSEDED

    def test_a_substitution_carries_a_note(self):
        diff = diff_documents(_versioned(LONG_TEXT), _versioned(REPLACEMENT_TEXT))
        assert diff.notes

    def test_whitespace_only_reformatting_is_not_a_text_change(self):
        diff = diff_documents(_versioned(LONG_TEXT), _versioned(LONG_TEXT.replace(" ", "  ")))
        assert diff.kind is ChangeKind.UNCHANGED

    def test_gaining_a_first_version_is_a_text_change(self):
        diff = diff_documents(make_document(versions=[]), _versioned(LONG_TEXT))
        assert diff.kind in (ChangeKind.TEXT_REVISED, ChangeKind.SUPERSEDED)

    def test_the_similarity_is_reported(self):
        diff = diff_documents(_versioned(LONG_TEXT), _versioned(EDITED_TEXT))
        assert diff.text_similarity is not None and 0.0 < diff.text_similarity <= 1.0

    def test_a_text_change_outranks_a_status_change(self):
        previous = _versioned(LONG_TEXT)
        current = _versioned(EDITED_TEXT)
        current.status = BillStatus.REPORTED
        assert diff_documents(previous, current).kind is ChangeKind.TEXT_REVISED

    def test_the_diff_serialises(self):
        payload = diff_documents(None, make_document()).to_dict()
        assert payload["kind"] == "new" and payload["document_id"]

    def test_the_previous_and_current_status_are_recorded(self):
        diff = diff_documents(make_document(), make_document(status=BillStatus.REPORTED))
        assert diff.previous_status is BillStatus.INTRODUCED
        assert diff.current_status is BillStatus.REPORTED
