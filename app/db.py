"""Async psycopg3 connection pool, opened/closed by the FastAPI lifespan."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool


async def create_pool(
    dsn: str, *, min_size: int = 1, max_size: int = 10
) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False)
    await pool.open(wait=True, timeout=30)
    return pool
