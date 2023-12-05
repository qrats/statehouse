"""Incremental ingest.

One DAG, generated per jurisdiction, so a stuck state cannot hold up the other
forty-nine and so the Airflow UI shows per-jurisdiction history. The alternative
— one DAG with a dynamic task per jurisdiction — was tried and made a single
slow portal look like a platform outage.
"""

from __future__ import annotations

from typing import Any

from statehouse.config.jurisdictions import Jurisdiction, default_registry
from statehouse.config.settings import load_settings
from statehouse.core.enums import RunState
from statehouse.core.errors import StatehouseError
from statehouse.orchestration.runs import RunRegistry
from statehouse.orchestration.watermark import advance

try:  # pragma: no cover - Airflow is only present in the scheduler image
    from airflow import DAG
    from airflow.operators.python import PythonOperator
except ImportError:  # pragma: no cover
    DAG = None  # type: ignore[assignment,misc]
    PythonOperator = None  # type: ignore[assignment,misc]

from common import DEFAULT_ARGS, START_DATE, bootstrap_task, dag_tags, task_run_id

__all__ = ["build_dag", "fetch_task", "transform_task", "load_task", "finalise_task"]

#: Every jurisdiction is polled on the same cadence; the scheduler inside the
#: fetch task is what decides whether there is anything worth doing.
SCHEDULE = "*/30 * * * *"

_RUNS = RunRegistry()


def fetch_task(jurisdiction_code: str, **context: Any) -> dict[str, Any]:
    """Run the spider and archive what it fetched."""
    log = bootstrap_task("fetch", jurisdiction=jurisdiction_code)
    registry = default_registry()
    jurisdiction: Jurisdiction = registry[jurisdiction_code]
    run_id = task_run_id(context, jurisdiction_code, "fetch")
    _RUNS.start(run_id, jurisdiction_code, dag=context.get("dag_run"))

    from statehouse.runner import run_spider

    try:
        outcome = run_spider(jurisdiction, settings=load_settings())
    except StatehouseError as exc:
        _RUNS.finish(run_id, RunState.FAILED, exc)
        log.error("fetch failed", extra={"error": exc.as_dict()})
        raise

    _RUNS.record(run_id, seen=outcome.documents_seen, fetch_failures=outcome.fetch_failures)
    log.info(
        "fetch complete",
        extra={"documents": outcome.documents_seen, "failures": outcome.fetch_failures},
    )
    return {
        "run_id": run_id,
        "documents_seen": outcome.documents_seen,
        "batch_key": outcome.batch_key,
        "observed_through": outcome.observed_through.isoformat()
        if outcome.observed_through
        else None,
    }


def transform_task(jurisdiction_code: str, **context: Any) -> dict[str, Any]:
    """Normalise, deduplicate and gate the batch the fetch task produced."""
    log = bootstrap_task("transform", jurisdiction=jurisdiction_code)
    upstream = context["ti"].xcom_pull(task_ids="fetch")
    from statehouse.runner import transform_batch

    result = transform_batch(jurisdiction_code, upstream["batch_key"])
    _RUNS.record(upstream["run_id"], findings=result.findings)
    log.info(
        "transform complete",
        extra={"documents": result.document_count, "blocked": not result.passed},
    )
    if not result.passed:
        raise StatehouseError("quality gate blocked the batch", blocked_by=result.blocked_by)
    return {"run_id": upstream["run_id"], "batch_key": result.batch_key}


def load_task(jurisdiction_code: str, **context: Any) -> dict[str, Any]:
    """Write to the warehouse and the search index."""
    log = bootstrap_task("load", jurisdiction=jurisdiction_code)
    upstream = context["ti"].xcom_pull(task_ids="transform")
    from statehouse.runner import load_batch

    result = load_batch(jurisdiction_code, upstream["batch_key"])
    _RUNS.record(upstream["run_id"], written=result.written, skipped=result.unchanged)
    log.info("load complete", extra={"written": result.written, "unchanged": result.unchanged})
    return {"run_id": upstream["run_id"], "written": result.written}


def finalise_task(jurisdiction_code: str, **context: Any) -> dict[str, Any]:
    """Advance the watermark and close the run.

    The watermark moves *here*, after the load, and never earlier: a watermark
    advanced before the write means a failed load silently skips documents
    that nobody will look for again.
    """
    log = bootstrap_task("finalise", jurisdiction=jurisdiction_code)
    fetched = context["ti"].xcom_pull(task_ids="fetch")
    loaded = context["ti"].xcom_pull(task_ids="load")
    from statehouse.runner import watermark_store

    store = watermark_store()
    current = store.get(jurisdiction_code, "default")
    from statehouse.utils.dates import parse_datetime

    updated = advance(
        current,
        jurisdiction=jurisdiction_code,
        observed_through=parse_datetime(fetched.get("observed_through")),
    )
    store.put(updated)
    run = _RUNS.finish(loaded["run_id"], RunState.SUCCEEDED)
    log.info(
        "run finished",
        extra={"state": run.state.value, "written": run.documents_written},
    )
    return {"state": run.state.value, "watermark_revision": updated.revision}


def build_dag(jurisdiction: Jurisdiction) -> Any:
    """Construct the DAG for one jurisdiction."""
    if DAG is None:  # pragma: no cover - import-time guard outside Airflow
        return None

    dag = DAG(
        dag_id=f"statehouse_ingest_{jurisdiction.code}",
        description=f"Incremental ingest for {jurisdiction.name}",
        default_args=DEFAULT_ARGS,
        start_date=START_DATE,
        schedule=SCHEDULE,
        catchup=False,
        max_active_runs=1,
        tags=dag_tags("ingest", jurisdiction.method.value, jurisdiction.code),
    )

    with dag:
        steps = []
        for name, fn in (
            ("fetch", fetch_task),
            ("transform", transform_task),
            ("load", load_task),
            ("finalise", finalise_task),
        ):
            steps.append(
                PythonOperator(
                    task_id=name,
                    python_callable=fn,
                    op_kwargs={"jurisdiction_code": jurisdiction.code},
                )
            )
        for upstream, downstream in zip(steps, steps[1:]):
            upstream >> downstream

    return dag


for _entry in default_registry().enabled():  # pragma: no cover - Airflow discovery
    _dag = build_dag(_entry)
    if _dag is not None:
        globals()[_dag.dag_id] = _dag
