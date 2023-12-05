"""Historical backfill.

Manually triggered with a jurisdiction and an optional starting year. Runs a
bounded number of slices per DAG run and re-triggers itself while work
remains, which keeps any single run inside the execution timeout and leaves a
readable history of how far the backfill has got.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from statehouse.config.jurisdictions import default_registry
from statehouse.orchestration.backfill import BackfillPlanner

try:  # pragma: no cover
    from airflow import DAG
    from airflow.operators.python import PythonOperator
except ImportError:  # pragma: no cover
    DAG = None  # type: ignore[assignment,misc]
    PythonOperator = None  # type: ignore[assignment,misc]

from common import DEFAULT_ARGS, START_DATE, bootstrap_task, dag_tags

__all__ = ["plan_slices", "run_slices", "report_progress"]

#: How many slices one DAG run attempts. Sized so a run finishes inside the
#: two-hour execution timeout even on the slowest browser-backed portal.
SLICES_PER_RUN = 12


def _conf(context: dict[str, Any]) -> dict[str, Any]:
    dag_run = context.get("dag_run")
    return dict(getattr(dag_run, "conf", None) or {})


def plan_slices(**context: Any) -> dict[str, Any]:
    """Build the plan and hand the next batch of slices downstream."""
    conf = _conf(context)
    code = conf.get("jurisdiction")
    log = bootstrap_task("backfill.plan", jurisdiction=code)
    if not code:
        raise ValueError("backfill requires a jurisdiction in the run configuration")

    jurisdiction = default_registry()[code]
    planner = BackfillPlanner(chunk_days=int(conf.get("chunk_days", 30)))
    plan = planner.plan(
        jurisdiction,
        through=date.fromisoformat(conf.get("through") or date.today().isoformat()),
        from_year=conf.get("from_year"),
        completed_labels=conf.get("completed") or [],
    )
    batch = plan.take(int(conf.get("slices_per_run", SLICES_PER_RUN)))
    log.info(
        "backfill planned",
        extra={"total_slices": len(plan.slices), "this_run": len(batch)},
    )
    return {
        "jurisdiction": code,
        "remaining": len(plan.slices) - len(batch),
        "slices": [
            {
                "session": item.session,
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "label": item.label,
            }
            for item in batch
        ],
    }


def run_slices(**context: Any) -> dict[str, Any]:
    """Ingest each planned slice, tolerating individual failures.

    A slice that fails is recorded and skipped rather than failing the run:
    the next trigger re-plans and picks it up, and one bad month should not
    stop a decade.
    """
    upstream = context["ti"].xcom_pull(task_ids="plan")
    code = upstream["jurisdiction"]
    log = bootstrap_task("backfill.run", jurisdiction=code)
    jurisdiction = default_registry()[code]

    from statehouse.runner import load_batch, run_spider, transform_batch

    completed: list[str] = []
    failed: list[str] = []
    written = 0

    for item in upstream["slices"]:
        try:
            fetched = run_spider(jurisdiction, session=item["session"])
            transformed = transform_batch(code, fetched.batch_key)
            if not transformed.passed:
                failed.append(item["label"])
                continue
            loaded = load_batch(code, transformed.batch_key)
            written += loaded.written
            completed.append(item["label"])
        except Exception as exc:  # noqa: BLE001 - recorded per slice
            failed.append(item["label"])
            log.warning("slice failed", extra={"label": item["label"], "error": str(exc)})

    log.info(
        "backfill batch done",
        extra={"completed": len(completed), "failed": len(failed), "written": written},
    )
    return {
        "completed": completed,
        "failed": failed,
        "written": written,
        "remaining": upstream["remaining"],
    }


def report_progress(**context: Any) -> dict[str, Any]:
    """Summarise the run and say whether another trigger is warranted."""
    result = context["ti"].xcom_pull(task_ids="run")
    log = bootstrap_task("backfill.report")
    more = result["remaining"] > 0 or bool(result["failed"])
    log.info("backfill progress", extra={"remaining": result["remaining"], "more": more})
    return {"more_work": more, **result}


def build_dag() -> Any:
    if DAG is None:  # pragma: no cover
        return None
    dag = DAG(
        dag_id="statehouse_backfill",
        description="Historical backfill, triggered with a jurisdiction",
        default_args=DEFAULT_ARGS,
        start_date=START_DATE,
        schedule=None,
        catchup=False,
        max_active_runs=2,
        tags=dag_tags("backfill"),
    )
    with dag:
        plan = PythonOperator(task_id="plan", python_callable=plan_slices)
        run = PythonOperator(task_id="run", python_callable=run_slices)
        report = PythonOperator(task_id="report", python_callable=report_progress)
        plan >> run >> report
    return dag


_dag = build_dag()  # pragma: no cover
if _dag is not None:  # pragma: no cover
    globals()[_dag.dag_id] = _dag
