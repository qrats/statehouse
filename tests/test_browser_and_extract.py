"""Browser pooling and sessions, table and text extraction."""

from __future__ import annotations

import pytest

from statehouse.browser.driver import DriverOptions, chrome_arguments
from statehouse.browser.pool import BrowserPool
from statehouse.browser.session import PortalSession, RenderRequest
from statehouse.config.settings import Settings
from statehouse.core.errors import ParseError, PermanentError, PortalUnavailable, TransientError
from statehouse.extract.tables import first_table_with_headers, parse_tables
from statehouse.extract.textract import (
    extract_body_text,
    guess_content_kind,
    split_sections,
    strip_line_numbers,
)

from .conftest import FakeDriver

FILLER = " ".join(["settled"] * 200)

DOCKET_HTML = """
<table id="history">
  <tr><th>Date</th><th>Chamber</th><th>Action</th></tr>
  <tr><td>01/05/2024</td><td>House</td><td>Introduced</td></tr>
  <tr><td>02/01/2024</td><td>House</td><td>Referred to Health</td></tr>
</table>
"""


class TestDriverOptions:
    def test_the_sandbox_flag_is_always_present(self):
        assert "--no-sandbox" in chrome_arguments(DriverOptions())

    def test_headless_is_requested_when_asked(self):
        assert "--headless=new" in chrome_arguments(DriverOptions(headless=True))

    def test_headed_mode_omits_the_flag(self):
        assert "--headless=new" not in chrome_arguments(DriverOptions(headless=False))

    def test_the_window_size_is_passed(self):
        assert "--window-size=800,600" in chrome_arguments(DriverOptions(window_size=(800, 600)))

    def test_images_are_disabled_by_default(self):
        assert any("imagesEnabled=false" in arg for arg in chrome_arguments(DriverOptions()))

    def test_a_user_agent_is_passed_when_set(self):
        args = chrome_arguments(DriverOptions(user_agent="ua/1"))
        assert "--user-agent=ua/1" in args

    def test_extra_arguments_are_appended(self):
        args = chrome_arguments(DriverOptions(extra_arguments=["--proxy-server=x"]))
        assert args[-1] == "--proxy-server=x"

    def test_options_derive_from_settings(self):
        options = DriverOptions.from_settings(Settings(browser_headless=False, user_agent="ua/2"))
        assert options.headless is False and options.user_agent == "ua/2"

    def test_a_non_positive_timeout_is_rejected(self):
        with pytest.raises(ValueError):
            DriverOptions(page_load_timeout=0)

    def test_a_non_positive_window_is_rejected(self):
        with pytest.raises(ValueError):
            DriverOptions(window_size=(0, 100))


class TestBrowserPool:
    def _factory(self, created: list[FakeDriver]):
        def factory() -> FakeDriver:
            driver = FakeDriver()
            created.append(driver)
            return driver

        return factory

    def test_a_driver_is_created_on_demand(self):
        created: list[FakeDriver] = []
        pool = BrowserPool(self._factory(created), size=2)
        pool.acquire()
        assert len(created) == 1

    def test_a_released_driver_is_reused(self):
        created: list[FakeDriver] = []
        pool = BrowserPool(self._factory(created), size=2)
        lease = pool.acquire()
        pool.release(lease)
        pool.acquire()
        assert len(created) == 1

    def test_the_pool_size_is_a_hard_cap(self):
        pool = BrowserPool(self._factory([]), size=1)
        pool.acquire()
        with pytest.raises(TransientError):
            pool.acquire()

    def test_a_worn_out_driver_is_recycled(self):
        created: list[FakeDriver] = []
        pool = BrowserPool(self._factory(created), size=1, recycle_after=2)
        lease = pool.acquire()
        lease.record_page()
        lease.record_page()
        pool.release(lease)
        assert pool.recycled == 1 and created[0].quit_calls == 1

    def test_a_recycled_driver_is_replaced_on_the_next_acquire(self):
        created: list[FakeDriver] = []
        pool = BrowserPool(self._factory(created), size=1, recycle_after=1)
        lease = pool.acquire()
        lease.record_page()
        pool.release(lease)
        pool.acquire()
        assert len(created) == 2

    def test_a_failing_driver_is_retired(self):
        created: list[FakeDriver] = []
        pool = BrowserPool(self._factory(created), size=1, max_failures=2)
        lease = pool.acquire()
        lease.record_failure()
        lease.record_failure()
        pool.release(lease)
        assert created[0].quit_calls == 1

    def test_the_lease_context_counts_a_failure(self):
        created: list[FakeDriver] = []
        pool = BrowserPool(self._factory(created), size=1, max_failures=1)
        with pytest.raises(ValueError):
            with pool.lease():
                raise ValueError("boom")
        assert created[0].quit_calls == 1

    def test_the_lease_context_always_releases(self):
        pool = BrowserPool(self._factory([]), size=1)
        with pool.lease():
            pass
        assert pool.in_use == 0

    def test_closing_quits_every_driver(self):
        created: list[FakeDriver] = []
        pool = BrowserPool(self._factory(created), size=2)
        pool.acquire()
        lease = pool.acquire()
        pool.release(lease)
        pool.close()
        assert all(driver.quit_calls == 1 for driver in created)

    def test_acquiring_from_a_closed_pool_is_permanent(self):
        pool = BrowserPool(self._factory([]), size=1)
        pool.close()
        with pytest.raises(PermanentError):
            pool.acquire()

    def test_the_pool_works_as_a_context_manager(self):
        created: list[FakeDriver] = []
        with BrowserPool(self._factory(created), size=1) as pool:
            pool.acquire()
        assert created[0].quit_calls == 1

    def test_a_non_positive_size_is_rejected(self):
        with pytest.raises(ValueError):
            BrowserPool(self._factory([]), size=0)


