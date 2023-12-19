"""Freshness monitoring.

The DAG that catches the failure nobody else does: a jurisdiction that stops
producing data without anything erroring. Runs hourly, evaluates the alert
rules, and emits a digest.
"""

from __future__ import annotations

from typing import Any

from statehouse.config.jurisdictions import default_registry
from statehouse.core.enums import Severity
from statehouse.observability.alerts import AlertRules, evaluate_alerts

try:  # pragma: no cover
    from airflow import DAG
    from airflow.operators.python import PythonOperator
except ImportError:  # pragma: no cover
    DAG = None  # type: ignore[assignment,misc]
    PythonOperator = None  # type: ignore[assignment,misc]

from common import DEFAULT_ARGS, START_DATE, bootstrap_task, dag_tags

__all__ = ["collect_state", "evaluate_rules", "publish_digest"]

SCHEDULE = "0 * * * *"


def collect_state(**_context: Any) -> dict[str, Any]:
    """Read the current watermarks and run history."""
    log = bootstrap_task("monitor.collect")
    from statehouse.runner import watermark_store

    watermarks = watermark_store().list_all()
    log.info("collected state", extra={"watermarks": len(watermarks)})
    return {
        "watermarks": [
            {
                "jurisdiction": w.jurisdiction,
                "stream": w.stream,
                "observed_through": w.observed_through.isoformat() if w.observed_through else None,
                "revision": w.revision,
            }
            for w in watermarks
        ]
    }


def evaluate_rules(**context: Any) -> dict[str, Any]:
    """Turn state into alerts."""
    log = bootstrap_task("monitor.evaluate")
    from statehouse.core.models import Watermark
    from statehouse.orchestration.runs import RunRegistry
    from statehouse.utils.dates import parse_datetime

    upstream = context["ti"].xcom_pull(task_ids="collect")
    watermarks = [
        Watermark(
            jurisdiction=entry["jurisdiction"],
            stream=entry["stream"],
            observed_through=parse_datetime(entry["observed_through"]),
            revision=entry["revision"],
        )
        for entry in upstream["watermarks"]
    ]
    alerts = evaluate_alerts(
        default_registry().enabled(), watermarks, RunRegistry(), rules=AlertRules()
    )
    log.info("rules evaluated", extra={"alerts": len(alerts)})
    return {"alerts": [alert.to_dict() for alert in alerts]}


def publish_digest(**context: Any) -> dict[str, Any]:
    """Emit the digest and fail the task when anything is critical.

    Failing on critical is what puts the DAG red in the UI; warnings are
    logged and left green, because a permanently-yellow dashboard gets
    ignored.
    """
    log = bootstrap_task("monitor.publish")
    alerts = context["ti"].xcom_pull(task_ids="evaluate")["alerts"]
    criticals = [a for a in alerts if a["severity"] == Severity.CRITICAL.value]
    for alert in alerts:
        log.warning("alert", extra=alert)
    if criticals:
        raise RuntimeError(
            f"{len(criticals)} critical freshness alerts: "
            + ", ".join(sorted({a["subject"] for a in criticals}))
        )
    return {"alerts": len(alerts), "critical": 0}


def build_dag() -> Any:
    if DAG is None:  # pragma: no cover
        return None
    dag = DAG(
        dag_id="statehouse_monitor_freshness",
        description="Hourly freshness and failure alerting",
        default_args={**DEFAULT_ARGS, "retries": 0},
        start_date=START_DATE,
        schedule=SCHEDULE,
        catchup=False,
        max_active_runs=1,
        tags=dag_tags("monitoring"),
    )
    with dag:
        collect = PythonOperator(task_id="collect", python_callable=collect_state)
        evaluate = PythonOperator(task_id="evaluate", python_callable=evaluate_rules)
        publish = PythonOperator(task_id="publish", python_callable=publish_digest)
        collect >> evaluate >> publish
    return dag


_dag = build_dag()  # pragma: no cover
if _dag is not None:  # pragma: no cover
    globals()[_dag.dag_id] = _dag
