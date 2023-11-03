"""Throttle, frontier, middlewares and the spider runner."""

from __future__ import annotations

import pytest

from statehouse.config.jurisdictions import PolitenessPolicy, default_registry
from statehouse.core.errors import FetchError, PortalUnavailable, RateLimited
from statehouse.scraping.frontier import CrawlFrontier, FrontierEntry
from statehouse.scraping.middlewares.headers import BASE_HEADERS, RotatingHeadersMiddleware
from statehouse.scraping.middlewares.retry import (
    MIN_PLAUSIBLE_BODY,
    ClassifyingRetryMiddleware,
    classify_response,
)
from statehouse.scraping.runner import SPIDER_BY_ADAPTER, build_command
from statehouse.scraping.throttle import DomainThrottle, TokenBucket

PAGE = b"x" * (MIN_PLAUSIBLE_BODY + 100)


class TestTokenBucket:
    def test_a_new_bucket_starts_full(self):
        assert TokenBucket(capacity=3, refill_per_second=1).tokens == 3

    def test_taking_spends_a_token(self):
        bucket = TokenBucket(capacity=2, refill_per_second=1)
        bucket.take(0.0)
        assert bucket.tokens == pytest.approx(1.0)

    def test_the_burst_can_be_spent_at_once(self):
        bucket = TokenBucket(capacity=3, refill_per_second=1)
        assert all(bucket.take(0.0) for _ in range(3))

    def test_an_exhausted_bucket_refuses(self):
        bucket = TokenBucket(capacity=1, refill_per_second=1)
        bucket.take(0.0)
        assert not bucket.take(0.0)

    def test_tokens_refill_over_time(self):
        bucket = TokenBucket(capacity=1, refill_per_second=1)
        bucket.take(0.0)
        assert bucket.take(1.0)

    def test_refilling_never_exceeds_the_capacity(self):
        bucket = TokenBucket(capacity=2, refill_per_second=10)
        bucket.take(0.0)
        bucket._refill(100.0)
        assert bucket.tokens == 2

    def test_the_wait_time_is_zero_when_a_token_is_available(self):
        assert TokenBucket(capacity=1, refill_per_second=1).wait_time(0.0) == 0.0

    def test_the_wait_time_reflects_the_refill_rate(self):
        bucket = TokenBucket(capacity=1, refill_per_second=0.5)
        bucket.take(0.0)
        assert bucket.wait_time(0.0) == pytest.approx(2.0)

    def test_asking_for_more_than_the_capacity_is_an_error(self):
        with pytest.raises(ValueError):
            TokenBucket(capacity=1, refill_per_second=1).wait_time(0.0, amount=5)

    def test_a_non_positive_capacity_is_rejected(self):
        with pytest.raises(ValueError):
            TokenBucket(capacity=0, refill_per_second=1)

    def test_a_non_positive_rate_is_rejected(self):
        with pytest.raises(ValueError):
            TokenBucket(capacity=1, refill_per_second=0)


class TestDomainThrottle:
    def test_a_registered_host_uses_its_policy(self):
        throttle = DomainThrottle({"a.test": PolitenessPolicy(requests_per_minute=120, burst=2)})
        assert throttle.policy_for("a.test").requests_per_minute == 120

    def test_an_unregistered_host_uses_the_default(self):
        throttle = DomainThrottle({}, default_policy=PolitenessPolicy(requests_per_minute=5))
        assert throttle.policy_for("unknown.test").requests_per_minute == 5

    def test_host_matching_is_case_insensitive(self):
        throttle = DomainThrottle({"a.test": PolitenessPolicy(requests_per_minute=120)})
        assert throttle.policy_for("A.TEST").requests_per_minute == 120

    def test_hosts_have_independent_budgets(self):
        throttle = DomainThrottle(
            {
                "a.test": PolitenessPolicy(requests_per_minute=60, burst=1),
                "b.test": PolitenessPolicy(requests_per_minute=60, burst=1),
            }
        )
        assert throttle.acquire("a.test", 0.0) and throttle.acquire("b.test", 0.0)

    def test_a_host_budget_is_enforced(self):
        throttle = DomainThrottle({"a.test": PolitenessPolicy(requests_per_minute=60, burst=1)})
        throttle.acquire("a.test", 0.0)
        assert not throttle.acquire("a.test", 0.0)

    def test_registering_replaces_a_policy_and_resets_the_bucket(self):
        throttle = DomainThrottle({"a.test": PolitenessPolicy(requests_per_minute=60, burst=1)})
        throttle.acquire("a.test", 0.0)
        throttle.register("a.test", PolitenessPolicy(requests_per_minute=60, burst=5))
        assert throttle.acquire("a.test", 0.0)

    def test_hosts_are_enumerable(self):
        throttle = DomainThrottle({"a.test": PolitenessPolicy(), "b.test": PolitenessPolicy()})
        assert throttle.hosts() == ["a.test", "b.test"]

    def test_every_registry_portal_can_be_throttled(self):
        registry = default_registry()
        throttle = DomainThrottle(
            {entry.portal_url: entry.politeness for entry in registry.values()}
        )
        assert len(throttle.hosts()) == len(registry)