class TestPortalSession:
    def test_a_settled_page_is_returned(self):
        driver = FakeDriver({"https://x.test/a": [FILLER]})
        session = PortalSession(driver, jurisdiction="zz")
        assert session.render(RenderRequest(url="https://x.test/a")).html == FILLER

    def test_polling_continues_until_the_marker_appears(self):
        driver = FakeDriver(
            {"https://x.test/a": ["loading " + FILLER, "loading " + FILLER, "grid " + FILLER]}
        )
        session = PortalSession(driver, jurisdiction="zz")
        result = session.render(RenderRequest(url="https://x.test/a", wait_for="grid"))
        assert result.polls == 3

    def test_a_page_that_never_settles_is_unavailable(self):
        driver = FakeDriver({"https://x.test/a": ["loading " + FILLER]})
        session = PortalSession(driver, jurisdiction="zz")
        with pytest.raises(PortalUnavailable):
            session.render(RenderRequest(url="https://x.test/a", wait_for="grid", settle_polls=3))

    def test_an_empty_page_is_unavailable(self):
        driver = FakeDriver({"https://x.test/a": ["tiny"]})
        session = PortalSession(driver, jurisdiction="zz")
        with pytest.raises(PortalUnavailable):
            session.render(RenderRequest(url="https://x.test/a", settle_polls=2))

    def test_a_maintenance_page_is_rejected(self):
        driver = FakeDriver({"https://x.test/a": ["Scheduled maintenance " + FILLER]})
        session = PortalSession(driver, jurisdiction="zz")
        with pytest.raises(PortalUnavailable):
            session.render(RenderRequest(url="https://x.test/a"))

    def test_click_actions_are_run(self):
        driver = FakeDriver({"https://x.test/a": [FILLER]})
        session = PortalSession(driver, jurisdiction="zz")
        session.render(RenderRequest(url="https://x.test/a", actions=(("click", "#go"),)))
        assert driver.elements["#go"].clicks == 1

    def test_script_actions_are_run(self):
        driver = FakeDriver({"https://x.test/a": [FILLER]})
        session = PortalSession(driver, jurisdiction="zz")
        session.render(RenderRequest(url="https://x.test/a", actions=(("script", "go()"),)))
        assert driver.scripts == ["go()"]

    def test_type_actions_send_keys(self):
        driver = FakeDriver({"https://x.test/a": [FILLER]})
        session = PortalSession(driver, jurisdiction="zz")
        session.render(RenderRequest(url="https://x.test/a", actions=(("type", "#q=broadband"),)))
        assert driver.elements["#q"].typed == ["broadband"]

    def test_an_unknown_action_is_a_parse_error(self):
        driver = FakeDriver({"https://x.test/a": [FILLER]})
        session = PortalSession(driver, jurisdiction="zz")
        with pytest.raises(ParseError):
            session.render(RenderRequest(url="https://x.test/a", actions=(("wiggle", "x"),)))

    def test_rendering_many_skips_the_failures(self):
        driver = FakeDriver({"https://x.test/a": [FILLER], "https://x.test/b": ["tiny"]})
        session = PortalSession(driver, jurisdiction="zz")
        results = session.render_many(
            [
                RenderRequest(url="https://x.test/a"),
                RenderRequest(url="https://x.test/b", settle_polls=2),
            ]
        )
        assert results[0].size > 0 and results[1].size == 0 and results[1].notes

    def test_the_page_count_is_tracked(self):
        driver = FakeDriver({"https://x.test/a": [FILLER]})
        session = PortalSession(driver, jurisdiction="zz")
        session.render(RenderRequest(url="https://x.test/a"))
        assert session.pages_rendered == 1

    def test_a_request_needs_a_url(self):
        with pytest.raises(ValueError):
            RenderRequest(url="")

    def test_a_non_positive_poll_budget_is_rejected(self):
        with pytest.raises(ValueError):
            RenderRequest(url="https://x.test/a", settle_polls=0)


