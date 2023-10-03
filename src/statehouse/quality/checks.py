"""Per-batch quality checks.

Each check takes the batch and returns findings. They are pure functions over
canonical records — no database, no network — which is why they can run in the
same task that built the batch, before anything is written.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta

from statehouse.core.enums import BillStatus, Chamber, DocumentKind, Severity
from statehouse.core.models import Document, QualityFinding
from statehouse.transform.status import TERMINAL_STATUSES

__all__ = ["CHECKS", "run_checks", "CheckFn"]

CheckFn = Callable[[Sequence[Document]], list[QualityFinding]]

_MIN_TITLE_LENGTH = 8
_MAX_FUTURE_DAYS = 3
_EARLIEST_PLAUSIBLE = date(1900, 1, 1)


def check_identifier_present(batch: Sequence[Document]) -> list[QualityFinding]:
    """Every document must carry a non-trivial identifier."""
    findings: list[QualityFinding] = []
    for document in batch:
        if len(document.identifier) < 2:
            findings.append(
                QualityFinding(
                    check="identifier_present",
                    severity=Severity.ERROR,
                    message="identifier is too short to be a bill designator",
                    subject=document.citation,
                    observed=document.identifier,
                )
            )
    return findings


def check_title_present(batch: Sequence[Document]) -> list[QualityFinding]:
    """A missing title usually means the detail page did not render."""
    findings: list[QualityFinding] = []
    for document in batch:
        if len(document.title) < _MIN_TITLE_LENGTH:
            findings.append(
                QualityFinding(
                    check="title_present",
                    severity=Severity.WARN if document.title else Severity.ERROR,
                    message="title missing or implausibly short",
                    subject=document.citation,
                    observed=document.title,
                    expected=f"at least {_MIN_TITLE_LENGTH} characters",
                )
            )
    return findings


def check_dates_plausible(batch: Sequence[Document]) -> list[QualityFinding]:
    """Dates must not be in the future or before the twentieth century."""
    ceiling = max((d.observed_at.date() for d in batch if d.observed_at), default=None)
    findings: list[QualityFinding] = []
    for document in batch:
        limit = (ceiling or date.today()) + timedelta(days=_MAX_FUTURE_DAYS)
        for label, value in (
            ("introduced_on", document.introduced_on),
            ("last_action_on", document.last_action_on),
        ):
            if value is None:
                continue
            if value > limit:
                findings.append(
                    QualityFinding(
                        check="dates_plausible",
                        severity=Severity.ERROR,
                        message=f"{label} is in the future",
                        subject=document.citation,
                        observed=value.isoformat(),
                    )
                )
            elif value < _EARLIEST_PLAUSIBLE:
                findings.append(
                    QualityFinding(
                        check="dates_plausible",
                        severity=Severity.ERROR,
                        message=f"{label} predates the twentieth century",
                        subject=document.citation,
                        observed=value.isoformat(),
                    )
                )
    return findings


def check_status_supported(batch: Sequence[Document]) -> list[QualityFinding]:
    """A document past introduction should have a docket to justify it."""
    findings: list[QualityFinding] = []
    for document in batch:
        if document.status in (BillStatus.UNKNOWN, BillStatus.PREFILED):
            continue
        if not document.actions:
            findings.append(
                QualityFinding(
                    check="status_supported",
                    severity=Severity.WARN,
                    message="status set but no actions were captured",
                    subject=document.citation,
                    observed=document.status.value,
                )
            )
    return findings


def check_terminal_consistency(batch: Sequence[Document]) -> list[QualityFinding]:
    """A terminal status must be the last thing that happened."""
    findings: list[QualityFinding] = []
    for document in batch:
        if document.status not in TERMINAL_STATUSES or not document.actions:
            continue
        ordered = document.ordered_actions()
        terminal_dates = [
            action.occurred_on
            for action in ordered
            if action.resulting_status in TERMINAL_STATUSES
        ]
        if terminal_dates and ordered[-1].occurred_on > max(terminal_dates):
            findings.append(
                QualityFinding(
                    check="terminal_consistency",
                    severity=Severity.WARN,
                    message="actions recorded after the terminal action",
                    subject=document.citation,
                    observed=ordered[-1].occurred_on.isoformat(),
                )
            )
    return findings


def check_chamber_known(batch: Sequence[Document]) -> list[QualityFinding]:
    """Bills should resolve to a chamber; regulations legitimately do not."""
    findings: list[QualityFinding] = []
    for document in batch:
        if document.kind in (DocumentKind.REGULATION, DocumentKind.NOTICE):
            continue
        if document.chamber is Chamber.UNKNOWN:
            findings.append(
                QualityFinding(
                    check="chamber_known",
                    severity=Severity.WARN,
                    message="chamber could not be determined",
                    subject=document.citation,
                    observed=document.identifier,
                )
            )
    return findings


def check_no_duplicate_ids(batch: Sequence[Document]) -> list[QualityFinding]:
    """The batch must already be deduplicated before it reaches the loader."""
    seen: dict[str, int] = {}
    for document in batch:
        seen[document.document_id] = seen.get(document.document_id, 0) + 1
    return [
        QualityFinding(
            check="no_duplicate_ids",
            severity=Severity.ERROR,
            message="document id appears more than once in the batch",
            subject=document_id,
            observed=count,
            expected=1,
        )
        for document_id, count in sorted(seen.items())
        if count > 1
    ]


def check_sponsor_sanity(batch: Sequence[Document]) -> list[QualityFinding]:
    """At most one primary sponsor, and no empty sponsor lists on bills."""
    findings: list[QualityFinding] = []
    for document in batch:
        primaries = [s for s in document.sponsors if s.role.value == "primary"]
        if len(primaries) > 1:
            findings.append(
                QualityFinding(
                    check="sponsor_sanity",
                    severity=Severity.WARN,
                    message="more than one primary sponsor",
                    subject=document.citation,
                    observed=len(primaries),
                    expected=1,
                )
            )
        if document.kind is DocumentKind.BILL and not document.sponsors:
            findings.append(
                QualityFinding(
                    check="sponsor_sanity",
                    severity=Severity.INFO,
                    message="bill has no sponsors",
                    subject=document.citation,
                )
            )
    return findings


def check_batch_not_empty(batch: Sequence[Document]) -> list[QualityFinding]:
    """An empty batch is legitimate out of session but always worth saying."""
    if batch:
        return []
    return [
        QualityFinding(
            check="batch_not_empty",
            severity=Severity.INFO,
            message="batch contained no documents",
        )
    ]


CHECKS: tuple[CheckFn, ...] = (
    check_batch_not_empty,
    check_identifier_present,
    check_title_present,
    check_dates_plausible,
    check_status_supported,
    check_terminal_consistency,
    check_chamber_known,
    check_no_duplicate_ids,
    check_sponsor_sanity,
)


def run_checks(
    batch: Sequence[Document], checks: Sequence[CheckFn] | None = None
) -> list[QualityFinding]:
    """Run every check and return the findings, ordered worst-first.

    Ties break on check name then subject so the output is stable across runs
    — a report that reorders itself is a report nobody diffs.
    """
    findings: list[QualityFinding] = []
    for check in checks if checks is not None else CHECKS:
        findings.extend(check(batch))
    return sorted(
        findings,
        key=lambda f: (-f.severity.rank, f.check, f.subject or "", f.message),
    )
