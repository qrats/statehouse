"""Shared DAG helpers.

Airflow parses every file in this folder on a short interval, so nothing here
does work at import time: no registry queries against a database, no network,
no settings that raise. Anything expensive happens inside a task.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from statehouse.config.jurisdictions import default_registry
from statehouse.config.settings import load_settings
from statehouse.core.ids import run_id as derive_run_id
from statehouse.observability.logging import configure_logging, get_logger
from statehouse.version import VERSION

__all__ = [
    "DEFAULT_ARGS",
    "OWNER",
    "START_DATE",
    "dag_tags",
    "task_run_id",
    "bootstrap_task",
    "registry_for",
]

OWNER = "data-platform"
START_DATE = datetime(2023, 9, 1)

DEFAULT_ARGS: dict[str, Any] = {
    "owner": OWNER,
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "execution_timeout": timedelta(hours=2),
}


def dag_tags(*extra: str) -> list[str]:
    """Tags every DAG carries, plus whatever the DAG adds."""
    return sorted({"statehouse", f"v{VERSION}", *extra})


def task_run_id(context: dict[str, Any], jurisdiction: str, task: str) -> str:
    """Deterministic run id from the Airflow context.

    A retried task reuses the id, which is what makes the run registry count
    attempts instead of inventing runs.
    """
    dag_run = context.get("dag_run")
    identifier = getattr(dag_run, "run_id", None) or str(context.get("run_id") or "manual")
    return derive_run_id(jurisdiction, identifier, task)


def bootstrap_task(name: str, **context: Any) -> Any:
    """Configure logging and return a bound logger. Called first in each task."""
    settings = load_settings()
    configure_logging(settings.log_level, fmt=settings.log_format)
    return get_logger(f"statehouse.dag.{name}", version=VERSION, **context)


def registry_for(codes: list[str] | None = None) -> list[Any]:
    """Resolve jurisdiction codes to registry entries, defaulting to enabled."""
    registry = default_registry()
    if not codes:
        return registry.enabled()
    return registry.resolve_many(codes)
