"""Logging helpers: human-readable by default, ``LOG_JSON=true`` for CI."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

__all__ = ["configure_logging", "get_logger", "JsonFormatter", "log_event", "timed"]

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """One JSON object per line — trivially ingestible by log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("run_id", "source_id", "doc_id", "url", "duration_s", "event", "count"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", json_output: bool = False, stream: Any = None) -> logging.Logger:
    """Configure the root ``puppetnet`` logger idempotently."""
    global _CONFIGURED
    root = logging.getLogger("puppetnet")
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric)

    handler_stream = stream or sys.stdout
    if not _CONFIGURED or not root.handlers:
        root.handlers.clear()
        handler = logging.StreamHandler(handler_stream)
        handler.setLevel(numeric)
        if json_output:
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
        root.addHandler(handler)
        root.propagate = False
        _CONFIGURED = True
    else:
        for handler in root.handlers:
            handler.setLevel(numeric)

    # Tame noisy third-party loggers inside GitHub Actions output.
    for noisy in ("urllib3", "requests", "neo4j", "asyncio", "filelock", "spacy"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))
    return root


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("puppetnet"):
        name = f"puppetnet.{name}"
    return logging.getLogger(name)


def log_event(logger: logging.Logger, message: str, level: int = logging.INFO, **fields: Any) -> None:
    """Structured log line: keyword fields become LogRecord attributes."""
    logger.log(level, message, extra={k: v for k, v in fields.items() if k not in {"message", "args"}})


@contextmanager
def timed(logger: logging.Logger, label: str, level: int = logging.INFO) -> Iterator[dict[str, float]]:
    """Time a block and log ``label completed in Xs`` on exit."""
    started = time.perf_counter()
    result: dict[str, float] = {"seconds": 0.0}
    try:
        yield result
    finally:
        elapsed = time.perf_counter() - started
        result["seconds"] = round(elapsed, 3)
        logger.log(level, "%s completed in %.2fs", label, elapsed)


def banner(text: str, width: int = 78) -> str:
    line = "=" * width
    return f"\n{line}\n{text.center(width)}\n{line}"


def human_int(value: float) -> str:
    return f"{int(value):,}"


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