class TestFrontier:
    def test_an_entry_is_queued(self):
        frontier = CrawlFrontier()
        assert frontier.push(FrontierEntry(url="https://x.test/a")) and len(frontier) == 1

    def test_urls_are_canonicalised_on_the_way_in(self):
        frontier = CrawlFrontier()
        frontier.push(FrontierEntry(url="https://x.test/a?utm_source=n"))
        assert frontier.pending_urls() == ["https://x.test/a"]

    def test_a_duplicate_is_rejected(self):
        frontier = CrawlFrontier()
        frontier.push(FrontierEntry(url="https://x.test/a"))
        assert not frontier.push(FrontierEntry(url="https://x.test/a/"))

    def test_rejections_are_counted(self):
        frontier = CrawlFrontier()
        frontier.push(FrontierEntry(url="https://x.test/a"))
        frontier.push(FrontierEntry(url="https://x.test/a"))
        assert frontier.rejected_count == 1

    def test_too_deep_is_rejected(self):
        frontier = CrawlFrontier(max_depth=1)
        assert not frontier.push(FrontierEntry(url="https://x.test/a", depth=2))

    def test_an_off_host_link_is_rejected(self):
        frontier = CrawlFrontier(allowed_host="x.test")
        assert not frontier.push(FrontierEntry(url="https://news.example/a"))

    def test_a_subdomain_of_the_allowed_host_is_accepted(self):
        frontier = CrawlFrontier(allowed_host="x.test")
        assert frontier.push(FrontierEntry(url="https://sub.x.test/a"))

    def test_higher_priority_comes_out_first(self):
        frontier = CrawlFrontier()
        frontier.push(FrontierEntry(url="https://x.test/low", priority=1))
        frontier.push(FrontierEntry(url="https://x.test/high", priority=9))
        assert frontier.pop().url.endswith("/high")

    def test_equal_priorities_keep_insertion_order(self):
        frontier = CrawlFrontier()
        frontier.push(FrontierEntry(url="https://x.test/one"))
        frontier.push(FrontierEntry(url="https://x.test/two"))
        assert [frontier.pop().url, frontier.pop().url] == [
            "https://x.test/one",
            "https://x.test/two",
        ]

    def test_popping_an_empty_frontier_returns_nothing(self):
        assert CrawlFrontier().pop() is None

    def test_draining_respects_a_limit(self):
        frontier = CrawlFrontier()
        frontier.push_many(FrontierEntry(url=f"https://x.test/{n}") for n in range(5))
        assert len(frontier.drain(2)) == 2

    def test_draining_without_a_limit_empties_it(self):
        frontier = CrawlFrontier()
        frontier.push_many(FrontierEntry(url=f"https://x.test/{n}") for n in range(5))
        frontier.drain()
        assert not frontier

    def test_pushing_many_reports_how_many_landed(self):
        frontier = CrawlFrontier()
        entries = [FrontierEntry(url="https://x.test/a"), FrontierEntry(url="https://x.test/a")]
        assert frontier.push_many(entries) == 1

    def test_seen_urls_can_be_preloaded(self):
        frontier = CrawlFrontier(seen=["https://x.test/a"])
        assert not frontier.push(FrontierEntry(url="https://x.test/a"))

    def test_pending_urls_are_in_priority_order(self):
        frontier = CrawlFrontier()
        frontier.push(FrontierEntry(url="https://x.test/low", priority=1))
        frontier.push(FrontierEntry(url="https://x.test/high", priority=9))
        assert frontier.pending_urls()[0].endswith("/high")

    def test_an_entry_needs_a_url(self):
        with pytest.raises(ValueError):
            FrontierEntry(url="")

    def test_a_negative_depth_is_rejected(self):
        with pytest.raises(ValueError):
            FrontierEntry(url="https://x.test/a", depth=-1)


class TestResponseClassification:
    def test_a_healthy_page_is_fine(self):
        assert classify_response(200, PAGE) is None

    def test_a_short_body_is_treated_as_unavailable(self):
        assert isinstance(classify_response(200, b"tiny"), PortalUnavailable)

    def test_a_maintenance_page_with_a_200_is_caught(self):
        body = b"<html>The site is down for maintenance</html>" + b" " * MIN_PLAUSIBLE_BODY
        assert isinstance(classify_response(200, body), PortalUnavailable)

    def test_a_blocked_request_page_is_caught(self):
        body = b"Request blocked" + b" " * MIN_PLAUSIBLE_BODY
        assert isinstance(classify_response(200, body), PortalUnavailable)

    @pytest.mark.parametrize("status", [429, 503])
    def test_throttling_statuses_are_rate_limits(self, status):
        assert isinstance(classify_response(status, b""), RateLimited)

    @pytest.mark.parametrize("status", [500, 502, 504, 408])
    def test_server_errors_are_retryable(self, status):
        assert isinstance(classify_response(status, b""), FetchError)

    @pytest.mark.parametrize("status", [400, 403, 404, 410])
    def test_client_errors_are_left_to_the_spider(self, status):
        assert classify_response(status, b"") is None


