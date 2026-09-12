"""Structured JSON logging, hand-rolled over the stdlib.

No extra dependency (e.g. python-json-logger) — stdlib logging + json is
enough for phase 1, and keeps the dependency list exactly what §3 names.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

# `/s/{token}` — the token in that path segment IS the credential for the
# answer page (app/web.py), so a log line carrying the full path is a log
# line carrying a bearer-equivalent secret. The README already tells
# self-hosters to redact this in their reverse proxy's access log; grumpy
# has no business writing it to its own stdout while doing so.
#
# The trailing segment (/answer, /tutorial, /tutorial/explain) is kept:
# it's what makes the log useful, and it isn't secret.
_SESSION_PATH_RE = re.compile(r"^/s/[^/]+")


def redact_session_token(path: str) -> str:
    """Replaces the token in a `/s/{token}...` path with a placeholder.

    Any other path is returned unchanged. Applied to every logged path
    rather than only the ones a route matched, so a 404 on a malformed
    session URL doesn't log the token either.
    """
    return _SESSION_PATH_RE.sub("/s/<redacted>", path)

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

    # uvicorn's access log is disabled outright, not just reformatted. It
    # writes the raw request line ("GET /s/<token>/answer HTTP/1.1 303"),
    # which would re-leak the session token that redact_session_token()
    # exists to keep out of the logs — and app/main.py's own log_requests
    # middleware already records method, redacted path, status and duration
    # for every request, so nothing is lost by silencing it.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False
    access_logger.disabled = True

    logging.getLogger("uvicorn.error").handlers = [handler]

    # httpx logs every request it makes at INFO, as a line containing the
    # full URL. Left at the root level it inherits, that means RealGrader's
    # Anthropic calls narrate themselves into grumpy's structured log on
    # every graded answer. Nothing secret is in those URLs, but it's a
    # third-party logger emitting unaudited URLs into our log stream — and
    # under TestClient (which drives the app through httpx) it emits the
    # very /s/{token} URLs the redaction above exists to suppress.
    logging.getLogger("httpx").setLevel(logging.WARNING)
