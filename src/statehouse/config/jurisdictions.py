"""The jurisdiction registry.

Fifty states plus the federal source, each with a different portal, a
different idea of what a session is called, and a different tolerance for
being scraped. Everything that varies per jurisdiction lives here so that
adding a state is a registry entry plus a spider, not a change to the
pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace

from statehouse.core.enums import Chamber, FetchMethod
from statehouse.core.errors import ConfigError, JurisdictionNotConfigured

__all__ = [
    "PolitenessPolicy",
    "Jurisdiction",
    "JurisdictionRegistry",
    "default_registry",
    "REGISTRY_ENTRIES",
]


@dataclass(frozen=True)
class PolitenessPolicy:
    """How hard we are willing to lean on one source.

    ``requests_per_minute`` is the steady-state budget; ``burst`` is how much
    of it may be spent at once. ``off_hours_multiplier`` lets a portal that
    only tolerates traffic overnight be scraped harder at 03:00 local.
    """

    requests_per_minute: int = 30
    concurrency: int = 4
    burst: int = 5
    off_hours_multiplier: float = 1.0
    off_hours_local: tuple[int, int] = (1, 6)
    respect_robots: bool = True
    backoff_seconds: float = 2.0
    max_retries: int = 4

    def __post_init__(self) -> None:
        if self.requests_per_minute <= 0:
            raise ConfigError("requests_per_minute must be positive")
        if self.concurrency <= 0:
            raise ConfigError("concurrency must be positive")
        if self.burst <= 0:
            raise ConfigError("burst must be positive")
        if self.off_hours_multiplier <= 0:
            raise ConfigError("off_hours_multiplier must be positive")
        start, end = self.off_hours_local
        if not (0 <= start <= 23 and 0 <= end <= 23):
            raise ConfigError("off_hours_local must be hours of the day")

    @property
    def min_interval_seconds(self) -> float:
        return 60.0 / float(self.requests_per_minute)

    def budget_at(self, local_hour: int) -> int:
        """Requests per minute permitted at ``local_hour`` in the source's zone."""
        start, end = self.off_hours_local
        if start <= end:
            in_window = start <= local_hour < end
        else:  # window wraps midnight
            in_window = local_hour >= start or local_hour < end
        if in_window:
            return max(1, int(self.requests_per_minute * self.off_hours_multiplier))
        return self.requests_per_minute


@dataclass(frozen=True)
class Jurisdiction:
    """One ingestible source."""

    code: str
    name: str
    timezone: str
    portal_url: str
    adapter: str
    method: FetchMethod = FetchMethod.HTTP
    chambers: tuple[Chamber, ...] = (Chamber.UPPER, Chamber.LOWER)
    session_pattern: str = "{year}"
    politeness: PolitenessPolicy = field(default_factory=PolitenessPolicy)
    enabled: bool = True
    backfill_from_year: int = 2015
    notes: str = ""
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.code or not self.code.strip():
            raise ConfigError("Jurisdiction requires a code")
        object.__setattr__(self, "code", self.code.strip().lower())
        if not self.portal_url.startswith(("http://", "https://")):
            raise ConfigError("portal_url must be absolute", code=self.code)
        object.__setattr__(self, "method", FetchMethod.parse(self.method))

    @property
    def requires_browser(self) -> bool:
        return self.method is FetchMethod.BROWSER

    @property
    def is_federal(self) -> bool:
        return self.code == "us"

    def session_label(self, year: int) -> str:
        """Render the source's own session label for a calendar year.

        Two-year jurisdictions render as ``"2023-2024"`` anchored on the odd
        year, which is what every biennial legislature in the registry uses.
        """
        if "{biennium}" in self.session_pattern:
            start = year if year % 2 == 1 else year - 1
            return self.session_pattern.format(biennium=f"{start}-{start + 1}", year=year)
        return self.session_pattern.format(year=year)

    def with_politeness(self, **changes: object) -> Jurisdiction:
        return replace(self, politeness=replace(self.politeness, **changes))  # type: ignore[arg-type]


