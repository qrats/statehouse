"""statehouse — legislative and regulatory document ingestion.

The package is deliberately split so that the pure-Python layers (``core``,
``transform``, ``quality``, ``orchestration``) import cleanly without Scrapy,
Selenium, Airflow, boto3 or a database driver installed. Only ``scraping``,
``browser``, ``load`` and the DAG files reach for those, and they are imported
lazily by the CLI.
"""

from __future__ import annotations

from statehouse.version import VERSION

__all__ = ["VERSION"]
