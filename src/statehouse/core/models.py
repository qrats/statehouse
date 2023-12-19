"""Canonical records.

Plain dataclasses on purpose. These cross the Scrapy/Airflow boundary and get
serialised into XCom, S3 and Postgres; keeping them dependency-free means the
transform and quality layers can be tested without a scheduler or a database.

Validation happens in ``__post_init__`` and raises
:class:`~statehouse.core.errors.ValidationError` — a record that reaches the
load layer is already known to be well formed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from typing import Any

from statehouse.core.clock import ensure_utc, isoformat
from statehouse.core.enums import (
    ActionKind,
    BillStatus,
    Chamber,
    DocumentKind,
    FetchMethod,
    RunState,
    Severity,
    SponsorRole,
)
from statehouse.core.errors import ValidationError
from statehouse.core.hashing import content_digest, field_digest, semantic_digest
from statehouse.core.ids import canonical_citation, document_id

__all__ = [
    "SourceRef",
    "RawFetch",
    "Sponsor",
    "Action",
    "DocumentVersion",
    "Document",
    "QualityFinding",
    "IngestRun",
    "Watermark",
    "to_jsonable",
]


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses, enums and datetimes to JSON types."""
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, datetime):
        return isoformat(value)
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value") and hasattr(value, "name"):  # enum member
        return value.value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    return str(value)


@dataclass(frozen=True)
class SourceRef:
    """Where a record came from, precisely enough to go back and look."""

    jurisdiction: str
    url: str
    method: FetchMethod = FetchMethod.HTTP
    fetched_at: datetime | None = None
    http_status: int | None = None
    spider: str | None = None

    def __post_init__(self) -> None:
        if not self.jurisdiction:
            raise ValidationError("SourceRef requires a jurisdiction")
        if not self.url:
            raise ValidationError("SourceRef requires a url", jurisdiction=self.jurisdiction)
        object.__setattr__(self, "jurisdiction", self.jurisdiction.lower())
        object.__setattr__(self, "method", FetchMethod.parse(self.method))
        if self.fetched_at is not None:
            object.__setattr__(self, "fetched_at", ensure_utc(self.fetched_at))


@dataclass
class RawFetch:
    """One retrieval, before anything has been read out of it.

    Archived verbatim so a parser bug is a reprocess rather than a re-scrape —
    which matters when the source only keeps ninety days of history.
    """

    source: SourceRef
    body: bytes
    content_type: str = "text/html"
    encoding: str = "utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.body, str):
            self.body = self.body.encode(self.encoding, errors="replace")
        if not self.digest:
            self.digest = content_digest(self.body)
        self.headers = {str(k).lower(): str(v) for k, v in self.headers.items()}

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding, errors="replace")

    @property
    def size_bytes(self) -> int:
        return len(self.body)

    def archive_key(self) -> str:
        """Object key for the raw lake, partitioned by jurisdiction and day."""
        day = (self.source.fetched_at or ensure_utc(datetime(1970, 1, 1))).date().isoformat()
        return f"raw/{self.source.jurisdiction}/{day}/{self.digest}.bin"


@dataclass
class Sponsor:
    """A person or committee attached to a document."""

    name: str
    role: SponsorRole = SponsorRole.UNKNOWN
    party: str | None = None
    district: str | None = None
    chamber: Chamber = Chamber.UNKNOWN

    def __post_init__(self) -> None:
        self.name = " ".join((self.name or "").split())
        if not self.name:
            raise ValidationError("Sponsor requires a name")
        self.role = SponsorRole.parse(self.role, SponsorRole.UNKNOWN)
        self.chamber = Chamber.parse(self.chamber, Chamber.UNKNOWN)
        if self.party:
            self.party = self.party.strip().upper()[:4] or None