class JurisdictionRegistry(Mapping[str, Jurisdiction]):
    """Immutable, case-insensitive lookup over the configured jurisdictions."""

    def __init__(self, entries: Iterable[Jurisdiction]) -> None:
        self._entries: dict[str, Jurisdiction] = {}
        for entry in entries:
            if entry.code in self._entries:
                raise ConfigError("duplicate jurisdiction code", code=entry.code)
            self._entries[entry.code] = entry

    def __getitem__(self, code: str) -> Jurisdiction:
        try:
            return self._entries[str(code).strip().lower()]
        except KeyError as exc:
            raise JurisdictionNotConfigured(
                "no such jurisdiction", code=code, known=len(self._entries)
            ) from exc

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, code: object) -> bool:
        # Spelled out because ``__getitem__`` raises JurisdictionNotConfigured
        # rather than KeyError, which Mapping's default would let escape.
        return str(code).strip().lower() in self._entries

    def enabled(self) -> list[Jurisdiction]:
        return [self._entries[code] for code in self if self._entries[code].enabled]

    def by_method(self, method: FetchMethod | str) -> list[Jurisdiction]:
        wanted = FetchMethod.parse(method)
        return [self._entries[code] for code in self if self._entries[code].method is wanted]

    def by_tag(self, tag: str) -> list[Jurisdiction]:
        return [self._entries[code] for code in self if tag in self._entries[code].tags]

    def resolve_many(self, codes: Iterable[str]) -> list[Jurisdiction]:
        """Resolve a list of codes, expanding ``"all"`` to every enabled entry."""
        wanted = [str(c).strip().lower() for c in codes if str(c).strip()]
        if not wanted or wanted == ["all"]:
            return self.enabled()
        return [self[code] for code in wanted]

    def replace(self, entry: Jurisdiction) -> JurisdictionRegistry:
        merged = dict(self._entries)
        merged[entry.code] = entry
        return JurisdictionRegistry(merged.values())


def _p(rpm: int, concurrency: int = 4, **kwargs: object) -> PolitenessPolicy:
    return PolitenessPolicy(requests_per_minute=rpm, concurrency=concurrency, **kwargs)  # type: ignore[arg-type]


