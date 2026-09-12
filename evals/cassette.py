"""Records/replays Anthropic Messages API responses to disk, keyed on a
hash of (diff, question, answer), so `make eval` is fast and free on
repeat runs. Delete the case's cassette file under evals/cassettes/ to
force a re-record against the real API.

Each RealGrader.grade() call makes exactly two HTTP requests
(interpretation, then comparison); a cassette stores both, in order, and
replays them in order on a later run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

CASSETTES_DIR = Path(__file__).resolve().parent / "cassettes"


def cassette_key(diff: str, question: str, answer: str) -> str:
    digest = hashlib.sha256()
    for part in (diff, question, answer):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


class CassetteTransport(httpx.AsyncBaseTransport):
    def __init__(self, case_key: str) -> None:
        self._path = CASSETTES_DIR / f"{case_key}.json"
        self._real = httpx.AsyncHTTPTransport()
        self._recorded: list[dict] = (
            json.loads(self._path.read_text()) if self._path.exists() else []
        )
        self._replay_index = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._replay_index < len(self._recorded):
            entry = self._recorded[self._replay_index]
            self._replay_index += 1
            return httpx.Response(entry["status_code"], json=entry["body"], request=request)

        response = await self._real.handle_async_request(request)
        body = await response.aread()
        payload = json.loads(body)
        self._recorded.append({"status_code": response.status_code, "body": payload})
        CASSETTES_DIR.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._recorded, indent=2))
        return httpx.Response(response.status_code, json=payload, request=request)

    async def aclose(self) -> None:
        await self._real.aclose()