class _Request:
    def __init__(self, url: str = "https://x.test/a", meta: dict | None = None) -> None:
        self.url = url
        self.meta = meta if meta is not None else {}
        self.headers: dict[str, str] = {}
        self.priority = 0

    def replace(self, **_kwargs: object) -> "_Request":
        clone = _Request(self.url, dict(self.meta))
        clone.priority = self.priority
        return clone


class _Response:
    def __init__(self, status: int = 200, body: bytes = PAGE) -> None:
        self.status = status
        self.body = body
        self.url = "https://x.test/a"
        self.headers: dict[str, bytes] = {}


class TestRetryMiddleware:
    def test_a_good_response_passes_through(self):
        middleware = ClassifyingRetryMiddleware()
        response = _Response()
        assert middleware.process_response(_Request(), response, None) is response

    def test_a_transient_failure_is_rescheduled(self):
        middleware = ClassifyingRetryMiddleware()
        result = middleware.process_response(_Request(), _Response(status=500), None)
        assert isinstance(result, _Request) and result.meta["retry_times"] == 1

    def test_a_retry_is_deprioritised(self):
        middleware = ClassifyingRetryMiddleware()
        result = middleware.process_response(_Request(), _Response(status=500), None)
        assert result.priority < 0

    def test_the_attempt_budget_is_eventually_exhausted(self):
        from statehouse.utils.retry import RetryPolicy

        middleware = ClassifyingRetryMiddleware(RetryPolicy(max_attempts=2))
        request = _Request(meta={"retry_times": 1})
        with pytest.raises(FetchError):
            middleware.process_response(request, _Response(status=500), None)

    def test_outcomes_are_counted(self):
        middleware = ClassifyingRetryMiddleware()
        middleware.process_response(_Request(), _Response(), None)
        assert middleware.counts["ok"] == 1

    def test_a_transport_exception_is_retried(self):
        middleware = ClassifyingRetryMiddleware()
        assert middleware.process_exception(_Request(), OSError("reset"), None) is not None

    def test_a_transport_exception_eventually_gives_up(self):
        from statehouse.utils.retry import RetryPolicy

        middleware = ClassifyingRetryMiddleware(RetryPolicy(max_attempts=1))
        assert middleware.process_exception(_Request(), OSError("reset"), None) is None


class TestHeadersMiddleware:
    def test_the_base_headers_are_applied(self):
        middleware = RotatingHeadersMiddleware(user_agent="ua/1")
        request = _Request()
        middleware.process_request(request, None)
        assert request.headers["Accept"] == BASE_HEADERS["Accept"]

    def test_the_user_agent_is_set(self):
        middleware = RotatingHeadersMiddleware(user_agent="ua/1")
        request = _Request()
        middleware.process_request(request, None)
        assert request.headers["User-Agent"] == "ua/1"

    def test_api_requests_ask_for_json(self):
        middleware = RotatingHeadersMiddleware(user_agent="ua/1")
        assert "json" in middleware.headers_for("api")["Accept"]

    def test_bulk_requests_ask_for_xml(self):
        middleware = RotatingHeadersMiddleware(user_agent="ua/1")
        assert "xml" in middleware.headers_for("bulk")["Accept"]

    def test_an_explicit_header_is_not_overwritten(self):
        middleware = RotatingHeadersMiddleware(user_agent="ua/1")
        request = _Request()
        request.headers["Accept"] = "text/plain"
        middleware.process_request(request, None)
        assert request.headers["Accept"] == "text/plain"


class TestSpiderCommand:
    def test_every_registry_adapter_has_a_spider_or_is_unbuilt(self):
        registry = default_registry()
        built = {code for code in registry if registry[code].adapter in SPIDER_BY_ADAPTER}
        assert {"us", "ca", "tx", "ny", "il", "oh"} <= built

    def test_the_command_names_the_spider(self, registry):
        command = build_command(registry["ca"], session="2023-2024", output_path="/tmp/out.jsonl")
        assert "ca_bills" in command

    def test_the_session_is_passed_through(self, registry):
        command = build_command(registry["tx"], session="2023-2024", output_path="/tmp/out.jsonl")
        assert "session=2023-2024" in command

    def test_a_since_bound_is_passed_through(self, registry):
        command = build_command(
            registry["tx"], session="2023-2024", output_path="/tmp/o.jsonl", since="2024-01-01"
        )
        assert "since=2024-01-01" in command

    def test_an_adapter_with_no_spider_is_an_error(self, sample_jurisdiction):
        with pytest.raises(FetchError):
            build_command(sample_jurisdiction, session="2024", output_path="/tmp/o.jsonl")
