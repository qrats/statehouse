"""Exception hierarchy.

Everything the platform raises on purpose descends from :class:`StatehouseError`
so a DAG task can catch one type and decide whether the failure is worth a
retry. The distinction that matters operationally is
:class:`TransientError` (retry, the source will probably be fine in a minute)
versus :class:`PermanentError` (do not retry, a human has to look).
"""

from __future__ import annotations

__all__ = [
    "StatehouseError",
    "TransientError",
    "PermanentError",
    "ConfigError",
    "JurisdictionNotConfigured",
    "FetchError",
    "RateLimited",
    "PortalUnavailable",
    "ParseError",
    "SchemaViolation",
    "ValidationError",
    "StorageError",
    "WatermarkConflict",
    "CheckpointCorrupt",
    "QualityGateFailed",
]


class StatehouseError(Exception):
    """Base class for every error the platform raises deliberately."""

    #: Short machine-readable label, used in metrics and run records.
    code = "statehouse_error"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = dict(context)

    def __str__(self) -> str:  # pragma: no cover - trivial
        if not self.context:
            return self.message
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({rendered})"

    def as_dict(self) -> dict[str, object]:
        """Serialisable form, used when a failure is recorded on a run."""
        return {"code": self.code, "message": self.message, "context": dict(self.context)}


class TransientError(StatehouseError):
    """Failure that is expected to clear on its own. Safe to retry."""

    code = "transient"
    retryable = True


class PermanentError(StatehouseError):
    """Failure that will recur identically until someone changes something."""

    code = "permanent"
    retryable = False


class ConfigError(PermanentError):
    """Configuration is missing, malformed or internally inconsistent."""

    code = "config"


class JurisdictionNotConfigured(ConfigError):
    """A jurisdiction code was referenced that the registry does not know."""

    code = "jurisdiction_not_configured"


class FetchError(TransientError):
    """A network fetch failed in a way that is worth retrying."""

    code = "fetch"


class RateLimited(TransientError):
    """The source asked us to slow down, explicitly or via a 429/503."""

    code = "rate_limited"

    def __init__(self, message: str, retry_after_seconds: float | None = None, **context: object):
        super().__init__(message, **context)
        self.retry_after_seconds = retry_after_seconds


class PortalUnavailable(TransientError):
    """A portal is up but serving a maintenance page or an empty shell."""

    code = "portal_unavailable"


class ParseError(PermanentError):
    """Fetched bytes did not contain what the extractor was promised."""

    code = "parse"


class SchemaViolation(PermanentError):
    """A record does not satisfy the canonical schema."""

    code = "schema_violation"


class ValidationError(PermanentError):
    """A value failed a domain rule (bad date range, unknown chamber, ...)."""

    code = "validation"


class StorageError(TransientError):
    """A write to Postgres, S3 or the search index failed."""

    code = "storage"


class WatermarkConflict(PermanentError):
    """Two runs tried to advance the same watermark past each other."""

    code = "watermark_conflict"


class CheckpointCorrupt(PermanentError):
    """A persisted checkpoint could not be decoded into a usable position."""

    code = "checkpoint_corrupt"


class QualityGateFailed(PermanentError):
    """A data-quality gate rejected a batch."""

    code = "quality_gate_failed"

    def __init__(self, message: str, failures: list[str] | None = None, **context: object):
        super().__init__(message, **context)
        self.failures = list(failures or [])