@dataclass
class Action:
    """One line of a document's docket."""

    occurred_on: date
    description: str
    kind: ActionKind = ActionKind.UNKNOWN
    chamber: Chamber = Chamber.UNKNOWN
    committee: str | None = None
    resulting_status: BillStatus | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.occurred_on, datetime):
            self.occurred_on = self.occurred_on.date()
        if not isinstance(self.occurred_on, date):
            raise ValidationError("Action requires a date", description=self.description)
        self.description = " ".join((self.description or "").split())
        if not self.description:
            raise ValidationError("Action requires a description")
        self.kind = ActionKind.parse(self.kind, ActionKind.UNKNOWN)
        self.chamber = Chamber.parse(self.chamber, Chamber.UNKNOWN)
        if self.resulting_status is not None:
            self.resulting_status = BillStatus.parse(self.resulting_status, BillStatus.UNKNOWN)
        if self.sequence < 0:
            raise ValidationError("Action sequence cannot be negative")

    def sort_key(self) -> tuple[date, int, str]:
        return (self.occurred_on, self.sequence, self.description)


@dataclass
class DocumentVersion:
    """One observed revision of a document's text."""

    label: str
    text: str
    published_on: date | None = None
    source: SourceRef | None = None
    semantic_hash: str = ""
    byte_hash: str = ""

    def __post_init__(self) -> None:
        self.label = " ".join((self.label or "").split()) or "unlabelled"
        if self.text is None:
            raise ValidationError("DocumentVersion requires text", label=self.label)
        if not self.semantic_hash:
            self.semantic_hash = semantic_digest(self.text)
        if not self.byte_hash:
            self.byte_hash = content_digest(self.text)

    @property
    def length(self) -> int:
        return len(self.text)


@dataclass
class Document:
    """The canonical record: one bill, resolution or regulation."""

    jurisdiction: str
    session: str
    identifier: str
    title: str
    kind: DocumentKind = DocumentKind.BILL
    chamber: Chamber = Chamber.UNKNOWN
    status: BillStatus = BillStatus.UNKNOWN
    introduced_on: date | None = None
    last_action_on: date | None = None
    summary: str = ""
    subjects: list[str] = field(default_factory=list)
    sponsors: list[Sponsor] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    versions: list[DocumentVersion] = field(default_factory=list)
    source: SourceRef | None = None
    observed_at: datetime | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.jurisdiction = (self.jurisdiction or "").strip().lower()
        if not self.jurisdiction:
            raise ValidationError("Document requires a jurisdiction")
        self.session = " ".join((self.session or "").split())
        if not self.session:
            raise ValidationError("Document requires a session", jurisdiction=self.jurisdiction)
        self.identifier = " ".join((self.identifier or "").split())
        if not self.identifier:
            raise ValidationError("Document requires an identifier", jurisdiction=self.jurisdiction)
        self.title = " ".join((self.title or "").split())
        self.kind = DocumentKind.parse(self.kind, DocumentKind.UNKNOWN)
        self.chamber = Chamber.parse(self.chamber, Chamber.UNKNOWN)
        self.status = BillStatus.parse(self.status, BillStatus.UNKNOWN)
        # De-duplicated case-insensitively, keeping the first spelling seen:
        # portals mix "Health" and "HEALTH" within one page.
        folded: dict[str, str] = {}
        for subject in self.subjects:
            cleaned = " ".join((subject or "").split())
            if cleaned:
                folded.setdefault(cleaned.lower(), cleaned)
        self.subjects = [folded[key] for key in sorted(folded)]
        if self.observed_at is not None:
            self.observed_at = ensure_utc(self.observed_at)
        if self.introduced_on and self.last_action_on and self.last_action_on < self.introduced_on:
            raise ValidationError(
                "last_action_on precedes introduced_on",
                identifier=self.identifier,
                jurisdiction=self.jurisdiction,
            )

    @property
    def document_id(self) -> str:
        return document_id(self.jurisdiction, self.session, self.identifier, kind=self.kind.value)

    @property
    def citation(self) -> str:
        return canonical_citation(self.jurisdiction, self.session, self.identifier)

    @property
    def latest_version(self) -> DocumentVersion | None:
        if not self.versions:
            return None
        dated = [v for v in self.versions if v.published_on]
        if dated:
            return max(dated, key=lambda v: v.published_on)  # type: ignore[arg-type,return-value]
        return self.versions[-1]

    def metadata_fingerprint(self) -> str:
        """Digest of the fields that make this a metadata-only change."""
        return field_digest(
            {
                "title": self.title,
                "summary": self.summary,
                "chamber": self.chamber.value,
                "kind": self.kind.value,
                "subjects": self.subjects,
                "sponsors": [f"{s.role.value}:{s.name}" for s in self.sponsors],
                "introduced_on": self.introduced_on.isoformat() if self.introduced_on else "",
            }
        )

    def ordered_actions(self) -> list[Action]:
        return sorted(self.actions, key=Action.sort_key)

    def with_status(self, status: BillStatus) -> Document:
        return replace(self, status=BillStatus.parse(status, BillStatus.UNKNOWN))

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["document_id"] = self.document_id
        payload["citation"] = self.citation
        return payload


