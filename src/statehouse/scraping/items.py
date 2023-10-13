"""Scrapy item definitions.

Kept thin. Spiders fill these in with whatever the page gave them; the
transform layer is what turns them into canonical records, so nothing here
validates or coerces.
"""

from __future__ import annotations

try:  # pragma: no cover - exercised only where Scrapy is installed
    import scrapy

    _Item = scrapy.Item
    _Field = scrapy.Field
except ImportError:  # pragma: no cover - the pure-Python test path

    class _Item(dict):  # type: ignore[no-redef]
        """Minimal stand-in so the module imports without Scrapy."""

        fields: dict[str, object] = {}

        def __init_subclass__(cls, **kwargs: object) -> None:
            super().__init_subclass__(**kwargs)
            cls.fields = {
                name: value
                for name, value in vars(cls).items()
                if not name.startswith("_") and isinstance(value, _FieldMarker)
            }

    class _FieldMarker:
        pass

    def _Field(**_kwargs: object) -> "_FieldMarker":  # type: ignore[no-redef]
        return _FieldMarker()


class DocumentItem(_Item):
    """A scraped bill, resolution or regulation."""

    identifier = _Field()
    title = _Field()
    summary = _Field()
    kind = _Field()
    chamber = _Field()
    status = _Field()
    session = _Field()
    jurisdiction = _Field()
    introduced_on = _Field()
    last_action_on = _Field()
    subjects = _Field()
    sponsors = _Field()
    actions = _Field()
    versions = _Field()
    url = _Field()
    fetch_method = _Field()
    http_status = _Field()
    extras = _Field()


class ActionItem(_Item):
    """One docket line, when a portal serves them on their own page."""

    identifier = _Field()
    date = _Field()
    description = _Field()
    chamber = _Field()
    committee = _Field()
    sequence = _Field()


class VersionItem(_Item):
    """One text version of a document."""

    identifier = _Field()
    label = _Field()
    text = _Field()
    published_on = _Field()
    url = _Field()
