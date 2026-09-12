"""Session tokens must never reach the logs.

The token in `/s/{token}` is the credential for the answer page, so a log
line carrying the raw request path carries a bearer-equivalent secret.
README's deploy checklist tells self-hosters to redact this in their
reverse proxy; these tests hold grumpy to the same standard for its own
stdout.
"""

from __future__ import annotations

import logging

import pytest

from app.logging_config import JsonFormatter, configure_logging, redact_session_token

_TOKEN = "KgIZiNGbq9qWkoSOdz7AMaMlKEfdFDDC0tQh7t7bm3U"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/s/{_TOKEN}", "/s/<redacted>"),
        (f"/s/{_TOKEN}/answer", "/s/<redacted>/answer"),
        (f"/s/{_TOKEN}/tutorial", "/s/<redacted>/tutorial"),
        (f"/s/{_TOKEN}/tutorial/explain", "/s/<redacted>/tutorial/explain"),
        # Not a session path — left alone.
        ("/healthz", "/healthz"),
        ("/sessions", "/sessions"),
        ("/verdict", "/verdict"),
        # Degenerate shapes: no token present, so nothing to redact.
        ("/s/", "/s/"),
        ("/s", "/s"),
    ],
)
def test_redacts_only_the_session_token_segment(path: str, expected: str) -> None:
    assert redact_session_token(path) == expected


def test_redaction_survives_tokens_containing_url_safe_punctuation() -> None:
    # secrets.token_urlsafe() emits '-' and '_'; neither may end the match early.
    assert redact_session_token("/s/ab-cd_ef-gh/answer") == "/s/<redacted>/answer"


def test_uvicorn_access_logger_is_disabled() -> None:
    """It logs the raw request line, which would re-leak the token that
    redact_session_token() exists to keep out of the logs."""
    configure_logging("INFO")
    access_logger = logging.getLogger("uvicorn.access")

    assert access_logger.disabled is True
    assert access_logger.handlers == []
    assert access_logger.propagate is False


def test_request_log_line_does_not_contain_the_token(grumpy_env) -> None:
    """End to end through the app: drive a request at a /s/{token} URL and
    assert the token appears nowhere in what grumpy actually emitted.

    Not pytest's caplog: configure_logging() replaces root.handlers, which
    removes caplog's own handler, so caplog captures nothing here and every
    assertion against it passes vacuously. This attaches a handler *after*
    startup has configured logging, and asserts on formatted output — the
    bytes a self-hoster would really see on stdout.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    emitted: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(self.format(record))

    with TestClient(app) as client:  # lifespan runs configure_logging()
        handler = Capture()
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            client.get(f"/s/{_TOKEN}")  # 404; the path is logged either way
        finally:
            root.removeHandler(handler)

    logged = "\n".join(emitted)
    assert logged, "captured no log output — the assertions below would be vacuous"
    # Every record, not just grumpy.request: the point is that nothing
    # grumpy configures emits the token, including third-party loggers that
    # inherit the root handler (httpx did, until it was quieted).
    assert _TOKEN not in logged, "session token leaked into the logs"
    assert "/s/<redacted>" in logged
