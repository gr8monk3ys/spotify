"""Structured logging configuration for SpotifyForge.

Supports two output formats controlled by ``SPOTIFYFORGE_LOG_FORMAT``:

* ``"text"`` (default in development) — human-readable coloured output
* ``"json"`` (default in production) — structured JSON, one object per line

Also provides a correlation-ID middleware for FastAPI that threads a unique
request ID through all log records emitted during that request.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from typing import Any

# ---------------------------------------------------------------------------
# Correlation ID (request-scoped via contextvars)
# ---------------------------------------------------------------------------

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Return the current request's correlation ID, or empty string."""
    return correlation_id_var.get()


def new_correlation_id() -> str:
    """Generate and set a new correlation ID for the current context."""
    cid = uuid.uuid4().hex[:12]
    correlation_id_var.set(cid)
    return cid


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        cid = get_correlation_id()
        if cid:
            entry["correlation_id"] = cid
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        # Merge any extra fields attached to the record
        for key in ("user_id", "request_method", "request_path", "status_code", "duration_ms"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        return json.dumps(entry, default=str)


# ---------------------------------------------------------------------------
# Setup helper
# ---------------------------------------------------------------------------


def configure_logging(
    level: str = "INFO",
    log_format: str = "text",
) -> None:
    """Configure root logger with the chosen format.

    Parameters
    ----------
    level:
        Log level string (e.g. ``"INFO"``, ``"DEBUG"``).
    log_format:
        ``"json"`` for structured JSON output, ``"text"`` for human-readable.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any existing handlers to avoid duplicates on re-init
    root.handlers.clear()

    handler = logging.StreamHandler()

    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root.addHandler(handler)
