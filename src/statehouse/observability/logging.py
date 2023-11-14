"""Structured logging.

Airflow's task logs are the only place most of these messages are ever read,
and they are read by grep. JSON lines with a stable key set beat prose.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

__all__ = ["JsonFormatter", "configure_logging", "get_logger", "bind", "ScrapyLogFormatter"]

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Anything passed through ``extra=`` lands as a top-level key, which is what
    makes the logs queryable. Keys colliding with the reserved set are
    prefixed rather than dropped.
    """

    def __init__(self, service: str = "statehouse") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "service": self.service,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key if key not in payload else f"ctx_{key}"] = _safe(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    return str(value)


def configure_logging(level: str = "INFO", *, fmt: str = "json", service: str = "statehouse") -> None:
    """Install a single handler on the root logger.

    Idempotent: calling it twice replaces the handler rather than adding a
    second, which otherwise doubles every Airflow log line.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter(service=service))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """A logger carrying fixed context on every record it emits."""
    return bind(logging.getLogger(name), **context)


def bind(logger: logging.Logger | logging.LoggerAdapter, **context: Any) -> logging.LoggerAdapter:
    """Attach context to a logger, merging with anything already bound."""
    base = dict(getattr(logger, "extra", {}) or {})
    base.update(context)
    target = logger.logger if isinstance(logger, logging.LoggerAdapter) else logger
    return logging.LoggerAdapter(target, base)


class ScrapyLogFormatter:  # pragma: no cover - only used inside Scrapy
    """Quietens Scrapy's per-item logging, which is unusable at our volume."""

    def crawled(self, request: Any, response: Any, spider: Any) -> dict[str, Any] | None:
        return None

    def scraped(self, item: Any, response: Any, spider: Any) -> dict[str, Any] | None:
        return None

    def dropped(self, item: Any, exception: Any, response: Any, spider: Any) -> dict[str, Any]:
        return {
            "level": logging.WARNING,
            "msg": "item dropped: %(exception)s",
            "args": {"exception": exception},
        }
