# Test layout and style

## Layout

The tree mirrors `app/`, with one exception: HTTP-level tests live in `tests/api/`.

| Where | What goes there |
|---|---|
| `tests/test_<module>.py` | Tests for a top-level module: `app/config.py` -> `tests/test_config.py` |
| `tests/<package>/test_<module>.py` | Tests for `app/<package>/<module>.py`: `app/ai/grading.py` -> `tests/ai/test_grading.py` |
| `tests/api/` | Behaviour of an endpoint, driven through the real app: `/healthz`, auth, `POST /sessions`, `GET /verdict`, the `/s/{token}` walkthrough. One file per endpoint or concern, named for that, not for a module |
| `tests/conftest.py` | Fixtures shared by more than one file. A fixture used by one file stays in that file |

When you add a module under `app/`, add its test file at the mirrored path. When you add an endpoint, add or extend a file in `tests/api/`.

## Style

- Every test module opens with a docstring saying what it covers and why it is shaped the way it is. If a test is deliberately written an unobvious way (concurrency, a state that must not be collapsed), say so.
- Start each file with `from __future__ import annotations`, and annotate test functions `-> None`.
- Test names say the behaviour, not the function: `test_v10_sorts_after_v9_not_after_v1`, not `test_version_key`.
- One behaviour per test. Prefer several small tests over one with many asserts.
- Pure functions get pure tests: no `grumpy_env`, no database. Only pull in `grumpy_env` (a real Postgres via Testcontainers) when the test needs the app or the database.
- Use a real Postgres, not mocks, for anything that touches SQL. The fake grader (`FAKE_GRADER=true`, passes answers containing `looks-good`) stands in for the model; tests never call the real API.
- Never log or assert on a real secret; generate tokens with `secrets.token_urlsafe`.
