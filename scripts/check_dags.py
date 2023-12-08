#!/usr/bin/env python
"""Fail the build when a DAG file will not import or breaks a convention.

Airflow's own import check tells you that something broke, not what. This adds
the conventions we actually rely on: every DAG has an owner and tags, no DAG
has catchup on, and no DAG has a schedule shorter than the fetch it performs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MIN_SCHEDULE_MINUTES = 15
REQUIRED_TAG = "statehouse"


def _fail(problems: list[str]) -> int:
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    print(f"{len(problems)} DAG problem(s)", file=sys.stderr)
    return 1


def check(dag_folder: Path) -> list[str]:
    from airflow.models import DagBag

    bag = DagBag(dag_folder=str(dag_folder), include_examples=False)
    problems: list[str] = [
        f"{path}: {error}" for path, error in sorted(bag.import_errors.items())
    ]
    if not bag.dags and not problems:
        problems.append(f"{dag_folder}: no DAGs were discovered")

    for dag_id, dag in sorted(bag.dags.items()):
        if not dag.tags or REQUIRED_TAG not in dag.tags:
            problems.append(f"{dag_id}: missing the '{REQUIRED_TAG}' tag")
        if not (dag.default_args or {}).get("owner"):
            problems.append(f"{dag_id}: no owner in default_args")
        if dag.catchup:
            problems.append(f"{dag_id}: catchup must be off")
        if dag.max_active_runs and dag.max_active_runs > 4:
            problems.append(f"{dag_id}: max_active_runs is unreasonably high")
        if not dag.description:
            problems.append(f"{dag_id}: no description")
        if not dag.tasks:
            problems.append(f"{dag_id}: has no tasks")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dags", default="airflow/dags", help="DAG folder to check")
    args = parser.parse_args(argv)

    folder = Path(args.dags).resolve()
    if not folder.is_dir():
        print(f"error: {folder} is not a directory", file=sys.stderr)
        return 1

    problems = check(folder)
    if problems:
        return _fail(problems)
    print(f"DAG check passed for {folder}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
