"""Subject-tag normalisation.

Each portal maintains its own subject vocabulary, and clients want one. The
mapping below is curated rather than clever: an automated clustering pass was
tried and produced tags nobody could explain to a customer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from statehouse.utils.text import clean_text, split_list

__all__ = ["CANONICAL_SUBJECTS", "normalise_subject", "normalise_subjects", "subject_aliases"]

#: Canonical tag -> the source phrases that map onto it.
CANONICAL_SUBJECTS: dict[str, tuple[str, ...]] = {
    "agriculture": ("agriculture", "farming", "livestock", "crops", "agricultural policy"),
    "appropriations": ("appropriations", "budget", "state budget", "spending", "fiscal"),
    "banking": ("banking", "financial institutions", "credit unions", "banks"),
    "civil-rights": ("civil rights", "discrimination", "equal protection", "human rights"),
    "criminal-justice": (
        "criminal justice",
        "crimes",
        "criminal law",
        "corrections",
        "sentencing",
        "penal code",
    ),
    "education": ("education", "schools", "higher education", "k-12", "school districts"),
    "elections": ("elections", "voting", "campaign finance", "redistricting", "ballot"),
    "energy": ("energy", "utilities", "electricity", "renewable energy", "power"),
    "environment": (
        "environment",
        "environmental protection",
        "conservation",
        "pollution",
        "water quality",
    ),
    "health": ("health", "public health", "medicaid", "health care", "hospitals", "mental health"),
    "housing": ("housing", "landlord tenant", "affordable housing", "rent", "homelessness"),
    "immigration": ("immigration", "immigrants", "refugees"),
    "insurance": ("insurance", "health insurance", "property insurance", "insurers"),
    "labor": ("labor", "employment", "workers compensation", "wages", "unions", "minimum wage"),
    "local-government": ("local government", "counties", "municipalities", "cities", "townships"),
    "privacy": ("privacy", "data protection", "consumer data", "biometric", "data breach"),
    "public-safety": ("public safety", "law enforcement", "police", "fire", "emergency services"),
    "revenue": ("revenue", "taxation", "taxes", "tax credits", "sales tax", "income tax"),
    "technology": ("technology", "artificial intelligence", "broadband", "telecommunications"),
    "transportation": ("transportation", "highways", "motor vehicles", "transit", "roads"),
    "veterans": ("veterans", "military", "national guard", "armed forces"),
}

_ALIAS_INDEX: dict[str, str] = {}
for _canonical, _phrases in CANONICAL_SUBJECTS.items():
    _ALIAS_INDEX[_canonical] = _canonical
    for _phrase in _phrases:
        _ALIAS_INDEX[_phrase] = _canonical

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SUFFIX = re.compile(r"(?:\s*--.*|\s*\(.*\)|\s*;.*)$")


def _key(value: str) -> str:
    folded = clean_text(value, keep_paragraphs=False).lower()
    folded = _SUFFIX.sub("", folded)
    folded = _PUNCT.sub(" ", folded)
    return " ".join(folded.split())


def normalise_subject(raw: str) -> str | None:
    """Map one source subject onto a canonical tag.

    Exact matches win; failing that, a source phrase contained in the input
    wins, longest first, which handles hierarchical vocabularies like
    ``"HEALTH--MEDICAID--ELIGIBILITY"``. Unmappable input returns ``None``
    rather than inventing a tag, and the caller records it as a finding so the
    vocabulary can be extended deliberately.
    """
    key = _key(raw)
    if not key:
        return None
    if key in _ALIAS_INDEX:
        return _ALIAS_INDEX[key]
    for phrase in sorted(_ALIAS_INDEX, key=len, reverse=True):
        if len(phrase) >= 5 and phrase in key:
            return _ALIAS_INDEX[phrase]
    return None


def normalise_subjects(raw: str | Iterable[str]) -> tuple[list[str], list[str]]:
    """Normalise a subject cell.

    Returns ``(canonical_tags, unmapped)``. Canonical tags are sorted and
    de-duplicated; unmapped source phrases are returned in their cleaned form
    so a finding can name them.
    """
    cells = split_list(raw) if isinstance(raw, str) else [str(item) for item in raw]
    canonical: set[str] = set()
    unmapped: list[str] = []
    for cell in cells:
        mapped = normalise_subject(cell)
        if mapped:
            canonical.add(mapped)
        else:
            cleaned = clean_text(cell, keep_paragraphs=False)
            if cleaned and cleaned not in unmapped:
                unmapped.append(cleaned)
    return sorted(canonical), unmapped


def subject_aliases(canonical: str) -> tuple[str, ...]:
    """Source phrases known to map onto ``canonical``."""
    return CANONICAL_SUBJECTS.get(canonical, ())
