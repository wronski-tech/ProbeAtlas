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
    return _SECRET_RE.sub("[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update({k: v for k, v in extra.items() if k not in payload})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def setup_logging(service: str) -> None:
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
    """``logger.info("msg", extra=log_fields(job_id=...))``"""
    return {"fields": fields}
