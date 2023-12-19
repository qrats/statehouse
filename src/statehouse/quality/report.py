"""Rendering quality findings for humans and for the run record."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from statehouse.core.enums import Severity
from statehouse.core.models import QualityFinding

__all__ = ["QualityReport", "summarise", "render_text"]


@dataclass
class QualityReport:
    """Findings grouped for presentation."""

    jurisdiction: str
    run_id: str
    documents: int
    findings: list[QualityFinding] = field(default_factory=list)

    @property
    def by_severity(self) -> dict[str, int]:
        counts = {level.value: 0 for level in Severity}
        for finding in self.findings:
            counts[finding.severity.value] += 1
        return counts

    @property
    def by_check(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.check] = counts.get(finding.check, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def clean(self) -> bool:
        return not any(f.blocking for f in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "jurisdiction": self.jurisdiction,
            "run_id": self.run_id,
            "documents": self.documents,
            "by_severity": self.by_severity,
            "by_check": self.by_check,
            "findings": [
                {
                    "check": f.check,
                    "severity": f.severity.value,
                    "message": f.message,
                    "subject": f.subject,
                    "observed": f.observed,
                    "expected": f.expected,
                }
                for f in self.findings
            ],
        }


def summarise(
    jurisdiction: str,
    run_id: str,
    documents: int,
    findings: Sequence[QualityFinding],
) -> QualityReport:
    """Build a report, keeping the findings in the order they arrived."""
    return QualityReport(
        jurisdiction=jurisdiction,
        run_id=run_id,
        documents=documents,
        findings=list(findings),
    )


def render_text(report: QualityReport, *, limit: int = 20) -> str:
    """Plain-text rendering for logs and the CLI.

    Truncates to ``limit`` findings with a count of the remainder, because a
    broken adapter can produce thousands and nobody reads past the first page.
    """
    lines = [
        f"quality report {report.jurisdiction} run={report.run_id} documents={report.documents}",
    ]
    counts = report.by_severity
    lines.append(
        "  " + " ".join(f"{name}={counts[name]}" for name in ("critical", "error", "warn", "info"))
    )
    for finding in report.findings[:limit]:
        subject = f" [{finding.subject}]" if finding.subject else ""
        lines.append(f"  {finding.severity.value:>8}  {finding.check}{subject}: {finding.message}")
    remaining = len(report.findings) - limit
    if remaining > 0:
        lines.append(f"  ... and {remaining} more")
    return "\n".join(lines)
