"""Integration test: /healthz must check a real database, not return a literal."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_returns_ok_against_real_postgres(grumpy_env) -> None:
    # grumpy_env sets DATABASE_URL plus the other now-required settings
    # (GRUMPY_BASE_URL, GRUMPY_TOKEN) via monkeypatch, so config succeeds.
    # /healthz itself needs none of them — it only proves the DB check.

    # TestClient used as a context manager drives the real ASGI lifespan
    # (startup: run migrations, open the pool; shutdown: close the pool).
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
