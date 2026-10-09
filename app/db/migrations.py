"""Home-rolled migration runner: applies migrations/*.sql in version order,
tracked in schema_migrations. A Postgres advisory lock serialises replicas
that boot at once, so a migration never applies twice.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import psycopg

logger = logging.getLogger("grumpy.migrations")

_VERSION_RE = re.compile(r"^V(\d+)__")


def _version_key(path: Path) -> tuple[int, str]:
    """Order by numeric version; a string sort would put V10 before V1. Unversioned names sort first."""
    match = _VERSION_RE.match(path.name)
    return (int(match.group(1)) if match else -1, path.name)

# Must stay stable across releases; it makes concurrent runs mutually exclusive.
_MIGRATION_LOCK_KEY = 8_741_902_233

_CREATE_TRACKER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def run_migrations(dsn: str, migrations_dir: Path | str) -> list[str]:
    """Apply migrations not yet recorded; returns the filenames applied by this call."""
    migrations_dir = Path(migrations_dir)
    applied_now: list[str] = []

    # A dedicated connection, since the advisory lock is session-scoped.
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        try:
            await conn.execute(_CREATE_TRACKER_SQL)

            cur = await conn.execute("SELECT version FROM schema_migrations")
            already_applied = {row[0] for row in await cur.fetchall()}

            for path in sorted(migrations_dir.glob("*.sql"), key=_version_key):
                if path.name in already_applied:
                    continue
                sql = path.read_text()
                logger.info(
                    "applying migration", extra={"outcome": "applying", "path": path.name}
                )
                # No bound parameters, so a multi-statement file runs as one simple-query batch.
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (path.name,),
                )
                applied_now.append(path.name)
                logger.info(
                    "migration applied", extra={"outcome": "applied", "path": path.name}
                )
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
    finally:
        await conn.close()

    return applied_now
