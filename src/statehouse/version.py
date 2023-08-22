"""Single source of truth for the distribution version.

Kept in its own module so that ``statehouse/__init__.py`` stays import-light:
the Airflow DAG files read the version to stamp run metadata and we do not want
that to drag the whole package graph in.
"""

from __future__ import annotations

__all__ = ["VERSION", "version_tuple"]

VERSION = "0.1.0"


def version_tuple() -> tuple[int, ...]:
    """Return ``VERSION`` split into integer components.

    Pre-release suffixes (``0.2.0rc1``) are truncated at the first
    non-numeric character so the result stays comparable.
    """
    parts: list[int] = []
    for chunk in VERSION.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts)
