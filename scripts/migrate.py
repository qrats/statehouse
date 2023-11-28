#!/usr/bin/env python
"""Apply the SQL migrations in order.

Deliberately simple: numbered files, applied once, recorded in a table. An
ORM migration framework buys nothing here because the schema is small and the
DDL is hand-written for the indexes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()[:32]


def discover(directory: Path) -> list[Migration]:
    """Load migrations in version order, rejecting anything misnamed."""
    found: list[Migration] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise SystemExit(f"error: {path.name} does not match NNN_name.sql")
        version, name = match.group(1), match.group(2)
        if version in seen:
            raise SystemExit(f"error: duplicate migration version {version}")
        seen.add(version)
        found.append(
            Migration(version=version, name=name, path=path, sql=path.read_text(encoding="utf-8"))
        )
    return found


def apply(migrations: list[Migration], dsn: str, *, check_only: bool) -> int:
    if check_only:
        for migration in migrations:
            print(f"{migration.version} {migration.name} {migration.checksum}")
        print(f"{len(migrations)} migration(s) validated")
        return 0

    import psycopg  # imported here so --check-only needs no driver

    applied = 0
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(LEDGER_DDL)
            cursor.execute("SELECT version, checksum FROM schema_migrations")
            ledger = dict(cursor.fetchall())

            for migration in migrations:
                recorded = ledger.get(migration.version)
                if recorded == migration.checksum:
                    continue
                if recorded is not None:
                    raise SystemExit(
                        f"error: {migration.version} was applied with a different checksum; "
                        "edit forward with a new migration instead"
                    )
                cursor.execute(migration.sql)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
                applied += 1
                print(f"applied {migration.version}_{migration.name}")
        connection.commit()

    print(f"{applied} migration(s) applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="migrations")
    parser.add_argument("--dsn", default=os.environ.get("STATEHOUSE_WAREHOUSE_DSN", ""))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        print(f"error: {directory} is not a directory", file=sys.stderr)
        return 1
    if not args.check_only and not args.dsn:
        print("error: a DSN is required unless --check-only", file=sys.stderr)
        return 1

    return apply(discover(directory), args.dsn, check_only=args.check_only)


if __name__ == "__main__":
    raise SystemExit(main())
