"""Portal sessions.

The browser-backed portals all have the same shape of problem: you cannot ask
for a page, you have to arrive at it. A session encodes the arrival —
establish cookies, submit the search form, wait for the grid, read the HTML —
and hands plain markup back to the same parsers the HTTP spiders use.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from statehouse.core.errors import ParseError, PortalUnavailable, TransientError
from statehouse.scraping.middlewares.retry import classify_response

__all__ = ["RenderRequest", "RenderResult", "PortalSession", "ReadyCondition"]

ReadyCondition = Callable[[str], bool]


@dataclass(frozen=True)
class RenderRequest:
    """One page to render."""

    url: str
    wait_for: str = ""
    actions: tuple[tuple[str, str], ...] = ()
    settle_polls: int = 20
    label: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("RenderRequest requires a url")
        if self.settle_polls <= 0:
            raise ValueError("settle_polls must be positive")


@dataclass
class RenderResult:
    """The markup a render produced, plus what it cost."""

    url: str
    html: str
    polls: int = 0
    actions_run: int = 0
    final_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.html)


class PortalSession:
    """Drives one browser through a portal.

    The driver is injected and only four of its methods are used — ``get``,
    ``page_source``, ``find_element`` and ``execute_script`` — which is what
    makes the whole flow testable against a fake.
    """

    def __init__(
        self,
        driver: Any,
        *,
        jurisdiction: str,
        ready: ReadyCondition | None = None,
    ) -> None:
        self.driver = driver
        self.jurisdiction = jurisdiction
        self.ready = ready or self._default_ready
        self.pages_rendered = 0
        self.polls_spent = 0

    @staticmethod
    def _default_ready(html: str) -> bool:
        return bool(html) and len(html) >= 512

    def render(self, request: RenderRequest) -> RenderResult:
        """Navigate, run the request's actions, and return settled markup.

        Polling rather than an implicit wait: these grids repaint several
        times and an implicit wait returns the first paint, which is usually
        the loading spinner. A page that never satisfies the ready condition
        raises :class:`~statehouse.core.errors.PortalUnavailable` so the retry
        layer treats it like any other transient failure.
        """
        self.driver.get(request.url)
        result = RenderResult(url=request.url, html="", final_url=request.url)

        for verb, argument in request.actions:
            self._run_action(verb, argument)
            result.actions_run += 1

        html = ""
        for poll in range(1, request.settle_polls + 1):
            html = self._page_source()
            result.polls = poll
            self.polls_spent += 1
            if request.wait_for and request.wait_for not in html:
                continue
            if self.ready(html):
                break
        else:
            raise PortalUnavailable(
                "page never settled",
                url=request.url,
                jurisdiction=self.jurisdiction,
                polls=request.settle_polls,
            )

        error = classify_response(200, html.encode("utf-8", errors="replace"))
        if error is not None:
            raise error

        result.html = html
        result.final_url = getattr(self.driver, "current_url", request.url) or request.url
        self.pages_rendered += 1
        return result

    def render_many(self, requests: Sequence[RenderRequest]) -> list[RenderResult]:
        """Render several pages on one session, skipping the ones that fail.

        Failures are recorded as a note on a zero-length result rather than
        aborting the sequence: losing one page of a grid should not cost the
        other forty.
        """
        results: list[RenderResult] = []
        for request in requests:
            try:
                results.append(self.render(request))
            except TransientError as exc:
                results.append(
                    RenderResult(
                        url=request.url,
                        html="",
                        notes=[f"{type(exc).__name__}: {exc.message}"],
                    )
                )
        return results

    def _page_source(self) -> str:
        source = getattr(self.driver, "page_source", "")
        return source() if callable(source) else (source or "")

    def _run_action(self, verb: str, argument: str) -> None:
        if verb == "click":
            element = self.driver.find_element("css selector", argument)
            element.click()
        elif verb == "script":
            self.driver.execute_script(argument)
        elif verb == "type":
            selector, _, value = argument.partition("=")
            element = self.driver.find_element("css selector", selector)
            element.send_keys(value)
        else:
            raise ParseError("unknown session action", verb=verb)
