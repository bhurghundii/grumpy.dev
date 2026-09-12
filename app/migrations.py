"""Home-rolled migration runner.

Applies migrations/*.sql on startup, tracked in a schema_migrations table.
No Alembic, no ORM — raw SQL files applied in filename order.

Wrapped in a fixed-constant Postgres advisory lock so that concurrent
replicas booting at the same time (self-hosters running >1 container, or
just two `docker compose up` runs racing) serialize instead of double
applying: the loser blocks on pg_advisory_lock, wakes once the winner has
committed and released it, and finds every migration already recorded in
schema_migrations.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

logger = logging.getLogger("grumpy.migrations")

# Arbitrary fixed constant — must stay stable across releases, since it's
# what makes concurrent migration runs against the same DB mutually exclusive.
_MIGRATION_LOCK_KEY = 8_741_902_233

_CREATE_TRACKER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def run_migrations(dsn: str, migrations_dir: Path | str) -> list[str]:
    """Apply any migrations/*.sql not yet recorded in schema_migrations.

    Returns the list of migration filenames applied by *this* call (empty
    if another process already applied everything, or if there was nothing
    pending).
    """
    migrations_dir = Path(migrations_dir)
    applied_now: list[str] = []

    # A dedicated connection, separate from the app's pool — the advisory
    # lock is session-scoped, and this keeps its lifetime trivial to reason
    # about (acquire, do the work, release, close) instead of leaking into a
    # pooled connection that gets handed back and reused mid-lock.
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        try:
            await conn.execute(_CREATE_TRACKER_SQL)

            cur = await conn.execute("SELECT version FROM schema_migrations")
            already_applied = {row[0] for row in await cur.fetchall()}

            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name in already_applied:
                    continue
                sql = path.read_text()
                logger.info(
                    "applying migration", extra={"outcome": "applying", "path": path.name}
                )
                # No bound parameters here, so psycopg3 is free to send a
                # migration file containing multiple ';'-separated
                # statements as a single simple-query batch.
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
