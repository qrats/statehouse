"""The gate that decides whether a batch may be written.

Separated from the checks so that the policy — how many errors are tolerable,
which checks are advisory this week — can change without touching the checks
themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from statehouse.core.enums import Severity
from statehouse.core.errors import QualityGateFailed
from statehouse.core.models import Document, QualityFinding
from statehouse.quality.checks import CheckFn, run_checks

__all__ = ["GateDecision", "QualityGate"]


@dataclass(frozen=True)
class GateDecision:
    """The outcome of running the gate over one batch."""

    passed: bool
    findings: tuple[QualityFinding, ...] = ()
    blocked_by: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def worst_severity(self) -> Severity:
        if not self.findings:
            return Severity.INFO
        return max((f.severity for f in self.findings), key=lambda s: s.rank)

    def summary(self) -> str:
        if self.passed and not self.findings:
            return "clean"
        parts = [f"{name}={count}" for name, count in sorted(self.counts.items()) if count]
        verdict = "pass" if self.passed else "block"
        return f"{verdict} ({', '.join(parts) or 'no findings'})"


@dataclass
class QualityGate:
    """Applies a tolerance policy to a batch's findings.

    ``max_errors`` is the number of ``error`` findings tolerated before the
    batch is blocked; ``critical`` always blocks regardless. ``waived_checks``
    demotes named checks to advisory, which is how a known-bad portal stays
    ingestible while someone fixes the adapter.
    """

    max_errors: int = 0
    max_error_rate: float = 0.0
    waived_checks: frozenset[str] = frozenset()
    checks: tuple[CheckFn, ...] | None = None

    def __post_init__(self) -> None:
        if self.max_errors < 0:
            raise ValueError("max_errors cannot be negative")
        if not 0.0 <= self.max_error_rate <= 1.0:
            raise ValueError("max_error_rate must be a fraction")
        self.waived_checks = frozenset(self.waived_checks)

    def evaluate(self, batch: Sequence[Document]) -> GateDecision:
        """Run the checks and decide.

        A finding from a waived check is kept in the report — dropping it
        would hide the problem — but demoted to ``warn`` so it cannot block.
        """
        raw = run_checks(batch, self.checks)
        findings: list[QualityFinding] = []
        for finding in raw:
            if finding.check in self.waived_checks and finding.severity.rank >= Severity.ERROR.rank:
                findings.append(
                    QualityFinding(
                        check=finding.check,
                        severity=Severity.WARN,
                        message=f"{finding.message} (waived)",
                        subject=finding.subject,
                        observed=finding.observed,
                        expected=finding.expected,
                    )
                )
            else:
                findings.append(finding)

        counts = {level.value: 0 for level in Severity}
        for finding in findings:
            counts[finding.severity.value] += 1

        criticals = [f for f in findings if f.severity is Severity.CRITICAL]
        errors = [f for f in findings if f.severity is Severity.ERROR]

        over_count = len(errors) > self.max_errors
        rate = (len(errors) / len(batch)) if batch else 0.0
        over_rate = bool(batch) and self.max_error_rate > 0.0 and rate > self.max_error_rate
        blocked = bool(criticals) or (over_count and not self._within_rate_allowance(rate))

        blocked_by: list[str] = []
        if criticals:
            blocked_by.extend(sorted({f.check for f in criticals}))
        if blocked and errors:
            blocked_by.extend(sorted({f.check for f in errors}))
        if over_rate:
            blocked = True
            blocked_by.append("error_rate")

        return GateDecision(
            passed=not blocked,
            findings=tuple(findings),
            blocked_by=tuple(dict.fromkeys(blocked_by)),
            counts=counts,
        )

    def _within_rate_allowance(self, rate: float) -> bool:
        """A configured rate allowance overrides the absolute error count."""
        return self.max_error_rate > 0.0 and rate <= self.max_error_rate

    def enforce(self, batch: Sequence[Document]) -> GateDecision:
        """Evaluate and raise when the batch is blocked."""
        decision = self.evaluate(batch)
        if not decision.passed:
            raise QualityGateFailed(
                "quality gate blocked the batch",
                failures=list(decision.blocked_by),
                documents=len(batch),
            )
        return decision