class TestTables:
    def test_a_header_row_is_recognised(self):
        table = parse_tables(DOCKET_HTML)[0]
        assert table.headers == ["Date", "Chamber", "Action"]

    def test_data_rows_are_read(self):
        assert len(parse_tables(DOCKET_HTML)[0]) == 2

    def test_a_column_can_be_read_by_name(self):
        assert parse_tables(DOCKET_HTML)[0].column("action") == ["Introduced", "Referred to Health"]

    def test_an_unknown_column_reads_as_empty(self):
        assert parse_tables(DOCKET_HTML)[0].column("nope") == []

    def test_rows_become_records(self):
        record = parse_tables(DOCKET_HTML)[0].records()[0]
        assert record["Date"] == "01/05/2024"

    def test_an_unheaded_table_keeps_every_row_as_data(self):
        markup = "<table><tr><td>a</td><td>b</td></tr></table>"
        table = parse_tables(markup)[0]
        assert table.headers == [] and len(table) == 1

    def test_empty_rows_are_dropped(self):
        markup = "<table><tr><td></td></tr><tr><td>a</td></tr></table>"
        assert len(parse_tables(markup)[0]) == 1

    def test_a_colspan_pads_the_row(self):
        markup = '<table><tr><td colspan="3">wide</td></tr></table>'
        assert len(parse_tables(markup)[0].rows[0]) == 3

    def test_extra_cells_land_in_a_catch_all(self):
        markup = (
            "<table><tr><th>A</th></tr><tr><td>one</td><td>two</td></tr></table>"
        )
        assert parse_tables(markup)[0].records()[0]["_extra"] == "two"

    def test_unclosed_rows_still_parse(self):
        markup = "<table><tr><th>A<tr><td>one<tr><td>two</table>"
        assert len(parse_tables(markup)[0]) == 2

    def test_markup_with_no_table_yields_nothing(self):
        assert parse_tables("<p>nothing here</p>") == []

    def test_the_table_with_the_wanted_headers_is_found(self):
        markup = "<table><tr><th>X</th></tr><tr><td>1</td></tr></table>" + DOCKET_HTML
        table = first_table_with_headers(markup, ["date", "action"])
        assert table is not None and "Date" in table.headers

    def test_no_matching_table_returns_nothing(self):
        assert first_table_with_headers(DOCKET_HTML, ["sponsor"]) is None

    def test_asking_for_no_headers_returns_nothing(self):
        assert first_table_with_headers(DOCKET_HTML, []) is None

    def test_header_matching_is_by_containment(self):
        markup = "<table><tr><th>Action Date</th></tr><tr><td>1</td></tr></table>"
        assert first_table_with_headers(markup, ["date"]) is not None


class TestTextExtraction:
    @pytest.mark.parametrize(
        "payload,expected",
        [
            (b"%PDF-1.4 ...", "pdf"),
            (b"{\\rtf1", "rtf"),
            (b"PK\x03\x04", "zip"),
            (b"<?xml version='1.0'?><bill/>", "xml"),
            (b"<html><body>hi</body></html>", "html"),
            (b"just words", "text"),
            (b"   ", "text"),
        ],
    )
    def test_content_kinds_are_guessed_from_the_bytes(self, payload, expected):
        assert guess_content_kind(payload) == expected

    def test_html_is_reduced_to_text(self):
        assert "hello" in extract_body_text(b"<html><body><p>hello</p></body></html>")

    def test_plain_text_passes_through(self):
        assert extract_body_text(b"hello world") == "hello world"

    def test_a_pdf_must_be_converted_first(self):
        with pytest.raises(ParseError):
            extract_body_text(b"%PDF-1.4 body")

    def test_printed_line_numbers_are_stripped(self):
        assert strip_line_numbers("  1  The department shall") == "The department shall"

    def test_a_numbered_list_item_keeps_its_number(self):
        assert strip_line_numbers("1. A numbered item") == "1. A numbered item"

    def test_sections_are_split_out(self):
        text = "Preamble words.\nSECTION 1. First part.\nSECTION 2. Second part."
        sections = split_sections(text)
        assert [heading for heading, _body in sections] == ["preamble", "SECTION 1", "SECTION 2"]

    def test_text_without_headings_is_one_body(self):
        assert split_sections("Just some words")[0][0] == "body"

    def test_empty_text_yields_no_sections(self):
        assert split_sections("   ") == []

    def test_article_headings_are_recognised(self):
        assert any(h.startswith("ARTICLE") for h, _ in split_sections("ARTICLE II. Powers here."))
