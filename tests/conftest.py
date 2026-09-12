"""Shared test fixtures: a real Postgres 16 via Testcontainers.

Two machine-specific Docker Desktop fixes live here, both gated the same
way — enabled on macOS or via an explicit env override, never
unconditionally, since disabling either unconditionally would leak
containers in CI:

1. TESTCONTAINERS_RYUK_DISABLED — Docker Desktop on this machine exposes the
   daemon at a non-standard per-user socket path, which breaks the Ryuk
   reaper container's own connection back to the daemon. See (2) below for
   why the same fact matters even before Ryuk gets involved.

2. DOCKER_HOST — the same non-standard socket path can also break
   Testcontainers' *initial* connection to the daemon (via docker-py), not
   just Ryuk. This isn't (as far as we can tell) reliably auto-detected via
   the active `docker context` by every docker-py version, so we set it
   explicitly here when it isn't already present in the environment.
"""

from __future__ import annotations

import os
import platform
import secrets
from dataclasses import dataclass
from pathlib import Path

import pytest
from testcontainers.community.postgres import PostgresContainer

_ON_MACOS_DOCKER_DESKTOP = (
    platform.system() == "Darwin" or os.getenv("GRUMPY_TESTCONTAINERS_MACOS_FIXUP")
)

if _ON_MACOS_DOCKER_DESKTOP:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    os.environ.setdefault(
        "DOCKER_HOST", f"unix://{Path.home()}/.docker/run/docker.sock"
    )


@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("postgres:16") as container:
        yield container


@pytest.fixture()
def database_url(postgres_container: PostgresContainer) -> str:
    # driver=None gives a plain postgresql://user:pass@host:port/db DSN.
    # The default (driver="psycopg2") would produce postgresql+psycopg2://,
    # which psycopg3 won't parse.
    return postgres_container.get_connection_url(driver=None)


@dataclass
class GrumpyEnv:
    base_url: str
    token: str


@pytest.fixture()
def grumpy_env(database_url: str, monkeypatch: pytest.MonkeyPatch) -> GrumpyEnv:
    """Sets the env vars app.config.Settings requires, so tests that import
    app.main and drive its lifespan (directly or via TestClient) get a
    working config without touching the real environment."""
    env = GrumpyEnv(
        base_url="https://grumpy.example.com",
        token=secrets.token_urlsafe(16),
    )
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("GRUMPY_BASE_URL", env.base_url)
    monkeypatch.setenv("GRUMPY_TOKEN", env.token)
    monkeypatch.setenv("FAKE_GRADER", "true")
    return env
