"""Structured logging.

Two things matter here beyond "write some JSON":

* **Nothing formats on the event loop.** Handlers sit behind a
  ``QueueHandler``/``QueueListener`` pair, so a log call on the hot path is an
  enqueue and the JSON serialisation plus file write happen on a background
  thread. A slow disk cannot stall a buy.
* **Every record is one JSON object per line**, so the audit trail greps and
  pipes into anything without a parser.
"""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import queue
import sys
import time
from typing import Any, Optional

from .config import LoggingConfig

_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with everything passed via `extra` merged in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human-readable console output; extras appended as key=value pairs."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{time.strftime('%H:%M:%S', time.localtime(record.created))}"
            f".{int(record.msecs):03d} {record.levelname:<5} {record.getMessage()}"
        )
        extras = " ".join(
            f"{k}={v}"
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        )
        line = f"{base} {extras}".rstrip()
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


_listener: Optional[logging.handlers.QueueListener] = None


def setup_logging(cfg: LoggingConfig) -> None:
    """Install the queue-backed console (and optional file) handlers."""
    global _listener

    handlers: list[logging.Handler] = []

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JsonFormatter() if cfg.json else ConsoleFormatter())
    handlers.append(console)

    if cfg.file:
        cfg.file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.file, maxBytes=64 * 1024 * 1024, backupCount=5
        )
        # The file is the audit trail, so it is always JSON regardless of what
        # the console is set to.
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)

    log_queue: queue.Queue = queue.Queue(-1)
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(logging.handlers.QueueHandler(log_queue))
    root.setLevel(getattr(logging, cfg.level, logging.INFO))

    if _listener is not None:
        _listener.stop()
    _listener = logging.handlers.QueueListener(
        log_queue, *handlers, respect_handler_level=True
    )
    _listener.start()
    atexit.register(shutdown_logging)


def shutdown_logging() -> None:
    """Flush and stop the background log writer."""
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
