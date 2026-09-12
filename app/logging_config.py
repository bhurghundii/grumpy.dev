"""Structured JSON logging, hand-rolled over the stdlib.

No extra dependency (e.g. python-json-logger) — stdlib logging + json is
enough for phase 1, and keeps the dependency list exactly what §3 names.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Fields a log record may carry (via `extra=`) that we want surfaced in the
# JSON payload. Most of these are None in phase 1 since nothing populates
# them yet, but the shape is wired now per §7.
_EXTRA_FIELDS = (
    "repo",
    "pr_number",
    "head_sha",
    "outcome",
    "duration_ms",
    "method",
    "path",
    "status_code",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Quiet down uvicorn's own access log formatting in favor of ours; keep
    # its errors flowing through the same JSON handler.
    logging.getLogger("uvicorn.access").handlers = [handler]
    logging.getLogger("uvicorn.error").handlers = [handler]
