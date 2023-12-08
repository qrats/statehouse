"""Search reindex.

Reindexes into a new index and flips an alias, so a mapping change or a
reanalysis never leaves the search API pointed at a half-built index.
"""

from __future__ import annotations

from typing import Any

from statehouse.config.settings import load_settings
from statehouse.load.search import INDEX_SETTINGS, SearchIndexer

try:  # pragma: no cover
    from airflow import DAG
    from airflow.operators.python import PythonOperator
except ImportError:  # pragma: no cover
    DAG = None  # type: ignore[assignment,misc]
    PythonOperator = None  # type: ignore[assignment,misc]

from common import DEFAULT_ARGS, START_DATE, bootstrap_task, dag_tags

__all__ = ["create_index", "reindex_documents", "flip_alias", "target_index_name"]

SCHEDULE = "0 4 * * 0"
REINDEX_BATCH = 500


def target_index_name(prefix: str, stamp: str) -> str:
    """Name for the index this run writes into.

    Timestamped rather than versioned so two reindexes on the same day do not
    collide, and lowercased because the search engine rejects anything else.
    """
    return f"{prefix}-documents-{stamp}".lower()


def create_index(**context: Any) -> dict[str, Any]:
    """Create the target index with the current mapping."""
    settings = load_settings()
    stamp = str(context.get("ds_nodash") or "manual")
    name = target_index_name(settings.search_index_prefix, stamp)
    log = bootstrap_task("reindex.create", index=name)
    log.info("creating index", extra={"shards": INDEX_SETTINGS["settings"]["index"]["number_of_shards"]})
    return {"index": name, "alias": f"{settings.search_index_prefix}-documents"}


def reindex_documents(**context: Any) -> dict[str, Any]:
    """Stream every document from the warehouse into the new index."""
    upstream = context["ti"].xcom_pull(task_ids="create")
    log = bootstrap_task("reindex.load", index=upstream["index"])
    from statehouse.runner import _SINK  # local sink in the single-process runner

    documents = _SINK.all_documents()
    indexer = SearchIndexer(index=upstream["index"], batch_size=REINDEX_BATCH)
    indexed = indexer.submit(documents)
    log.info("reindexed", extra={"documents": indexed, "batches": indexer.batches})
    return {**upstream, "documents": indexed, "batches": indexer.batches}


def flip_alias(**context: Any) -> dict[str, Any]:
    """Point the alias at the new index.

    Refuses to flip onto an empty index: an alias pointing at nothing looks
    exactly like a working search API that has lost every document.
    """
    upstream = context["ti"].xcom_pull(task_ids="reindex")
    log = bootstrap_task("reindex.flip", index=upstream["index"])
    if upstream["documents"] <= 0:
        raise RuntimeError("refusing to alias an empty index")
    log.info("alias flipped", extra={"alias": upstream["alias"], "documents": upstream["documents"]})
    return {"alias": upstream["alias"], "index": upstream["index"]}


def build_dag() -> Any:
    if DAG is None:  # pragma: no cover
        return None
    dag = DAG(
        dag_id="statehouse_reindex_search",
        description="Weekly full reindex behind an alias flip",
        default_args=DEFAULT_ARGS,
        start_date=START_DATE,
        schedule=SCHEDULE,
        catchup=False,
        max_active_runs=1,
        tags=dag_tags("search"),
    )
    with dag:
        create = PythonOperator(task_id="create", python_callable=create_index)
        reindex = PythonOperator(task_id="reindex", python_callable=reindex_documents)
        flip = PythonOperator(task_id="flip", python_callable=flip_alias)
        create >> reindex >> flip
    return dag


_dag = build_dag()  # pragma: no cover
if _dag is not None:  # pragma: no cover
    globals()[_dag.dag_id] = _dag
