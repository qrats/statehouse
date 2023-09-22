"""Sponsor parsing.

Sponsor cells are the messiest field on any portal: ``"Smith, J. (R-14)"``,
``"Rep. Jane Smith (D) District 14"``, ``"SMITH"``, and ``"Committee on Ways
and Means"`` all appear, sometimes in one list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from statehouse.core.enums import Chamber, SponsorRole
from statehouse.core.models import Sponsor
from statehouse.utils.text import clean_text, split_list, titlecase_name

__all__ = ["parse_sponsor", "parse_sponsor_list", "merge_sponsors", "PARTY_ALIASES"]

PARTY_ALIASES: dict[str, str] = {
    "d": "D",
    "dem": "D",
    "democrat": "D",
    "democratic": "D",
    "r": "R",
    "rep": "R",
    "republican": "R",
    "i": "I",
    "ind": "I",
    "independent": "I",
    "l": "L",
    "libertarian": "L",
    "g": "G",
    "green": "G",
    "np": "NP",
    "nonpartisan": "NP",
}

_TITLE = re.compile(
    r"^(?:the\s+)?(?:hon\.?|honorable|rep\.?|representative|sen\.?|senator|"
    r"assemblyman|assemblywoman|assembly\s?member|delegate|del\.?|mr\.?|mrs\.?|ms\.?|dr\.?)\s+",
    re.IGNORECASE,
)
_PAREN = re.compile(r"\(([^)]*)\)")
_PARTY_DISTRICT = re.compile(r"^\s*([A-Za-z]{1,12})\s*[-–/]\s*([A-Za-z0-9\- ]{1,12})\s*$")
_DISTRICT_WORD = re.compile(r"(?i)\bdistrict\s+([A-Za-z0-9\-]+)")
_COMMITTEE = re.compile(r"(?i)\b(committee|caucus|delegation|subcommittee)\b")
_TRAILING_ROLE = re.compile(r"(?i)\s*[\-–,]\s*(primary|co-?sponsor|lead|author|coauthor)\s*$")

_ROLE_WORDS = {
    "primary": SponsorRole.PRIMARY,
    "lead": SponsorRole.PRIMARY,
    "author": SponsorRole.PRIMARY,
    "sponsor": SponsorRole.PRIMARY,
    "cosponsor": SponsorRole.COSPONSOR,
    "co-sponsor": SponsorRole.COSPONSOR,
    "coauthor": SponsorRole.COSPONSOR,
    "requestor": SponsorRole.REQUESTOR,
}

_CHAMBER_HINTS = (
    (re.compile(r"(?i)\b(sen\.?|senator)\b"), Chamber.UPPER),
    (re.compile(r"(?i)\b(rep\.?|representative|assembly|delegate|del\.?)\b"), Chamber.LOWER),
)


def _normalise_party(raw: str) -> str | None:
    key = raw.strip().strip(".").lower()
    if not key:
        return None
    return PARTY_ALIASES.get(key, raw.strip().upper()[:4] or None)


def parse_sponsor(
    raw: str,
    *,
    default_role: SponsorRole = SponsorRole.UNKNOWN,
    default_chamber: Chamber = Chamber.UNKNOWN,
) -> Sponsor | None:
    """Parse one sponsor cell into a :class:`Sponsor`.

    Returns ``None`` when the cell holds no name at all — portals pad
    fixed-width sponsor lists with placeholders like ``"--"`` and ``"None"``.
    Committees are recognised by name and given
    :attr:`SponsorRole.COMMITTEE`, overriding ``default_role``.
    """
    text = clean_text(raw or "", keep_paragraphs=False)
    if not text or text.strip("-–—.") == "" or text.lower() in {"none", "n/a", "na", "vacant"}:
        return None

    role = default_role
    role_match = _TRAILING_ROLE.search(text)
    if role_match:
        role = _ROLE_WORDS.get(role_match.group(1).lower().replace("-", ""), role)
        text = _TRAILING_ROLE.sub("", text)

    chamber = default_chamber
    for pattern, hinted in _CHAMBER_HINTS:
        if pattern.search(text):
            chamber = hinted
            break

    party: str | None = None
    district: str | None = None
    for group in _PAREN.findall(text):
        inner = group.strip()
        combined = _PARTY_DISTRICT.match(inner)
        if combined:
            party = _normalise_party(combined.group(1))
            district = combined.group(2).strip() or None
            continue
        if inner.lower() in PARTY_ALIASES or (len(inner) <= 4 and inner.isalpha()):
            party = _normalise_party(inner)
            continue
        district_word = _DISTRICT_WORD.search(inner)
        if district_word:
            district = district_word.group(1)

    outside_district = _DISTRICT_WORD.search(text)
    if district is None and outside_district:
        district = outside_district.group(1)

    name = _PAREN.sub(" ", text)
    name = _DISTRICT_WORD.sub(" ", name)
    name = _TITLE.sub("", clean_text(name, keep_paragraphs=False)).strip(" ,;-")
    if not name:
        return None

    if _COMMITTEE.search(name):
        return Sponsor(
            name=clean_text(name, keep_paragraphs=False),
            role=SponsorRole.COMMITTEE,
            chamber=chamber,
        )

    return Sponsor(
        name=titlecase_name(name),
        role=role,
        party=party,
        district=district,
        chamber=chamber,
    )


def parse_sponsor_list(
    raw: str | Iterable[str],
    *,
    primary_first: bool = True,
    default_chamber: Chamber = Chamber.UNKNOWN,
) -> list[Sponsor]:
    """Parse a whole sponsor cell or an already-split list.

    With ``primary_first`` the first parsed entry is promoted to
    :attr:`SponsorRole.PRIMARY` and the rest to
    :attr:`SponsorRole.COSPONSOR`, unless an explicit role was found in the
    text or the entry is a committee. That convention holds for every portal
    in the registry.
    """
    cells = split_list(raw) if isinstance(raw, str) else [str(item) for item in raw]
    parsed: list[Sponsor] = []
    for index, cell in enumerate(cells):
        default_role = SponsorRole.UNKNOWN
        if primary_first:
            default_role = SponsorRole.PRIMARY if index == 0 else SponsorRole.COSPONSOR
        sponsor = parse_sponsor(cell, default_role=default_role, default_chamber=default_chamber)
        if sponsor is not None:
            parsed.append(sponsor)
    return parsed


def merge_sponsors(existing: Iterable[Sponsor], incoming: Iterable[Sponsor]) -> list[Sponsor]:
    """Merge two sponsor lists, keyed on the normalised name.

    Incoming entries fill in blanks (party, district, chamber) but never
    downgrade a known value to unknown, and never demote a primary sponsor to
    a cosponsor. Order follows ``existing`` first, then newcomers in the order
    they arrived.
    """
    merged: dict[str, Sponsor] = {}
    order: list[str] = []

    for sponsor in list(existing) + list(incoming):
        key = sponsor.name.lower()
        if key not in merged:
            merged[key] = sponsor
            order.append(key)
            continue
        current = merged[key]
        role = current.role
        if current.role is SponsorRole.UNKNOWN:
            role = sponsor.role
        elif sponsor.role is SponsorRole.PRIMARY:
            role = SponsorRole.PRIMARY
        merged[key] = Sponsor(
            name=current.name,
            role=role,
            party=current.party or sponsor.party,
            district=current.district or sponsor.district,
            chamber=current.chamber if current.chamber is not Chamber.UNKNOWN else sponsor.chamber,
        )

    return [merged[key] for key in order]
