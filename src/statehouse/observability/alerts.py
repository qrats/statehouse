"""Alerting rules.

Proactive rather than reactive: the failure mode that actually hurts is a
jurisdiction going quiet, and nothing errors when that happens. These rules
are evaluated by the monitoring DAG and turn state into a list of alerts.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from statehouse.config.jurisdictions import Jurisdiction
from statehouse.core.clock import Clock, utc_now
from statehouse.core.enums import Severity
from statehouse.core.models import Watermark
from statehouse.orchestration.runs import RunRegistry
from statehouse.orchestration.watermark import lag_seconds

__all__ = ["Alert", "AlertRules", "evaluate_alerts"]


@dataclass(frozen=True)
class Alert:
    """One thing somebody should look at."""

    rule: str
    severity: Severity
    subject: str
    message: str
    value: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "severity": self.severity.value,
            "subject": self.subject,
            "message": self.message,
            "value": self.value,
        }


@dataclass
class AlertRules:
    """Thresholds. Deliberately few — an alert nobody acts on is noise."""

    stale_warn_seconds: float = 6 * 3600.0
    stale_critical_seconds: float = 24 * 3600.0
    consecutive_failure_warn: int = 2
    consecutive_failure_critical: int = 4
    never_ingested_is_critical: bool = True

    def __post_init__(self) -> None:
        if self.stale_warn_seconds <= 0 or self.stale_critical_seconds <= 0:
            raise ValueError("staleness thresholds must be positive")
        if self.stale_critical_seconds < self.stale_warn_seconds:
            raise ValueError("critical staleness must be at least the warn threshold")
        if self.consecutive_failure_warn < 1:
            raise ValueError("consecutive_failure_warn must be at least 1")
        if self.consecutive_failure_critical < self.consecutive_failure_warn:
            raise ValueError("critical failure count must be at least the warn count")


def _staleness_alert(
    jurisdiction: Jurisdiction, watermark: Watermark | None, now: datetime, rules: AlertRules
) -> Alert | None:
    lag = lag_seconds(watermark, now)
    if lag is None:
        if not rules.never_ingested_is_critical:
            return None
        return Alert(
            rule="never_ingested",
            severity=Severity.CRITICAL,
            subject=jurisdiction.code,
            message="jurisdiction has no watermark at all",
        )
    if lag >= rules.stale_critical_seconds:
        return Alert(
            rule="stale_watermark",
            severity=Severity.CRITICAL,
            subject=jurisdiction.code,
            message="watermark is critically stale",
            value=lag,
        )
    if lag >= rules.stale_warn_seconds:
        return Alert(
            rule="stale_watermark",
            severity=Severity.WARN,
            subject=jurisdiction.code,
            message="watermark is behind the freshness target",
            value=lag,
        )
    return None


def _failure_alert(jurisdiction: Jurisdiction, failures: int, rules: AlertRules) -> Alert | None:
    if failures >= rules.consecutive_failure_critical:
        return Alert(
            rule="consecutive_failures",
            severity=Severity.CRITICAL,
            subject=jurisdiction.code,
            message="ingest has failed repeatedly",
            value=float(failures),
        )
    if failures >= rules.consecutive_failure_warn:
        return Alert(
            rule="consecutive_failures",
            severity=Severity.WARN,
            subject=jurisdiction.code,
            message="ingest is failing",
            value=float(failures),
        )
    return None


def evaluate_alerts(
    jurisdictions: Iterable[Jurisdiction],
    watermarks: Sequence[Watermark],
    runs: RunRegistry,
    *,
    rules: AlertRules | None = None,
    stream: str = "default",
    clock: Clock | None = None,
) -> list[Alert]:
    """Evaluate every rule against current state.

    Disabled jurisdictions are skipped — they are off on purpose and alerting
    on them trains people to ignore the channel. Results are ordered
    worst-first, then by jurisdiction, so the digest reads top-down.
    """
    active = rules or AlertRules()
    now = utc_now(clock)
    index = {w.key: w for w in watermarks}
    alerts: list[Alert] = []

    for jurisdiction in jurisdictions:
        if not jurisdiction.enabled:
            continue
        staleness = _staleness_alert(
            jurisdiction, index.get(f"{jurisdiction.code}:{stream}"), now, active
        )
        if staleness is not None:
            alerts.append(staleness)
        failure = _failure_alert(
            jurisdiction, runs.consecutive_failures(jurisdiction.code), active
        )
        if failure is not None:
            alerts.append(failure)

    return sorted(alerts, key=lambda a: (-a.severity.rank, a.subject, a.rule))
