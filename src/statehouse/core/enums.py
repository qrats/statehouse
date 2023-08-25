"""Closed vocabularies.

Every one of these is a ``str`` enum so that the value round-trips through
JSON, Postgres and the search index without a codec, and so that comparisons
against raw strings read naturally in the spiders.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "Chamber",
    "DocumentKind",
    "BillStatus",
    "ActionKind",
    "FetchMethod",
    "RunState",
    "Severity",
    "ChangeKind",
    "SponsorRole",
]


class _StrEnum(str, Enum):
    """``str`` enum with a forgiving parser."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)

    @classmethod
    def parse(cls, value: object, default: "_StrEnum | None" = None) -> "_StrEnum":
        """Coerce ``value`` to a member, case- and separator-insensitively.

        ``"IN COMMITTEE"``, ``"in-committee"`` and ``"in_committee"`` all reach
        the same member. Returns ``default`` when given something unusable and a
        default was supplied; otherwise raises ``ValueError``.
        """
        if isinstance(value, cls):
            return value
        if value is not None:
            text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
            for member in cls:
                if member.value == text or member.name.lower() == text:
                    return member
        if default is not None:
            return default
        raise ValueError(f"{value!r} is not a valid {cls.__name__}")


class Chamber(_StrEnum):
    """Which body a document belongs to."""

    UPPER = "upper"
    LOWER = "lower"
    JOINT = "joint"
    EXECUTIVE = "executive"
    AGENCY = "agency"
    UNKNOWN = "unknown"


class DocumentKind(_StrEnum):
    """Top-level document taxonomy."""

    BILL = "bill"
    RESOLUTION = "resolution"
    AMENDMENT = "amendment"
    COMMITTEE_REPORT = "committee_report"
    FISCAL_NOTE = "fiscal_note"
    VETO_MESSAGE = "veto_message"
    REGULATION = "regulation"
    NOTICE = "notice"
    EXECUTIVE_ORDER = "executive_order"
    TRANSCRIPT = "transcript"
    UNKNOWN = "unknown"


class BillStatus(_StrEnum):
    """Canonical status, normalised from wildly different portal wording."""

    PREFILED = "prefiled"
    INTRODUCED = "introduced"
    IN_COMMITTEE = "in_committee"
    REPORTED = "reported"
    ON_FLOOR = "on_floor"
    PASSED_ORIGIN = "passed_origin"
    PASSED_SECOND = "passed_second"
    ENROLLED = "enrolled"
    SENT_TO_EXECUTIVE = "sent_to_executive"
    SIGNED = "signed"
    VETOED = "vetoed"
    VETO_OVERRIDDEN = "veto_overridden"
    ENACTED = "enacted"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"
    DEAD = "dead"
    UNKNOWN = "unknown"


class ActionKind(_StrEnum):
    """The kind of event a single docket line records."""

    FILING = "filing"
    REFERRAL = "referral"
    HEARING = "hearing"
    COMMITTEE_VOTE = "committee_vote"
    FLOOR_VOTE = "floor_vote"
    AMENDMENT_OFFERED = "amendment_offered"
    SUBSTITUTION = "substitution"
    TRANSMITTAL = "transmittal"
    EXECUTIVE_ACTION = "executive_action"
    PROCEDURAL = "procedural"
    UNKNOWN = "unknown"


class FetchMethod(_StrEnum):
    """How a document was retrieved. Drives cost accounting and politeness."""

    HTTP = "http"
    BROWSER = "browser"
    API = "api"
    BULK = "bulk"
    FTP = "ftp"


class RunState(_StrEnum):
    """Lifecycle of an ingest run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    ABANDONED = "abandoned"

    @property
    def terminal(self) -> bool:
        return self in (RunState.SUCCEEDED, RunState.PARTIAL, RunState.FAILED, RunState.ABANDONED)


class Severity(_StrEnum):
    """Severity of a quality finding."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warn": 1, "error": 2, "critical": 3}[self.value]


class ChangeKind(_StrEnum):
    """What a diff between two observations of a document represents."""

    NEW = "new"
    TEXT_REVISED = "text_revised"
    METADATA_ONLY = "metadata_only"
    STATUS_ADVANCED = "status_advanced"
    STATUS_REGRESSED = "status_regressed"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    UNCHANGED = "unchanged"


class SponsorRole(_StrEnum):
    """Sponsorship role on a document."""

    PRIMARY = "primary"
    COSPONSOR = "cosponsor"
    COMMITTEE = "committee"
    REQUESTOR = "requestor"
    UNKNOWN = "unknown"
