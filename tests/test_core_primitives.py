"""Clock, hashing, ids and enums."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from statehouse.core.clock import FrozenClock, SystemClock, ensure_utc, isoformat, utc_now
from statehouse.core.enums import BillStatus, Chamber, FetchMethod, RunState, Severity
from statehouse.core.hashing import (
    content_digest,
    field_digest,
    normalise_for_digest,
    semantic_digest,
    short_digest,
)
from statehouse.core.ids import canonical_citation, document_id, parse_bill_number, run_id, slugify

UTC = timezone.utc


class TestClock:
    def test_system_clock_is_timezone_aware(self):
        assert SystemClock().now().tzinfo is not None

    def test_frozen_clock_does_not_move_on_its_own(self):
        clock = FrozenClock(datetime(2024, 1, 1, tzinfo=UTC))
        assert clock.now() == clock.now()

    def test_advance_moves_and_returns_the_new_time(self):
        clock = FrozenClock(datetime(2024, 1, 1, tzinfo=UTC))
        assert clock.advance(hours=3) == datetime(2024, 1, 1, 3, tzinfo=UTC)

    def test_naive_input_is_treated_as_utc(self):
        assert ensure_utc(datetime(2024, 1, 1, 5)) == datetime(2024, 1, 1, 5, tzinfo=UTC)

    def test_offset_input_is_converted(self):
        eastern = timezone(timedelta(hours=-5))
        assert ensure_utc(datetime(2024, 1, 1, 0, tzinfo=eastern)).hour == 5

    def test_isoformat_drops_microseconds_and_uses_z(self):
        moment = datetime(2024, 1, 1, 6, 30, 15, 123456, tzinfo=UTC)
        assert isoformat(moment) == "2024-01-01T06:30:15Z"

    def test_utc_now_honours_an_injected_clock(self):
        clock = FrozenClock(datetime(2030, 5, 5, tzinfo=UTC))
        assert utc_now(clock).year == 2030


class TestHashing:
    def test_text_and_its_utf8_bytes_agree(self):
        assert content_digest("hello") == content_digest(b"hello")

    def test_different_bytes_differ(self):
        assert content_digest("a") != content_digest("b")

    def test_semantic_digest_ignores_whitespace_runs(self):
        assert semantic_digest("a  b\n\nc") == semantic_digest("a b c")

    def test_semantic_digest_ignores_a_printed_on_line(self):
        left = "Section 1. The department shall act. Printed on March 4, 2024"
        right = "Section 1. The department shall act. Printed on March 5, 2024"
        assert semantic_digest(left) == semantic_digest(right)

    def test_semantic_digest_ignores_a_session_token(self):
        left = "Body text sessionid=abc123"
        right = "Body text sessionid=zzz999"
        assert semantic_digest(left) == semantic_digest(right)

    def test_semantic_digest_notices_a_real_edit(self):
        assert semantic_digest("shall act") != semantic_digest("may act")

    def test_normalisation_folds_case_and_soft_hyphens(self):
        assert normalise_for_digest("Broad­band") == "broadband"

    def test_field_digest_is_order_independent(self):
        assert field_digest({"a": 1, "b": 2}) == field_digest({"b": 2, "a": 1})

    def test_field_digest_treats_none_and_empty_alike(self):
        assert field_digest({"title": None}) == field_digest({"title": ""})

    def test_field_digest_sorts_list_values(self):
        assert field_digest({"s": ["b", "a"]}) == field_digest({"s": ["a", "b"]})

    def test_field_digest_can_be_limited_to_named_fields(self):
        values = {"title": "t", "noise": "changes"}
        first = field_digest(values, ["title"])
        values["noise"] = "different"
        assert field_digest(values, ["title"]) == first

    def test_short_digest_truncates(self):
        assert len(short_digest(content_digest("x"), 8)) == 8

    def test_short_digest_rejects_a_non_positive_length(self):
        with pytest.raises(ValueError):
            short_digest("abcdef", 0)


class TestIds:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("HB 1234", ("HB", 1234, "")),
            ("H.B. 1234", ("HB", 1234, "")),
            ("hb1234", ("HB", 1234, "")),
            ("SJR 7", ("SJR", 7, "")),
            ("HB 1234-A", ("HB", 1234, "A")),
        ],
    )
    def test_bill_numbers_parse(self, raw, expected):
        assert parse_bill_number(raw) == expected

    @pytest.mark.parametrize("raw", ["", "an act relating to fish", "1234", "----"])
    def test_non_designators_do_not_parse(self, raw):
        assert parse_bill_number(raw) is None

    def test_citation_is_stable_across_spellings(self):
        assert canonical_citation("CA", "2023-2024", "A.B. 1234") == canonical_citation(
            "ca", "2023-2024", "ab1234"
        )

    def test_citation_falls_back_for_unparseable_designators(self):
        assert canonical_citation("ca", "2024", "Budget Item 5") == "ca-2024-BUDGET-ITEM-5"

    def test_document_id_is_deterministic(self):
        assert document_id("ca", "2023-2024", "AB1") == document_id("ca", "2023-2024", "AB1")

    def test_document_id_separates_kinds(self):
        assert document_id("ca", "2023-2024", "AB1", kind="bill") != document_id(
            "ca", "2023-2024", "AB1", kind="resolution"
        )

    def test_document_id_separates_sessions(self):
        assert document_id("ca", "2021-2022", "AB1") != document_id("ca", "2023-2024", "AB1")

    def test_run_id_is_stable_for_a_retry(self):
        assert run_id("ca", "manual__2024-01-01", "fetch") == run_id(
            "ca", "manual__2024-01-01", "fetch"
        )

    def test_run_id_differs_per_task(self):
        assert run_id("ca", "r1", "fetch") != run_id("ca", "r1", "load")

    def test_slugify_handles_punctuation_only_input(self):
        assert slugify("!!!") == "unknown"

    def test_slugify_trims_to_the_limit_without_a_trailing_hyphen(self):
        assert not slugify("a b c d e f g", max_length=5).endswith("-")


class TestEnums:
    @pytest.mark.parametrize("raw", ["IN COMMITTEE", "in-committee", "in_committee"])
    def test_status_parsing_is_forgiving(self, raw):
        assert BillStatus.parse(raw) is BillStatus.IN_COMMITTEE

    def test_parse_returns_the_default_for_junk(self):
        assert BillStatus.parse("nonsense", BillStatus.UNKNOWN) is BillStatus.UNKNOWN

    def test_parse_raises_without_a_default(self):
        with pytest.raises(ValueError):
            Chamber.parse("nonsense")

    def test_members_compare_equal_to_their_values(self):
        assert FetchMethod.BROWSER == "browser"

    def test_severity_ranks_ascend(self):
        ranks = [s.rank for s in (Severity.INFO, Severity.WARN, Severity.ERROR, Severity.CRITICAL)]
        assert ranks == sorted(ranks)

    def test_run_state_terminality(self):
        assert RunState.SUCCEEDED.terminal
        assert not RunState.RUNNING.terminal
