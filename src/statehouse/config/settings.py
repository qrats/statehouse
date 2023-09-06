"""Runtime settings.

Read from the environment with sane defaults so that a developer can run the
pure-Python layers with nothing set, while the deployed DAGs get everything
from the Airflow environment. Nothing here reaches out to a network or a
secrets manager at import time.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields

from statehouse.core.errors import ConfigError

__all__ = ["Settings", "load_settings", "ENV_PREFIX"]

ENV_PREFIX = "STATEHOUSE_"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_bool(raw: str, key: str) -> bool:
    text = raw.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{key} must be a boolean", value=raw)


def _as_int(raw: str, key: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer", value=raw) from exc


def _as_float(raw: str, key: str) -> float:
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number", value=raw) from exc


@dataclass
class Settings:
    """Everything the pipeline needs to know about where it is running."""

    environment: str = "local"
    log_level: str = "INFO"
    log_format: str = "json"

    # Storage
    raw_bucket: str = "statehouse-raw-local"
    raw_prefix: str = "raw"
    warehouse_dsn: str = "postgresql://statehouse:statehouse@localhost:5432/statehouse"
    search_endpoint: str = "http://localhost:9200"
    search_index_prefix: str = "statehouse"

    # Fetching
    user_agent: str = "statehouse-bot/0.1 (+ops@statehouse.internal)"
    request_timeout_seconds: float = 30.0
    max_retries: int = 4
    retry_backoff_seconds: float = 2.0
    respect_robots: bool = True
    default_requests_per_minute: int = 30
    default_concurrency: int = 4

    # Browser
    browser_pool_size: int = 2
    browser_page_load_timeout: float = 45.0
    browser_headless: bool = True
    remote_webdriver_url: str = ""

    # Orchestration
    watermark_max_age_seconds: float = 86_400.0
    backfill_chunk_days: int = 30
    max_documents_per_run: int = 25_000

    # Quality
    quality_fail_fast: bool = False
    max_error_findings: int = 0

    extras: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.request_timeout_seconds <= 0:
            raise ConfigError("request_timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ConfigError("max_retries cannot be negative")
        if self.default_requests_per_minute <= 0:
            raise ConfigError("default_requests_per_minute must be positive")
        if self.default_concurrency <= 0:
            raise ConfigError("default_concurrency must be positive")
        if self.backfill_chunk_days <= 0:
            raise ConfigError("backfill_chunk_days must be positive")
        self.log_level = self.log_level.upper()
        self.environment = self.environment.strip().lower()

    @property
    def is_production(self) -> bool:
        return self.environment in ("prod", "production")

    def index_for(self, stream: str) -> str:
        return f"{self.search_index_prefix}-{stream}".lower()


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from ``env`` (defaults to ``os.environ``).

    Keys are the field names upper-cased and prefixed, e.g.
    ``STATEHOUSE_RAW_BUCKET``. Unknown ``STATEHOUSE_``-prefixed keys are kept
    in ``extras`` rather than rejected: the DAGs pass a few pass-through values
    that only a specific adapter cares about.
    """
    source = dict(os.environ if env is None else env)
    kwargs: dict[str, object] = {}
    known = {f.name for f in fields(Settings) if f.name != "extras"}
    extras: dict[str, str] = {}

    for key, raw in source.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX) :].lower()
        if name not in known:
            extras[name] = raw
            continue
        declared = next(f for f in fields(Settings) if f.name == name)
        if declared.type in ("bool", bool):
            kwargs[name] = _as_bool(raw, key)
        elif declared.type in ("int", int):
            kwargs[name] = _as_int(raw, key)
        elif declared.type in ("float", float):
            kwargs[name] = _as_float(raw, key)
        else:
            kwargs[name] = raw

    return Settings(extras=extras, **kwargs)  # type: ignore[arg-type]
