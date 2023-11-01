"""Driving Scrapy from inside an Airflow task.

Scrapy wants to own the process. Airflow already owns the process. The
resolution is a ``CrawlerProcess`` in a subprocess, with results collected
through a feed rather than shared memory, because a Twisted reactor cannot be
restarted inside the same interpreter and a worker runs many tasks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from typing import Any

from statehouse.config.jurisdictions import Jurisdiction
from statehouse.config.settings import Settings, load_settings
from statehouse.core.errors import FetchError

__all__ = ["crawl", "build_command", "SPIDER_BY_ADAPTER"]

#: Registry adapter name -> Scrapy spider name.
SPIDER_BY_ADAPTER: dict[str, str] = {
    "federal": "us_bills",
    "california": "ca_bills",
    "texas": "tx_bills",
    "newyork": "ny_bills",
    "illinois": "il_bills",
    "ohio": "oh_bills",
}


def build_command(
    jurisdiction: Jurisdiction,
    *,
    session: str,
    output_path: str,
    since: str | None = None,
) -> list[str]:
    """The ``scrapy crawl`` command line for one jurisdiction.

    Raises :class:`~statehouse.core.errors.FetchError` when the registry names
    an adapter with no spider behind it, which is what happens when someone
    adds a state to the registry and forgets the other half.
    """
    spider = SPIDER_BY_ADAPTER.get(jurisdiction.adapter)
    if not spider:
        raise FetchError(
            "no spider registered for adapter",
            adapter=jurisdiction.adapter,
            jurisdiction=jurisdiction.code,
        )
    command = [
        sys.executable,
        "-m",
        "scrapy",
        "crawl",
        spider,
        "-a",
        f"session={session}",
        "-O",
        output_path,
        "--nolog",
    ]
    if since:
        command.extend(["-a", f"since={since}"])
    return command


def crawl(
    jurisdiction: Jurisdiction,
    *,
    session: str,
    settings: Settings | None = None,
    timeout_seconds: float = 5400.0,
) -> Sequence[dict[str, Any]]:
    """Run a spider to completion and return the records it produced.

    A non-zero exit or an unreadable feed is a :class:`FetchError`; an empty
    feed is not, because out of session a portal legitimately has nothing.
    """
    active = settings or load_settings()
    handle, output_path = tempfile.mkstemp(suffix=".jsonl", prefix=f"{jurisdiction.code}-")
    os.close(handle)

    env = dict(os.environ)
    env["SCRAPY_SETTINGS_MODULE"] = "statehouse.scraping.settings"
    env["STATEHOUSE_LOG_LEVEL"] = active.log_level

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            build_command(jurisdiction, session=session, output_path=output_path),
            env=env,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise FetchError(
                "spider exited non-zero",
                jurisdiction=jurisdiction.code,
                returncode=completed.returncode,
                stderr=completed.stderr.decode("utf-8", "replace")[-2000:],
            )
        return _read_feed(output_path)
    except subprocess.TimeoutExpired as exc:
        raise FetchError(
            "spider exceeded its time budget",
            jurisdiction=jurisdiction.code,
            timeout_seconds=timeout_seconds,
        ) from exc
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def _read_feed(path: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
    except OSError as exc:
        raise FetchError("spider feed could not be read", path=path) from exc
    return records