@dataclass
class QualityFinding:
    """One thing a quality check noticed."""

    check: str
    severity: Severity
    message: str
    subject: str | None = None
    observed: Any = None
    expected: Any = None

    def __post_init__(self) -> None:
        if not self.check:
            raise ValidationError("QualityFinding requires a check name")
        self.severity = Severity.parse(self.severity, Severity.WARN)
        self.message = " ".join((self.message or "").split())

    @property
    def blocking(self) -> bool:
        return self.severity.rank >= Severity.ERROR.rank


@dataclass
class IngestRun:
    """Bookkeeping for one execution of one jurisdiction's pipeline."""

    run_id: str
    jurisdiction: str
    started_at: datetime
    state: RunState = RunState.PENDING
    finished_at: datetime | None = None
    documents_seen: int = 0
    documents_written: int = 0
    documents_skipped: int = 0
    fetch_failures: int = 0
    findings: list[QualityFinding] = field(default_factory=list)
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValidationError("IngestRun requires a run_id")
        self.jurisdiction = (self.jurisdiction or "").strip().lower()
        self.started_at = ensure_utc(self.started_at)
        if self.finished_at is not None:
            self.finished_at = ensure_utc(self.finished_at)
        self.state = RunState.parse(self.state, RunState.PENDING)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def blocking_findings(self) -> list[QualityFinding]:
        return [f for f in self.findings if f.blocking]

    def finish(self, state: RunState, at: datetime) -> IngestRun:
        self.state = RunState.parse(state, RunState.FAILED)
        self.finished_at = ensure_utc(at)
        return self


@dataclass
class Watermark:
    """How far a jurisdiction has been ingested.

    ``position`` is the source-defined cursor (a page token, a max updated-at,
    a session-relative sequence). ``observed_through`` is the wall-clock
    boundary the pipeline is confident about, which is what the scheduler uses
    to decide whether a jurisdiction is stale.
    """

    jurisdiction: str
    stream: str
    position: str = ""
    observed_through: datetime | None = None
    updated_at: datetime | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        self.jurisdiction = (self.jurisdiction or "").strip().lower()
        if not self.jurisdiction:
            raise ValidationError("Watermark requires a jurisdiction")
        self.stream = (self.stream or "").strip() or "default"
        if self.observed_through is not None:
            self.observed_through = ensure_utc(self.observed_through)
        if self.updated_at is not None:
            self.updated_at = ensure_utc(self.updated_at)
        if self.revision < 0:
            raise ValidationError("Watermark revision cannot be negative")

    @property
    def key(self) -> str:
        return f"{self.jurisdiction}:{self.stream}"

    def is_stale(self, now: datetime, max_age_seconds: float) -> bool:
        """True when the watermark has not moved recently enough.

        A watermark that has never been set is stale by definition.
        """
        if self.observed_through is None:
            return True
        return (ensure_utc(now) - self.observed_through).total_seconds() > max_age_seconds
