"""Structured JSON logging over the stdlib."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

# The token in `/s/{token}` is the credential, so it is never logged. The
# trailing segment is kept: it is useful and not secret.
_SESSION_PATH_RE = re.compile(r"^/s/[^/]+")


def redact_session_token(path: str) -> str:
    """Replace the token in a `/s/{token}...` path with a placeholder. Applied to
    every logged path, so a 404 on a malformed URL does not leak it either."""
    return _SESSION_PATH_RE.sub("/s/<redacted>", path)

# Fields a log record may carry via `extra=` that are surfaced in the JSON payload.
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
            "timestamp": datetime.now(UTC).isoformat(),
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

    # uvicorn's access log would re-leak the raw session token; log_requests
    # already records the redacted path.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False
    access_logger.disabled = True

    logging.getLogger("uvicorn.error").handlers = [handler]

    # httpx logs full URLs at INFO, which under TestClient includes /s/{token}.
    logging.getLogger("httpx").setLevel(logging.WARNING)
