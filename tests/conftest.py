"""Shared fixtures.

Everything here is deterministic: a frozen clock, a fixed registry, and record
builders with sensible defaults. No test in this suite touches a network, a
database or the real time.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from statehouse.config.jurisdictions import (
    Jurisdiction,
    JurisdictionRegistry,
    PolitenessPolicy,
    default_registry,
)
from statehouse.core.clock import FrozenClock
from statehouse.core.enums import BillStatus, Chamber, DocumentKind, FetchMethod
from statehouse.core.models import Action, Document, DocumentVersion, SourceRef, Sponsor

UTC = timezone.utc


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2024, 3, 15, 12, 0, tzinfo=UTC))


@pytest.fixture
def now(clock: FrozenClock) -> datetime:
    return clock.now()


@pytest.fixture
def registry() -> JurisdictionRegistry:
    return default_registry()


@pytest.fixture
def sample_jurisdiction() -> Jurisdiction:
    return Jurisdiction(
        code="zz",
        name="Test State",
        timezone="America/New_York",
        portal_url="https://legislature.example.gov",
        adapter="test",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=PolitenessPolicy(requests_per_minute=60, concurrency=2, burst=3),
        backfill_from_year=2019,
        tags=("test",),
    )


@pytest.fixture
def source() -> SourceRef:
    return SourceRef(
        jurisdiction="zz",
        url="https://legislature.example.gov/bill/HB1",
        method=FetchMethod.HTTP,
        fetched_at=datetime(2024, 3, 15, 11, 0, tzinfo=UTC),
        http_status=200,
        spider="zz_bills",
    )


def make_action(
    day: str = "2024-01-10",
    description: str = "Introduced and referred to committee",
    **kwargs: Any,
) -> Action:
    return Action(occurred_on=date.fromisoformat(day), description=description, **kwargs)


def make_document(
    identifier: str = "HB1",
    *,
    jurisdiction: str = "zz",
    session: str = "2023-2024",
    title: str = "An act relating to municipal broadband",
    status: BillStatus = BillStatus.INTRODUCED,
    actions: list[Action] | None = None,
    versions: list[DocumentVersion] | None = None,
    sponsors: list[Sponsor] | None = None,
    **kwargs: Any,
) -> Document:
    return Document(
        jurisdiction=jurisdiction,
        session=session,
        identifier=identifier,
        title=title,
        kind=kwargs.pop("kind", DocumentKind.BILL),
        chamber=kwargs.pop("chamber", Chamber.LOWER),
        status=status,
        actions=actions if actions is not None else [make_action()],
        versions=versions if versions is not None else [],
        sponsors=sponsors if sponsors is not None else [Sponsor(name="Jane Smith")],
        observed_at=kwargs.pop("observed_at", datetime(2024, 3, 15, 12, 0, tzinfo=UTC)),
        **kwargs,
    )


@pytest.fixture
def document() -> Document:
    return make_document()


@pytest.fixture
def document_with_text() -> Document:
    return make_document(
        versions=[
            DocumentVersion(
                label="as introduced",
                text="The department shall establish a municipal broadband programme.",
                published_on=date(2024, 1, 10),
            )
        ]
    )


class FakeElement:
    """Stands in for a Selenium element."""

    def __init__(self) -> None:
        self.clicks = 0
        self.typed: list[str] = []

    def click(self) -> None:
        self.clicks += 1

    def send_keys(self, value: str) -> None:
        self.typed.append(value)


class FakeDriver:
    """Stands in for a Selenium driver.

    ``pages`` maps a URL to the sequence of page sources it returns on
    successive reads, which is how a repainting grid is simulated.
    """

    def __init__(self, pages: dict[str, list[str]] | None = None) -> None:
        self.pages = pages or {}
        self.current_url = ""
        self.visited: list[str] = []
        self.scripts: list[str] = []
        self.elements: dict[str, FakeElement] = {}
        self.quit_calls = 0
        self._cursor: dict[str, int] = {}

    def get(self, url: str) -> None:
        self.visited.append(url)
        self.current_url = url
        self._cursor.setdefault(url, 0)

    @property
    def page_source(self) -> str:
        sequence = self.pages.get(self.current_url, [])
        if not sequence:
            return ""
        index = min(self._cursor.get(self.current_url, 0), len(sequence) - 1)
        self._cursor[self.current_url] = index + 1
        return sequence[index]

    def find_element(self, _by: str, selector: str) -> FakeElement:
        return self.elements.setdefault(selector, FakeElement())

    def execute_script(self, script: str) -> None:
        self.scripts.append(script)

    def quit(self) -> None:
        self.quit_calls += 1


@pytest.fixture
def fake_driver() -> FakeDriver:
    return FakeDriver()
