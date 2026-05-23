"""Structured JSON logger.

We deliberately don't use ``logging.config.dictConfig`` — the workers
are short-lived single-process programs and the surface area of the
stdlib module is more than we need. A small ``StreamHandler`` with a
custom ``Formatter`` is enough.

The emitted record is a single JSON object per line, terminated by
``\\n``. Fields that frequently identify the work item (``customer``,
``stage``, ``shard``, ``part_number``) can be supplied via :func:`bind`,
which returns a logger that adds those keys to every record.

Usage::

    from obs import get_logger

    log = get_logger(__name__).bind(stage="scraper", shard="chunk_3.csv")
    log.info("downloaded", part_number="AB123", url=...)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Mapping

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process",
}


class _JsonFormatter(logging.Formatter):
    """Serialise the LogRecord plus any ``extra=`` fields to a JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Allow callers to attach arbitrary key/value pairs via ``extra=``.
        # We skip LogRecord-internal attributes (in ``_RESERVED``) and the
        # ``_internal`` ones Python uses (single leading underscore + a
        # known prefix), but we DO want to surface user-supplied fields
        # that we had to rename to ``_<reserved>`` to dodge a collision.
        for key, value in record.__dict__.items():
            if key in _RESERVED:
                continue
            if key.startswith("__"):
                continue
            if key in payload:
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _BoundLogger:
    """A logging.Logger wrapper that injects a fixed set of fields."""

    def __init__(self, logger: logging.Logger, fields: Mapping[str, Any] | None = None):
        self._logger = logger
        self._fields: dict[str, Any] = dict(fields or {})

    def bind(self, **fields: Any) -> "_BoundLogger":
        merged = {**self._fields, **fields}
        return _BoundLogger(self._logger, merged)

    def _log(self, level: int, msg: str, **fields: Any) -> None:
        extra = {**self._fields, **fields}
        # Logging refuses to overwrite reserved attributes — make sure we don't.
        for key in list(extra):
            if key in _RESERVED:
                extra[f"_{key}"] = extra.pop(key)
        self._logger.log(level, msg, extra=extra)

    def debug(self, msg: str, **fields: Any) -> None:
        self._log(logging.DEBUG, msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._log(logging.INFO, msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._log(logging.WARNING, msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._log(logging.ERROR, msg, **fields)

    def exception(self, msg: str, **fields: Any) -> None:
        extra = {**self._fields, **fields}
        for key in list(extra):
            if key in _RESERVED:
                extra[f"_{key}"] = extra.pop(key)
        self._logger.exception(msg, extra=extra)


_configured = False


def _ensure_root_configured() -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    # Remove any handlers a third-party module may have installed at import time
    # (pandas, boto3, etc.) so we don't double-emit.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # Boto/urllib3 are chatty at INFO; pin them up unless explicitly requested.
    logging.getLogger("botocore").setLevel(os.getenv("BOTO_LOG_LEVEL", "WARNING"))
    logging.getLogger("urllib3").setLevel(os.getenv("BOTO_LOG_LEVEL", "WARNING"))
    _configured = True


def get_logger(name: str) -> _BoundLogger:
    """Return a bound logger for ``name``. Configures the root logger on first call."""
    _ensure_root_configured()
    base = {}
    customer = os.getenv("CUSTOMER")
    if customer:
        base["customer"] = customer
    return _BoundLogger(logging.getLogger(name), base)


def bind(**fields: Any) -> _BoundLogger:
    """Shortcut for ``get_logger(__name__).bind(**fields)`` for top-level scripts."""
    return get_logger("app").bind(**fields)