REGISTRY_ENTRIES: tuple[Jurisdiction, ...] = (
    Jurisdiction(
        code="us",
        name="United States Congress",
        timezone="America/New_York",
        portal_url="https://www.congress.gov",
        adapter="federal",
        method=FetchMethod.BULK,
        session_pattern="{biennium}",
        politeness=_p(120, 8),
        backfill_from_year=2011,
        tags=("federal", "bulk"),
        notes="Bulk XML drops; the HTML site is only used for rendered text.",
    ),
    Jurisdiction(
        code="ca",
        name="California",
        timezone="America/Los_Angeles",
        portal_url="https://leginfo.legislature.ca.gov",
        adapter="california",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=_p(45, 6, off_hours_multiplier=2.0),
        backfill_from_year=2009,
        tags=("large", "biennial"),
    ),
    Jurisdiction(
        code="tx",
        name="Texas",
        timezone="America/Chicago",
        portal_url="https://capitol.texas.gov",
        adapter="texas",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=_p(30, 4),
        backfill_from_year=2011,
        tags=("large", "biennial"),
        notes="Regular session runs odd years only; special sessions appear ad hoc.",
    ),
    Jurisdiction(
        code="ny",
        name="New York",
        timezone="America/New_York",
        portal_url="https://nyassembly.gov",
        adapter="newyork",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=_p(60, 6),
        backfill_from_year=2009,
        tags=("large", "biennial"),
    ),
    Jurisdiction(
        code="fl",
        name="Florida",
        timezone="America/New_York",
        portal_url="https://www.flsenate.gov",
        adapter="florida",
        method=FetchMethod.HTTP,
        session_pattern="{year}",
        politeness=_p(40, 5),
        backfill_from_year=2012,
        tags=("large", "annual"),
    ),
    Jurisdiction(
        code="il",
        name="Illinois",
        timezone="America/Chicago",
        portal_url="https://www.ilga.gov",
        adapter="illinois",
        method=FetchMethod.BROWSER,
        session_pattern="{biennium}",
        politeness=_p(12, 2, burst=2, off_hours_multiplier=2.5),
        backfill_from_year=2013,
        tags=("browser", "biennial"),
        notes="Session state lives in cookies; the bill list is a postback grid.",
    ),
    Jurisdiction(
        code="pa",
        name="Pennsylvania",
        timezone="America/New_York",
        portal_url="https://www.legis.state.pa.us",
        adapter="pennsylvania",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=_p(25, 3),
        backfill_from_year=2013,
        tags=("biennial",),
    ),
    Jurisdiction(
        code="oh",
        name="Ohio",
        timezone="America/New_York",
        portal_url="https://www.legislature.ohio.gov",
        adapter="ohio",
        method=FetchMethod.API,
        session_pattern="{biennium}",
        politeness=_p(90, 8),
        backfill_from_year=2011,
        tags=("api", "biennial"),
    ),
    Jurisdiction(
        code="ga",
        name="Georgia",
        timezone="America/New_York",
        portal_url="https://www.legis.ga.gov",
        adapter="georgia",
        method=FetchMethod.BROWSER,
        session_pattern="{biennium}",
        politeness=_p(10, 2, burst=2, off_hours_multiplier=3.0),
        backfill_from_year=2015,
        tags=("browser", "biennial"),
        notes="Angular front end; everything worth having is behind XHR.",
    ),
    Jurisdiction(
        code="wa",
        name="Washington",
        timezone="America/Los_Angeles",
        portal_url="https://app.leg.wa.gov",
        adapter="washington",
        method=FetchMethod.API,
        session_pattern="{biennium}",
        politeness=_p(75, 6),
        backfill_from_year=2009,
        tags=("api", "biennial"),
    ),
    Jurisdiction(
        code="ma",
        name="Massachusetts",
        timezone="America/New_York",
        portal_url="https://malegislature.gov",
        adapter="massachusetts",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=_p(20, 3),
        backfill_from_year=2013,
        tags=("biennial",),
    ),
    Jurisdiction(
        code="mi",
        name="Michigan",
        timezone="America/New_York",
        portal_url="https://www.legislature.mi.gov",
        adapter="michigan",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=_p(30, 4),
        backfill_from_year=2011,
        tags=("biennial",),
    ),
    Jurisdiction(
        code="nj",
        name="New Jersey",
        timezone="America/New_York",
        portal_url="https://www.njleg.state.nj.us",
        adapter="newjersey",
        method=FetchMethod.BROWSER,
        session_pattern="{biennium}",
        politeness=_p(15, 2, off_hours_multiplier=2.0),
        backfill_from_year=2014,
        tags=("browser", "biennial"),
    ),
    Jurisdiction(
        code="va",
        name="Virginia",
        timezone="America/New_York",
        portal_url="https://lis.virginia.gov",
        adapter="virginia",
        method=FetchMethod.HTTP,
        session_pattern="{year}",
        politeness=_p(50, 5),
        backfill_from_year=2012,
        tags=("annual",),
    ),
    Jurisdiction(
        code="nc",
        name="North Carolina",
        timezone="America/New_York",
        portal_url="https://www.ncleg.gov",
        adapter="northcarolina",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=_p(35, 4),
        backfill_from_year=2013,
        tags=("biennial",),
    ),
    Jurisdiction(
        code="az",
        name="Arizona",
        timezone="America/Phoenix",
        portal_url="https://www.azleg.gov",
        adapter="arizona",
        method=FetchMethod.API,
        session_pattern="{year}",
        politeness=_p(60, 6),
        backfill_from_year=2014,
        tags=("api", "annual"),
        notes="No daylight saving; local-hour politeness windows shift twice a year.",
    ),
    Jurisdiction(
        code="co",
        name="Colorado",
        timezone="America/Denver",
        portal_url="https://leg.colorado.gov",
        adapter="colorado",
        method=FetchMethod.HTTP,
        session_pattern="{year}",
        politeness=_p(30, 4),
        backfill_from_year=2015,
        tags=("annual",),
    ),
    Jurisdiction(
        code="mn",
        name="Minnesota",
        timezone="America/Chicago",
        portal_url="https://www.revisor.mn.gov",
        adapter="minnesota",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=_p(40, 4),
        backfill_from_year=2013,
        tags=("biennial",),
    ),
    Jurisdiction(
        code="or",
        name="Oregon",
        timezone="America/Los_Angeles",
        portal_url="https://olis.oregonlegislature.gov",
        adapter="oregon",
        method=FetchMethod.API,
        session_pattern="{year}",
        politeness=_p(80, 6),
        backfill_from_year=2013,
        tags=("api", "annual"),
    ),
    Jurisdiction(
        code="wi",
        name="Wisconsin",
        timezone="America/Chicago",
        portal_url="https://docs.legis.wisconsin.gov",
        adapter="wisconsin",
        method=FetchMethod.HTTP,
        session_pattern="{biennium}",
        politeness=_p(25, 3),
        backfill_from_year=2013,
        tags=("biennial",),
    ),
    Jurisdiction(
        code="mo",
        name="Missouri",
        timezone="America/Chicago",
        portal_url="https://www.house.mo.gov",
        adapter="missouri",
        method=FetchMethod.HTTP,
        session_pattern="{year}",
        politeness=_p(20, 3),
        backfill_from_year=2015,
        tags=("annual",),
        enabled=False,
        notes="Disabled pending a portal rewrite announced for next session.",
    ),
)


def default_registry() -> JurisdictionRegistry:
    """The registry the DAGs and the CLI use unless told otherwise."""
    return JurisdictionRegistry(REGISTRY_ENTRIES)
