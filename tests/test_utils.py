"""Text, dates, retry and URL helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from statehouse.core.errors import FetchError, PermanentError, RateLimited
from statehouse.utils.dates import (
    chunk_range,
    clamp_date,
    day_range,
    parse_date,
    parse_datetime,
    session_years,
)
from statehouse.utils.retry import RetryPolicy, backoff_delays, call_with_retry, first_success
from statehouse.utils.text import (
    clean_text,
    collapse_whitespace,
    dedent_block,
    iter_sentences,
    split_list,
    strip_leading_enumerator,
    strip_tags,
    titlecase_name,
    truncate,
)
from statehouse.utils.urls import absolutise, canonical_url, registrable_host, same_site

UTC = timezone.utc


class TestText:
    def test_whitespace_runs_collapse(self):
        assert collapse_whitespace("a   b\t c") == "a b c"

    def test_paragraphs_survive_when_asked(self):
        assert collapse_whitespace("a\n\n\n\nb", keep_paragraphs=True) == "a\n\nb"

    def test_tags_become_block_boundaries(self):
        assert "one" in strip_tags("<p>one</p><p>two</p>")
        assert "\n" in strip_tags("<p>one</p><p>two</p>")

    def test_script_content_is_dropped(self):
        assert "alert" not in strip_tags("<div>keep<script>alert(1)</script></div>")

    def test_entities_are_unescaped(self):
        assert "&" in strip_tags("<p>A &amp; B</p>")

    def test_clean_text_folds_typographic_punctuation(self):
        assert clean_text("“quoted”") == '"quoted"'

    def test_clean_text_normalises_line_endings(self):
        assert "\r" not in clean_text("a\r\nb")

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("MCDONALD, JAMES", "McDonald, James"),
            ("o'brien, kate", "O'Brien, Kate"),
            ("smith-jones, ann", "Smith-Jones, Ann"),
            ("JANE VAN DYKE", "Jane van Dyke"),
            ("robert smith jr", "Robert Smith JR"),
        ],
    )
    def test_names_are_titlecased_sensibly(self, raw, expected):
        assert titlecase_name(raw) == expected

    def test_an_empty_name_stays_empty(self):
        assert titlecase_name("   ") == ""

    def test_lists_split_on_several_separators(self):
        assert split_list("a; b, c|d") == ["a", "b", "c", "d"]

    def test_list_splitting_deduplicates_case_insensitively(self):
        assert split_list("Smith; smith") == ["Smith"]

    def test_list_splitting_preserves_order(self):
        assert split_list("z; a") == ["z", "a"]

    def test_truncate_prefers_a_word_boundary(self):
        assert truncate("the quick brown fox", 12).endswith("…")
        assert " fo" not in truncate("the quick brown fox", 12)

    def test_truncate_leaves_short_text_alone(self):
        assert truncate("short", 20) == "short"

    def test_truncate_rejects_a_non_positive_limit(self):
        with pytest.raises(ValueError):
            truncate("x", 0)

    def test_dedent_removes_common_indentation(self):
        assert dedent_block("    a\n    b") == "a\nb"

    def test_sentences_are_split(self):
        assert len(list(iter_sentences("One thing. Two things! Three?"))) == 3

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("(a) The department shall", "The department shall"),
            ("SECTION 3. The department", "The department"),
            ("1. A numbered item", "A numbered item"),
            ("No enumerator here", "No enumerator here"),
        ],
    )
    def test_leading_enumerators_are_stripped(self, line, expected):
        assert strip_leading_enumerator(line) == expected


class TestDates:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2024-01-05", date(2024, 1, 5)),
            ("1/5/2024", date(2024, 1, 5)),
            ("01-05-24", date(2024, 1, 5)),
            ("20240105", date(2024, 1, 5)),
            ("January 5, 2024", date(2024, 1, 5)),
            ("Jan. 5 2024", date(2024, 1, 5)),
            ("5 January 2024", date(2024, 1, 5)),
            ("2024-01-05T09:30:00", date(2024, 1, 5)),
        ],
    )
    def test_the_formats_portals_use_parse(self, raw, expected):
        assert parse_date(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "not a date", "13/45/2024", None])
    def test_unparseable_input_returns_none(self, raw):
        assert parse_date(raw) is None

    def test_a_two_digit_year_in_the_seventies_is_last_century(self):
        assert parse_date("1/5/75").year == 1975

    def test_a_date_object_passes_through(self):
        assert parse_date(date(2024, 1, 5)) == date(2024, 1, 5)

    def test_datetimes_come_back_aware(self):
        assert parse_datetime("2024-01-05T09:30:00Z").tzinfo is not None

    def test_a_bare_date_becomes_midnight(self):
        assert parse_datetime("2024-01-05") == datetime(2024, 1, 5, tzinfo=UTC)

    def test_a_time_component_is_kept(self):
        assert parse_datetime("January 5, 2024 09:30").hour == 9

    def test_day_range_is_inclusive(self):
        assert len(day_range(date(2024, 1, 1), date(2024, 1, 3))) == 3

    def test_an_inverted_day_range_is_empty(self):
        assert day_range(date(2024, 1, 3), date(2024, 1, 1)) == []

    def test_chunking_covers_the_range_exactly_once(self):
        chunks = chunk_range(date(2024, 1, 1), date(2024, 3, 31), 30)
        assert chunks[0][0] == date(2024, 1, 1)
        assert chunks[-1][1] == date(2024, 3, 31)
        assert all(
            later[0] > earlier[1] for earlier, later in zip(chunks, chunks[1:], strict=False)
        )

    def test_the_final_chunk_is_short_rather_than_overshooting(self):
        chunks = chunk_range(date(2024, 1, 1), date(2024, 1, 10), 7)
        assert chunks[-1] == (date(2024, 1, 8), date(2024, 1, 10))

    def test_a_single_day_chunks_to_one(self):
        assert chunk_range(date(2024, 1, 1), date(2024, 1, 1), 30) == [
            (date(2024, 1, 1), date(2024, 1, 1))
        ]

    def test_a_non_positive_chunk_size_is_rejected(self):
        with pytest.raises(ValueError):
            chunk_range(date(2024, 1, 1), date(2024, 2, 1), 0)

    def test_annual_session_years_are_consecutive(self):
        assert session_years(2020, 2023) == [2020, 2021, 2022, 2023]

    def test_biennial_session_years_anchor_on_odd(self):
        assert session_years(2020, 2024, biennial=True) == [2019, 2021, 2023]

    def test_an_inverted_year_range_is_empty(self):
        assert session_years(2024, 2020) == []

    def test_clamping_respects_both_bounds(self):
        assert clamp_date(date(2024, 6, 1), date(2024, 1, 1), date(2024, 3, 1)) == date(2024, 3, 1)

    def test_clamping_ignores_absent_bounds(self):
        assert clamp_date(date(2024, 6, 1)) == date(2024, 6, 1)


class TestRetry:
    def test_the_first_attempt_never_waits(self):
        assert RetryPolicy().delay_for(1) == 0.0

    def test_delays_grow_exponentially(self):
        policy = RetryPolicy(base_seconds=1.0, multiplier=2.0, jitter=0.0)
        assert [policy.delay_for(n) for n in (2, 3, 4)] == [1.0, 2.0, 4.0]

    def test_delays_are_capped(self):
        policy = RetryPolicy(base_seconds=1.0, multiplier=10.0, max_seconds=5.0, jitter=0.0)
        assert policy.delay_for(5) == 5.0

    def test_jitter_is_deterministic_for_a_seed(self):
        policy = RetryPolicy(base_seconds=1.0, jitter=0.5)
        assert backoff_delays(policy, seed=7) == backoff_delays(policy, seed=7)

    def test_a_bad_jitter_is_rejected(self):
        with pytest.raises(ValueError):
            RetryPolicy(jitter=1.0)

    def test_a_transient_failure_is_retried(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise FetchError("nope")
            return "ok"

        assert call_with_retry(flaky, RetryPolicy(max_attempts=4, jitter=0.0)) == "ok"
        assert attempts["n"] == 3

    def test_a_permanent_failure_is_not_retried(self):
        attempts = {"n": 0}

        def broken():
            attempts["n"] += 1
            raise PermanentError("no")

        with pytest.raises(PermanentError):
            call_with_retry(broken, RetryPolicy(max_attempts=4))
        assert attempts["n"] == 1

    def test_the_final_failure_is_re_raised(self):
        with pytest.raises(FetchError):
            call_with_retry(
                lambda: (_ for _ in ()).throw(FetchError("x")), RetryPolicy(max_attempts=2)
            )

    def test_retry_after_overrides_the_computed_backoff(self):
        slept: list[float] = []
        attempts = {"n": 0}

        def limited():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RateLimited("slow down", retry_after_seconds=45.0)
            return "ok"

        call_with_retry(
            limited,
            RetryPolicy(max_attempts=3, base_seconds=1.0, jitter=0.0),
            sleep=slept.append,
        )
        assert slept == [45.0]

    def test_on_retry_is_notified(self):
        seen: list[int] = []
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise FetchError("x")
            return 1

        call_with_retry(
            flaky,
            RetryPolicy(max_attempts=3, jitter=0.0),
            on_retry=lambda attempt, _e, _d: seen.append(attempt),
        )
        assert seen == [2]

    def test_first_success_takes_the_first_that_works(self):
        assert (
            first_success(
                [lambda: (_ for _ in ()).throw(FetchError("a")), lambda: "b"],
                policy=RetryPolicy(max_attempts=1),
            )
            == "b"
        )

    def test_first_success_raises_the_last_error(self):
        with pytest.raises(FetchError):
            first_success(
                [lambda: (_ for _ in ()).throw(FetchError("a"))],
                policy=RetryPolicy(max_attempts=1),
            )

    def test_first_success_needs_something_to_try(self):
        with pytest.raises(ValueError):
            first_success([])


class TestUrls:
    def test_tracking_parameters_are_dropped(self):
        assert canonical_url("https://x.test/a?utm_source=n&id=5") == "https://x.test/a?id=5"

    def test_session_identifiers_are_dropped(self):
        assert canonical_url("https://x.test/a?jsessionid=abc") == "https://x.test/a"

    def test_query_parameters_are_sorted(self):
        assert canonical_url("https://x.test/a?b=2&a=1") == "https://x.test/a?a=1&b=2"

    def test_the_fragment_is_discarded(self):
        assert canonical_url("https://x.test/a#top") == "https://x.test/a"

    def test_the_host_is_lowercased(self):
        assert canonical_url("HTTPS://X.TEST/A") == "https://x.test/A"

    def test_a_default_port_is_removed(self):
        assert canonical_url("https://x.test:443/a") == "https://x.test/a"

    def test_a_non_default_port_is_kept(self):
        assert canonical_url("https://x.test:8443/a") == "https://x.test:8443/a"

    def test_a_trailing_slash_is_removed_from_a_path(self):
        assert canonical_url("https://x.test/a/") == "https://x.test/a"

    def test_the_root_keeps_its_slash(self):
        assert canonical_url("https://x.test/") == "https://x.test/"

    def test_an_empty_url_stays_empty(self):
        assert canonical_url("") == ""

    def test_a_relative_url_is_returned_unchanged(self):
        assert canonical_url("/bill/1") == "/bill/1"

    def test_two_spellings_of_one_page_canonicalise_together(self):
        left = canonical_url("https://X.test/bill/1/?utm_medium=a&x=1#frag")
        right = canonical_url("https://x.test:443/bill/1?x=1")
        assert left == right

    def test_relative_links_resolve_against_the_base(self):
        assert absolutise("https://x.test/a/b", "../c") == "https://x.test/c"

    def test_an_empty_href_resolves_to_nothing(self):
        assert absolutise("https://x.test/a", "") == ""

    def test_the_registrable_host_is_extracted(self):
        assert registrable_host("https://sub.x.test:8080/a") == "sub.x.test"

    def test_subdomains_are_the_same_site(self):
        assert same_site("https://a.x.test/1", "https://b.x.test/2")

    def test_different_domains_are_not(self):
        assert not same_site("https://x.test/1", "https://y.test/2")

    def test_a_missing_host_is_never_the_same_site(self):
        assert not same_site("/relative", "https://x.test")
