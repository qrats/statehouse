"""Deriving canonical status from a docket."""

from __future__ import annotations

import pytest

from statehouse.core.enums import ActionKind, BillStatus, Chamber
from statehouse.transform.status import (
    STATUS_ORDER,
    TERMINAL_STATUSES,
    classify_action,
    derive_status,
    is_advance,
    status_from_action,
    status_rank,
)

from .conftest import make_action


class TestClassification:
    @pytest.mark.parametrize(
        "description,expected",
        [
            ("Introduced and first reading", ActionKind.FILING),
            ("Referred to Committee on Health", ActionKind.REFERRAL),
            ("Hearing scheduled for 3 March", ActionKind.HEARING),
            ("Reported out of committee, do pass", ActionKind.COMMITTEE_VOTE),
            ("Passed the House 88-12", ActionKind.FLOOR_VOTE),
            ("Sent to the Governor", ActionKind.TRANSMITTAL),
            ("Signed by the Governor", ActionKind.EXECUTIVE_ACTION),
            ("Amendment 3 offered", ActionKind.AMENDMENT_OFFERED),
            ("Motion to reconsider tabled", ActionKind.UNKNOWN),
        ],
    )
    def test_docket_lines_classify(self, description, expected):
        assert classify_action(description) is expected

    def test_an_empty_description_is_unknown(self):
        assert classify_action("") is ActionKind.UNKNOWN


class TestStatusFromAction:
    @pytest.mark.parametrize(
        "description,expected",
        [
            ("Prefiled for the 2024 session", BillStatus.PREFILED),
            ("Introduced", BillStatus.INTRODUCED),
            ("Referred to Committee on Rules", BillStatus.IN_COMMITTEE),
            ("Reported favorably", BillStatus.REPORTED),
            ("Placed on the calendar", BillStatus.ON_FLOOR),
            ("Enrolled and sent for signature", BillStatus.ENROLLED),
            ("Sent to the Governor", BillStatus.SENT_TO_EXECUTIVE),
            ("Signed by the Governor", BillStatus.SIGNED),
            ("Vetoed by the Governor", BillStatus.VETOED),
            ("Veto overridden by both chambers", BillStatus.VETO_OVERRIDDEN),
            ("Chaptered, Chapter 214", BillStatus.ENACTED),
            ("Became law without signature", BillStatus.ENACTED),
            ("Failed to pass", BillStatus.FAILED),
            ("Withdrawn by the sponsor", BillStatus.WITHDRAWN),
            ("Died in committee", BillStatus.DEAD),
            ("Indefinitely postponed", BillStatus.DEAD),
        ],
    )
    def test_lines_imply_a_status(self, description, expected):
        assert status_from_action(description) is expected

    def test_a_procedural_line_implies_nothing(self):
        assert status_from_action("Motion to reconsider tabled") is None

    def test_veto_override_beats_the_plain_veto_pattern(self):
        assert status_from_action("Veto overridden") is BillStatus.VETO_OVERRIDDEN

    def test_a_bare_passage_defaults_to_the_origin_chamber(self):
        assert status_from_action("Passed") is BillStatus.PASSED_ORIGIN

    def test_passage_in_the_origin_chamber_is_first_passage(self):
        assert (
            status_from_action("Passed", chamber=Chamber.LOWER, origin=Chamber.LOWER)
            is BillStatus.PASSED_ORIGIN
        )

    def test_passage_in_the_other_chamber_is_second_passage(self):
        assert (
            status_from_action("Passed", chamber=Chamber.UPPER, origin=Chamber.LOWER)
            is BillStatus.PASSED_SECOND
        )

    def test_the_chamber_can_be_inferred_from_the_wording(self):
        assert (
            status_from_action("Passed the Senate", origin=Chamber.LOWER)
            is BillStatus.PASSED_SECOND
        )


