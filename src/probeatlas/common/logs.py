"""Structured JSON logging for every ProbeAtlas process."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime

# Never let bearer tokens or our secret formats reach the logs.
_SECRET_RE = re.compile(r"(pa[kseha]_[A-Za-z0-9_\-]{8,})|((?i:bearer)\s+[A-Za-z0-9._\-]+)")


def redact(text: str) -> str:
    """Replace API keys, session/agent tokens, and bearer credentials in ``text`` with ``[REDACTED]``."""
    return _SECRET_RE.sub("[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line.

    Every record carries ``ts`` (UTC ISO 8601), ``level``, ``service``, ``logger``, and ``msg``. Structured
    fields passed through :func:`log_fields` are merged in. Messages and tracebacks are passed through
    :func:`redact`.
    """

    def __init__(self, service: str):
        """Create a formatter that tags every record with ``service`` (for example ``api`` or ``agent``)."""
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        """Serialize ``record`` to a single-line JSON string with secrets redacted."""
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update({k: redact(v) if isinstance(v, str) else v for k, v in extra.items() if k not in payload})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def setup_logging(service: str) -> None:
    """Configure the root logger for a ProbeAtlas process.

    Reads ``PROBEATLAS_LOG_LEVEL`` (default ``INFO``) and ``PROBEATLAS_LOG_FORMAT`` (``json`` or ``text``).
    HTTP client libraries are limited to WARNING because their request logs can contain full URLs.
    """
    level = os.environ.get("PROBEATLAS_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("PROBEATLAS_LOG_FORMAT", "json") == "json":
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Request logs from libraries may include URLs with query strings.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_fields(**fields) -> dict:
    """Attach structured fields to a log call: ``logger.info("msg", extra=log_fields(job_id=...))``."""
    return {"fields": fields}
