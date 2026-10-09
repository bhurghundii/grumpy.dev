"""make seed: inserts a session with a realistic diff straight into Postgres and
prints its /s/{token} URL, for developing the UI without an Action. Each run
uses a fresh head_sha, so it always yields a new pending session.
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

from app.config import get_settings
from app.db.pool import create_pool
from app.db.sessions import create_or_get_session

FIXTURE_DIFF_PATH = Path(__file__).resolve().parent / "fixtures" / "sample.diff"

REPO = "octo/seed-demo"
PR_NUMBER = 1
QUESTION = (
    "This change adds a second charge_already_processed() check inside the "
    "lock, right before recording the charge. What could go wrong if that "
    "check were removed and only the one before acquire_charge_lock() "
    "stayed?"
)


async def main() -> None:
    settings = get_settings()
    diff = FIXTURE_DIFF_PATH.read_text()
    head_sha = secrets.token_hex(20)  # 40 hex chars, fresh every run

    pool = await create_pool(settings.database_url.get_secret_value(), min_size=1, max_size=1)
    try:
        row, _created = await create_or_get_session(
            pool,
            repo=REPO,
            pr_number=PR_NUMBER,
            head_sha=head_sha,
            base_sha=secrets.token_hex(20),
            diff=diff,
            question=QUESTION,
            token=secrets.token_urlsafe(32),
            ttl_days=settings.session_ttl_days,
        )
    finally:
        await pool.close()

    url = f"{settings.grumpy_base_url.rstrip('/')}/s/{row['token']}"
    print(f"Seeded session for {REPO}#{PR_NUMBER}")
    print(url)


if __name__ == "__main__":
    asyncio.run(main())