class TestRanking:
    def test_the_order_is_strictly_increasing(self):
        ranks = [status_rank(status) for status in STATUS_ORDER]
        assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)

    def test_dead_outranks_every_procedural_step(self):
        assert status_rank(BillStatus.DEAD) > status_rank(BillStatus.ENROLLED)

    def test_enacted_outranks_signed(self):
        assert status_rank(BillStatus.ENACTED) > status_rank(BillStatus.SIGNED)

    def test_advancing_is_detected(self):
        assert is_advance(BillStatus.INTRODUCED, BillStatus.REPORTED)

    def test_regressing_is_not_an_advance(self):
        assert not is_advance(BillStatus.REPORTED, BillStatus.INTRODUCED)

    def test_the_terminal_set_is_what_it_claims(self):
        assert BillStatus.ENACTED in TERMINAL_STATUSES
        assert BillStatus.IN_COMMITTEE not in TERMINAL_STATUSES


class TestDeriveStatus:
    def test_an_empty_docket_uses_the_fallback(self):
        assert derive_status([], fallback=BillStatus.PREFILED) is BillStatus.PREFILED

    def test_an_unrecognisable_docket_uses_the_fallback(self):
        actions = [make_action("2024-01-01", "Something nobody has words for")]
        assert derive_status(actions, fallback=BillStatus.UNKNOWN) is BillStatus.UNKNOWN

    def test_the_latest_implying_action_wins(self):
        actions = [
            make_action("2024-01-01", "Introduced"),
            make_action("2024-02-01", "Referred to Committee on Health"),
            make_action("2024-03-01", "Reported favorably"),
        ]
        assert derive_status(actions) is BillStatus.REPORTED

    def test_a_later_withdrawal_beats_an_earlier_passage(self):
        actions = [
            make_action("2024-01-01", "Passed the House"),
            make_action("2024-02-01", "Withdrawn by the sponsor"),
        ]
        assert derive_status(actions) is BillStatus.WITHDRAWN

    def test_ordering_does_not_depend_on_input_order(self):
        forwards = [
            make_action("2024-01-01", "Introduced"),
            make_action("2024-03-01", "Signed by the Governor"),
        ]
        assert derive_status(forwards) is derive_status(list(reversed(forwards)))

    def test_same_day_actions_resolve_to_the_higher_rank(self):
        actions = [
            make_action("2024-03-01", "Enrolled", sequence=0),
            make_action("2024-03-01", "Signed by the Governor", sequence=0),
        ]
        assert derive_status(actions) is BillStatus.SIGNED

    def test_an_explicit_resulting_status_is_respected(self):
        actions = [
            make_action(
                "2024-03-01", "Some local procedural phrasing",
                resulting_status=BillStatus.ENACTED,
            )
        ]
        assert derive_status(actions) is BillStatus.ENACTED

    def test_the_origin_chamber_shapes_passage(self):
        actions = [make_action("2024-02-01", "Passed", chamber=Chamber.UPPER)]
        assert derive_status(actions, origin=Chamber.LOWER) is BillStatus.PASSED_SECOND

    def test_a_full_lifecycle_lands_on_enacted(self):
        actions = [
            make_action("2024-01-05", "Introduced"),
            make_action("2024-01-10", "Referred to Committee on Ways and Means"),
            make_action("2024-02-01", "Reported out, do pass"),
            make_action("2024-02-15", "Passed the House 90-10"),
            make_action("2024-03-01", "Passed the Senate 30-5", chamber=Chamber.UPPER),
            make_action("2024-03-10", "Enrolled"),
            make_action("2024-03-15", "Sent to the Governor"),
            make_action("2024-03-20", "Signed by the Governor"),
            make_action("2024-03-25", "Chaptered, Chapter 12"),
        ]
        assert derive_status(actions, origin=Chamber.LOWER) is BillStatus.ENACTED

    def test_a_veto_then_override_lands_on_override(self):
        actions = [
            make_action("2024-03-01", "Vetoed by the Governor"),
            make_action("2024-04-01", "Veto overridden by the House"),
        ]
        assert derive_status(actions) is BillStatus.VETO_OVERRIDDEN

    def test_sequence_breaks_a_same_day_tie_before_rank(self):
        actions = [
            make_action("2024-03-01", "Introduced", sequence=0),
            make_action("2024-03-01", "Referred to Committee on Health", sequence=1),
        ]
        assert derive_status(actions) is BillStatus.IN_COMMITTEE
