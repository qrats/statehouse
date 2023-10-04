"""Quality checks, the gate, and reporting."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from statehouse.core.enums import BillStatus, Chamber, DocumentKind, Severity
from statehouse.core.errors import QualityGateFailed
from statehouse.core.models import QualityFinding, Sponsor
from statehouse.quality.checks import (
    CHECKS,
    check_batch_not_empty,
    check_chamber_known,
    check_dates_plausible,
    check_identifier_present,
    check_no_duplicate_ids,
    check_sponsor_sanity,
    check_status_supported,
    check_terminal_consistency,
    check_title_present,
    run_checks,
)
from statehouse.quality.gate import QualityGate
from statehouse.quality.report import render_text, summarise

from .conftest import make_action, make_document

UTC = timezone.utc


def _codes(findings):
    return {finding.check for finding in findings}


class TestIndividualChecks:
    def test_an_empty_batch_is_noted(self):
        assert _codes(check_batch_not_empty([])) == {"batch_not_empty"}

    def test_a_populated_batch_is_not_noted(self):
        assert check_batch_not_empty([make_document()]) == []

    def test_a_one_character_identifier_is_an_error(self):
        findings = check_identifier_present([make_document(identifier="H")])
        assert findings and findings[0].severity is Severity.ERROR

    def test_a_normal_identifier_passes(self):
        assert check_identifier_present([make_document()]) == []

    def test_a_missing_title_is_an_error(self):
        findings = check_title_present([make_document(title="")])
        assert findings[0].severity is Severity.ERROR

    def test_a_short_title_is_a_warning(self):
        findings = check_title_present([make_document(title="Short")])
        assert findings[0].severity is Severity.WARN

    def test_a_future_date_is_an_error(self):
        doc = make_document(
            actions=[], introduced_on=date(2030, 1, 1), last_action_on=date(2030, 1, 1)
        )
        assert _codes(check_dates_plausible([doc])) == {"dates_plausible"}

    def test_a_nineteenth_century_date_is_an_error(self):
        doc = make_document(actions=[], introduced_on=date(1850, 1, 1))
        assert _codes(check_dates_plausible([doc])) == {"dates_plausible"}

    def test_plausible_dates_pass(self):
        doc = make_document(introduced_on=date(2024, 1, 1), last_action_on=date(2024, 2, 1))
        assert check_dates_plausible([doc]) == []

    def test_a_status_with_no_docket_is_a_warning(self):
        assert _codes(check_status_supported([make_document(actions=[])])) == {"status_supported"}

    def test_an_unknown_status_with_no_docket_is_fine(self):
        doc = make_document(actions=[], status=BillStatus.UNKNOWN)
        assert check_status_supported([doc]) == []

    def test_actions_after_a_terminal_action_are_flagged(self):
        doc = make_document(
            status=BillStatus.ENACTED,
            actions=[
                make_action("2024-01-01", "Chaptered", resulting_status=BillStatus.ENACTED),
                make_action("2024-02-01", "Clerical correction"),
            ],
        )
        assert _codes(check_terminal_consistency([doc])) == {"terminal_consistency"}

    def test_a_clean_terminal_docket_passes(self):
        doc = make_document(
            status=BillStatus.ENACTED,
            actions=[make_action("2024-01-01", "Chaptered", resulting_status=BillStatus.ENACTED)],
        )
        assert check_terminal_consistency([doc]) == []

    def test_an_unknown_chamber_on_a_bill_is_a_warning(self):
        doc = make_document(identifier="1234", chamber=Chamber.UNKNOWN)
        assert _codes(check_chamber_known([doc])) == {"chamber_known"}

    def test_a_regulation_may_have_no_chamber(self):
        doc = make_document(
            identifier="REG-1", kind=DocumentKind.REGULATION, chamber=Chamber.UNKNOWN
        )
        assert check_chamber_known([doc]) == []

    def test_a_repeated_id_in_one_batch_is_an_error(self):
        findings = check_no_duplicate_ids([make_document(), make_document()])
        assert findings and findings[0].severity is Severity.ERROR

    def test_distinct_ids_pass(self):
        assert check_no_duplicate_ids([make_document("HB1"), make_document("HB2")]) == []

    def test_two_primary_sponsors_are_a_warning(self):
        doc = make_document(
            sponsors=[
                Sponsor(name="A B", role="primary"),
                Sponsor(name="C D", role="primary"),
            ]
        )
        assert _codes(check_sponsor_sanity([doc])) == {"sponsor_sanity"}

    def test_a_bill_with_no_sponsors_is_informational(self):
        findings = check_sponsor_sanity([make_document(sponsors=[])])
        assert findings[0].severity is Severity.INFO


class TestRunChecks:
    def test_every_registered_check_runs(self):
        assert len(CHECKS) >= 8

    def test_a_clean_batch_produces_nothing(self):
        assert run_checks([make_document()]) == []

    def test_findings_are_ordered_worst_first(self):
        batch = [make_document(title=""), make_document("H")]
        ranks = [finding.severity.rank for finding in run_checks(batch)]
        assert ranks == sorted(ranks, reverse=True)

    def test_the_order_is_stable_across_runs(self):
        batch = [make_document(title=""), make_document("HB2", actions=[])]
        first = [(f.check, f.subject) for f in run_checks(batch)]
        assert first == [(f.check, f.subject) for f in run_checks(batch)]

    def test_a_custom_check_list_is_honoured(self):
        assert run_checks([make_document(title="")], [check_identifier_present]) == []


class TestQualityGate:
    def test_a_clean_batch_passes(self):
        assert QualityGate().evaluate([make_document()]).passed

    def test_a_clean_batch_summarises_as_clean(self):
        assert QualityGate().evaluate([make_document()]).summary() == "clean"

    def test_an_error_blocks_by_default(self):
        decision = QualityGate().evaluate([make_document(title="")])
        assert not decision.passed and "title_present" in decision.blocked_by

    def test_a_tolerated_error_does_not_block(self):
        assert QualityGate(max_errors=5).evaluate([make_document(title="")]).passed

    def test_a_rate_allowance_admits_a_small_proportion(self):
        batch = [make_document(f"HB{n}") for n in range(1, 101)]
        batch[0].title = ""
        assert QualityGate(max_errors=0, max_error_rate=0.05).evaluate(batch).passed

    def test_a_rate_allowance_still_blocks_a_large_proportion(self):
        batch = [make_document(f"HB{n}", title="") for n in range(1, 11)]
        assert not QualityGate(max_errors=0, max_error_rate=0.05).evaluate(batch).passed

    def test_a_waived_check_cannot_block(self):
        gate = QualityGate(waived_checks=frozenset({"title_present"}))
        assert gate.evaluate([make_document(title="")]).passed

    def test_a_waived_finding_is_still_reported(self):
        gate = QualityGate(waived_checks=frozenset({"title_present"}))
        decision = gate.evaluate([make_document(title="")])
        assert any("waived" in finding.message for finding in decision.findings)

    def test_a_waived_finding_is_demoted_to_a_warning(self):
        gate = QualityGate(waived_checks=frozenset({"title_present"}))
        decision = gate.evaluate([make_document(title="")])
        assert decision.worst_severity is Severity.WARN

    def test_counts_are_reported_by_severity(self):
        decision = QualityGate(max_errors=99).evaluate([make_document(title="")])
        assert decision.counts["error"] == 1

    def test_the_worst_severity_of_a_clean_batch_is_info(self):
        assert QualityGate().evaluate([make_document()]).worst_severity is Severity.INFO

    def test_enforce_raises_when_blocked(self):
        with pytest.raises(QualityGateFailed):
            QualityGate().enforce([make_document(title="")])

    def test_enforce_returns_the_decision_when_clean(self):
        assert QualityGate().enforce([make_document()]).passed

    def test_a_negative_error_budget_is_rejected(self):
        with pytest.raises(ValueError):
            QualityGate(max_errors=-1)

    def test_a_rate_outside_zero_to_one_is_rejected(self):
        with pytest.raises(ValueError):
            QualityGate(max_error_rate=1.5)

    def test_an_empty_batch_passes(self):
        assert QualityGate().evaluate([]).passed


class TestReport:
    def _report(self):
        findings = [
            QualityFinding(check="a", severity=Severity.ERROR, message="bad", subject="zz-1"),
            QualityFinding(check="b", severity=Severity.WARN, message="odd", subject="zz-2"),
        ]
        return summarise("zz", "run-1", 2, findings)

    def test_severity_counts_are_produced(self):
        assert self._report().by_severity["error"] == 1

    def test_check_counts_are_produced(self):
        assert self._report().by_check == {"a": 1, "b": 1}

    def test_a_report_with_an_error_is_not_clean(self):
        assert not self._report().clean

    def test_a_warning_only_report_is_clean(self):
        report = summarise(
            "zz", "run-1", 1, [QualityFinding(check="b", severity=Severity.WARN, message="odd")]
        )
        assert report.clean

    def test_the_report_serialises(self):
        payload = self._report().to_dict()
        assert payload["run_id"] == "run-1" and len(payload["findings"]) == 2

    def test_rendering_names_the_jurisdiction(self):
        assert "zz" in render_text(self._report())

    def test_rendering_truncates_and_says_so(self):
        findings = [
            QualityFinding(check=f"c{n}", severity=Severity.WARN, message="m") for n in range(30)
        ]
        rendered = render_text(summarise("zz", "r", 30, findings), limit=5)
        assert "and 25 more" in rendered

    def test_rendering_a_clean_report_shows_zero_counts(self):
        assert "error=0" in render_text(summarise("zz", "r", 0, []))
