"""Deriving a canonical status from a docket.

Portals disagree about almost everything, but they all publish a list of
dated actions in the jurisdiction's own words. Rather than trust a portal's
own status field — several of them are stale, and two are simply wrong for
vetoed bills — the canonical status is derived from the actions.

The ordering below is the legislative progression, and it is what makes
"advanced" and "regressed" meaningful in the change feed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from statehouse.core.enums import ActionKind, BillStatus, Chamber
from statehouse.core.models import Action

__all__ = [
    "STATUS_ORDER",
    "TERMINAL_STATUSES",
    "status_rank",
    "classify_action",
    "status_from_action",
    "derive_status",
    "is_advance",
]

#: Progression order. Terminal outcomes sit above every procedural step.
STATUS_ORDER: tuple[BillStatus, ...] = (
    BillStatus.UNKNOWN,
    BillStatus.PREFILED,
    BillStatus.INTRODUCED,
    BillStatus.IN_COMMITTEE,
    BillStatus.REPORTED,
    BillStatus.ON_FLOOR,
    BillStatus.PASSED_ORIGIN,
    BillStatus.PASSED_SECOND,
    BillStatus.ENROLLED,
    BillStatus.SENT_TO_EXECUTIVE,
    BillStatus.VETOED,
    BillStatus.SIGNED,
    BillStatus.VETO_OVERRIDDEN,
    BillStatus.ENACTED,
)

#: Statuses from which a bill does not progress further this session.
TERMINAL_STATUSES: frozenset[BillStatus] = frozenset(
    {
        BillStatus.ENACTED,
        BillStatus.FAILED,
        BillStatus.WITHDRAWN,
        BillStatus.DEAD,
        BillStatus.VETO_OVERRIDDEN,
    }
)

_RANKS: dict[BillStatus, int] = {status: index for index, status in enumerate(STATUS_ORDER)}

# Ordered most-specific first: the first pattern that matches wins.
# One row per docket phrase, aligned so the table can be scanned.
# fmt: off
_ACTION_PATTERNS: tuple[tuple[re.Pattern[str], ActionKind, BillStatus | None], ...] = (
    (re.compile(r"\bveto\s+overrid(?:den|e)\b"), ActionKind.FLOOR_VOTE, BillStatus.VETO_OVERRIDDEN),
    (re.compile(r"\boverrode\s+(?:the\s+)?veto\b"), ActionKind.FLOOR_VOTE, BillStatus.VETO_OVERRIDDEN),
    (re.compile(r"\bveto(?:ed)?\b"), ActionKind.EXECUTIVE_ACTION, BillStatus.VETOED),
    (re.compile(r"\bbecame\s+law\b"), ActionKind.EXECUTIVE_ACTION, BillStatus.ENACTED),
    (re.compile(r"\bchaptered\b"), ActionKind.EXECUTIVE_ACTION, BillStatus.ENACTED),
    (re.compile(r"\bact\s+no\.?\s*\d+"), ActionKind.EXECUTIVE_ACTION, BillStatus.ENACTED),
    (re.compile(r"\bpublic\s+law\b"), ActionKind.EXECUTIVE_ACTION, BillStatus.ENACTED),
    (re.compile(r"\bapproved\s+by\s+(?:the\s+)?governor\b"), ActionKind.EXECUTIVE_ACTION, BillStatus.SIGNED),
    (re.compile(r"\bsigned\s+by\s+(?:the\s+)?(?:governor|president)\b"), ActionKind.EXECUTIVE_ACTION, BillStatus.SIGNED),
    (re.compile(r"\bsent\s+to\s+(?:the\s+)?(?:governor|president)\b"), ActionKind.TRANSMITTAL, BillStatus.SENT_TO_EXECUTIVE),
    (re.compile(r"\bpresented\s+to\s+(?:the\s+)?(?:governor|president)\b"), ActionKind.TRANSMITTAL, BillStatus.SENT_TO_EXECUTIVE),
    (re.compile(r"\benroll(?:ed|ment)\b"), ActionKind.PROCEDURAL, BillStatus.ENROLLED),
    (re.compile(r"\bwithdrawn\b"), ActionKind.PROCEDURAL, BillStatus.WITHDRAWN),
    (re.compile(r"\bindefinitely\s+postponed\b"), ActionKind.PROCEDURAL, BillStatus.DEAD),
    (re.compile(r"\bdied\s+in\s+committee\b"), ActionKind.PROCEDURAL, BillStatus.DEAD),
    (re.compile(r"\bfailed\s+to\s+pass\b"), ActionKind.FLOOR_VOTE, BillStatus.FAILED),
    (re.compile(r"\blost\b.*\bvote\b"), ActionKind.FLOOR_VOTE, BillStatus.FAILED),
    (re.compile(r"\bpassed\b"), ActionKind.FLOOR_VOTE, None),
    (re.compile(r"\bthird\s+reading\b"), ActionKind.FLOOR_VOTE, BillStatus.ON_FLOOR),
    (re.compile(r"\bplaced\s+on\s+(?:the\s+)?calendar\b"), ActionKind.PROCEDURAL, BillStatus.ON_FLOOR),
    (re.compile(r"\breported\s+(?:out|favorably|do\s+pass)\b"), ActionKind.COMMITTEE_VOTE, BillStatus.REPORTED),
    (re.compile(r"\bdo\s+pass\b"), ActionKind.COMMITTEE_VOTE, BillStatus.REPORTED),
    (re.compile(r"\bcommittee\s+report\b"), ActionKind.COMMITTEE_VOTE, BillStatus.REPORTED),
    (re.compile(r"\breferred\s+to\b"), ActionKind.REFERRAL, BillStatus.IN_COMMITTEE),
    (re.compile(r"\bassigned\s+to\s+committee\b"), ActionKind.REFERRAL, BillStatus.IN_COMMITTEE),
    (re.compile(r"\bhearing\s+(?:scheduled|held|set)\b"), ActionKind.HEARING, BillStatus.IN_COMMITTEE),
    (re.compile(r"\bamendment\b"), ActionKind.AMENDMENT_OFFERED, None),
    (re.compile(r"\bsubstitut(?:ed|e)\b"), ActionKind.SUBSTITUTION, None),
    (re.compile(r"\bsecond\s+reading\b"), ActionKind.PROCEDURAL, BillStatus.ON_FLOOR),
    (re.compile(r"\bfirst\s+reading\b"), ActionKind.FILING, BillStatus.INTRODUCED),
    (re.compile(r"\bintroduced\b"), ActionKind.FILING, BillStatus.INTRODUCED),
    (re.compile(r"\bread\s+(?:the\s+)?first\s+time\b"), ActionKind.FILING, BillStatus.INTRODUCED),
    (re.compile(r"\bfiled\b"), ActionKind.FILING, BillStatus.INTRODUCED),
    (re.compile(r"\bprefiled\b"), ActionKind.FILING, BillStatus.PREFILED),
    (re.compile(r"\bpre-?filed\b"), ActionKind.FILING, BillStatus.PREFILED),
)
# fmt: on

_ORIGIN_HINT = re.compile(r"\b(house|senate|assembly)\b")


def status_rank(status: BillStatus | str) -> int:
    """Position of ``status`` in the progression.

    Terminal-but-unranked outcomes (failed, withdrawn, dead) sit at the very
    top: once a bill is dead nothing outranks that, and treating them as
    low-rank would let a stale procedural line resurrect the bill.
    """
    parsed = BillStatus.parse(status, BillStatus.UNKNOWN)
    if parsed in _RANKS:
        return _RANKS[parsed]
    return len(STATUS_ORDER)


def classify_action(description: str) -> ActionKind:
    """Best-effort classification of a docket line."""
    text = (description or "").lower()
    for pattern, kind, _status in _ACTION_PATTERNS:
        if pattern.search(text):
            return kind
    return ActionKind.UNKNOWN


def status_from_action(
    description: str,
    *,
    chamber: Chamber = Chamber.UNKNOWN,
    origin: Chamber = Chamber.UNKNOWN,
) -> BillStatus | None:
    """Status implied by one docket line, or ``None`` when it implies none.

    A bare "Passed" is ambiguous: whether it means the origin chamber or the
    second depends on where the action happened. When the acting chamber
    matches the bill's origin the result is ``passed_origin``; when it is the
    other chamber, ``passed_second``; when we cannot tell, ``passed_origin``,
    because a bill's first passage is by far the more common observation.
    """
    text = (description or "").lower()
    for pattern, _kind, status in _ACTION_PATTERNS:
        if not pattern.search(text):
            continue
        if status is not None:
            return status
        if pattern.pattern == r"\bpassed\b":
            return _resolve_passage(text, chamber, origin)
        return None
    return None


def _resolve_passage(text: str, chamber: Chamber, origin: Chamber) -> BillStatus:
    acting = chamber
    if acting is Chamber.UNKNOWN:
        hint = _ORIGIN_HINT.search(text)
        if hint:
            acting = Chamber.UPPER if hint.group(1) == "senate" else Chamber.LOWER
    if acting is Chamber.UNKNOWN or origin is Chamber.UNKNOWN:
        return BillStatus.PASSED_ORIGIN
    return BillStatus.PASSED_ORIGIN if acting is origin else BillStatus.PASSED_SECOND


def is_advance(previous: BillStatus | str, current: BillStatus | str) -> bool:
    """True when ``current`` is further along the progression than ``previous``."""
    return status_rank(current) > status_rank(previous)


def derive_status(
    actions: Iterable[Action],
    *,
    origin: Chamber = Chamber.UNKNOWN,
    fallback: BillStatus = BillStatus.UNKNOWN,
) -> BillStatus:
    """Derive the canonical status from a document's docket.

    Actions are considered in date order. The result is the status implied by
    the *latest* action that implies one, not the highest-ranked: a bill that
    passes and is then withdrawn is withdrawn. Where several actions share a
    date, the higher-ranked one wins, because portals routinely record a whole
    day's proceedings with no intra-day ordering. An empty docket, or one
    where nothing is recognisable, yields ``fallback``.
    """
    ordered: Sequence[Action] = sorted(actions, key=Action.sort_key)
    if not ordered:
        return fallback

    best: BillStatus | None = None
    best_key: tuple[object, ...] | None = None

    for action in ordered:
        implied = action.resulting_status or status_from_action(
            action.description, chamber=action.chamber, origin=origin
        )
        if implied is None:
            continue
        key = (action.occurred_on, action.sequence, status_rank(implied))
        if best_key is None or key >= best_key:
            best = implied
            best_key = key

    return best if best is not None else fallback
