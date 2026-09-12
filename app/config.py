"""Configuration for grumpy, via pydantic-settings.

Settings are constructed lazily (see get_settings()) rather than at import
time, so that:
  - config validation genuinely happens at *startup* (inside the FastAPI
    lifespan handler), matching the phase-1 requirement to fail fast there
    with a clear, named error;
  - tests can set environment variables per-test before the app object is
    actually started, without import-order tricks.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Rejected outright — the exact value shipped in .env.example as a
# copy-pasteable placeholder. A self-hoster who deploys with this literal
# string publishes their own auth bypass, since the value is public in
# this repo's history.
_PLACEHOLDER_GRUMPY_TOKENS = frozenset({"changeme-generate-a-real-token"})

# Below this, GRUMPY_TOKEN is rejected as too weak to be a real secret.
# secrets.token_urlsafe(16) — what tests/conftest.py's grumpy_env fixture
# uses, and what the README and .env.example recommend at minimum —
# produces 22 characters; this sits comfortably below that while still
# ruling out short, guessable, hand-typed strings.
MIN_GRUMPY_TOKEN_LENGTH = 20

# Mirrors app.schemas._REPO_RE. Duplicated rather than imported so
# app.config has no intra-app dependencies — it's constructed first, at
# startup, before anything else in the app exists to import from.
_REPO_SHAPE_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


def _parse_allowed_repos(raw: str | None) -> frozenset[str] | None:
    """None means unrestricted (any repo permitted). A blank/whitespace/
    comma-only string is treated the same as unset — required so that
    docker-compose's `${GRUMPY_ALLOWED_REPOS:-}` interpolation (which
    yields an empty string, not an absent variable, when unset in .env)
    doesn't accidentally lock out every repo. Only a non-empty, non-blank
    value restricts.
    """
    if raw is None:
        return None
    repos = frozenset(r.strip() for r in raw.split(",") if r.strip())
    return repos or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Required — no default. Missing this at startup must fail fast and name
    # the variable (DATABASE_URL), not fail somewhere deep in a DB call.
    database_url: str = Field(
        ...,
        description="Postgres DSN, e.g. postgresql://user:pass@host:5432/db",
    )

    # Required — used to build session URLs. Never derive this from a
    # request's Host header: a self-hoster behind a proxy would get a URL
    # that only resolves internally.
    grumpy_base_url: str = Field(
        ...,
        description="Public base URL used to build session URLs, e.g. https://grumpy.example.com",
    )

    # Required — the bearer token the Action must present to /sessions and
    # /verdict. Compared with secrets.compare_digest, never ==.
    grumpy_token: str = Field(
        ...,
        description="Shared bearer token required on /sessions and /verdict",
    )

    # Optional — defense-in-depth against a leaked/guessed GRUMPY_TOKEN.
    # Unset or blank (the default) permits any repo, matching the
    # pre-allow-list behavior. See is_repo_allowed() below.
    grumpy_allowed_repos: str | None = Field(
        default=None,
        description=(
            "Optional comma-separated allow-list of repos ('owner/name') "
            "permitted to use this deployment's /sessions and /verdict, "
            "e.g. 'octo/repo-a,octo/repo-b'. Unset or blank permits any "
            "repo."
        ),
    )

    log_level: str = "INFO"

    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR

    # How long a session stays valid before phase 5's sweeper would consider
    # it expired. Nothing sweeps expired rows yet.
    session_ttl_days: int = 7

    # Above this, POST /sessions returns 413 rather than persisting the diff
    # (and, later, handing an unbounded field to a model call).
    #
    # 400 KB, not the 1 MB this used to be, because the ceiling that
    # actually binds is the model's context window, not Postgres. At
    # roughly 3–4 bytes per token a 1 MB diff is ~250k–330k tokens, past
    # claude-opus-5's context — and app/grading.py's tutorial call sends
    # the diff *numbered*, which adds several bytes per line on top. The
    # old default therefore accepted diffs that could never be graded: the
    # API rejected the call, RealGrader turned that into a GradingError,
    # and the developer got "please try submitting your answer again"
    # forever, with nothing telling them the diff was simply too big.
    # 400 KB is ~100k–130k tokens, leaving comfortable room for the
    # prompts, the response, and thinking. A refusal now happens once, at
    # session creation, with a 413 that says what was wrong.
    max_diff_bytes: int = 400_000

    # Hard cap on the raw request body, enforced by MaxBodySizeMiddleware
    # (app/middleware.py) against actual bytes received off the wire —
    # before Starlette/Pydantic ever buffers the body into a Python object.
    # Deliberately well above max_diff_bytes: a JSON-encoded diff can
    # expand past its raw byte count (worst case ~6x, via \uXXXX-escaped
    # control characters; realistic diffs mostly just double via \n/\t
    # escapes), and a legitimate max-size diff must never be spuriously
    # rejected by this outer cap. See _validate_body_size_relationship
    # below, which enforces that relationship if either value is changed.
    max_request_body_bytes: int = 8_000_000

    # Above this, POST /s/{token}/answer re-renders the answer form with an
    # inline error (same pattern as the empty-answer case) rather than
    # persisting the answer or spending a grader call on it. Generous for a
    # prose answer; exists to bound Postgres storage and Anthropic API
    # spend per submission, not to constrain legitimate answers.
    max_answer_bytes: int = 20_000

    # Total priced actions (graded answer submissions + tutorial-breakdown
    # requests, combined) allowed per session before it locks in as
    # terminal 'failed'. 0 means unlimited — the session just stays
    # 'pending' after every wrong answer, forever, until it's passed; see
    # app/verdict.py, which keeps reporting PENDING for exactly as long as
    # that's true. A tutorial request costs exactly as much Anthropic
    # spend as an answer submission (2 calls each), so both are charged
    # against the same budget rather than two separate knobs — a cap that
    # only bounded answers would leave tutorial requests as an unbounded
    # cost hole. Defaults finite, like every other numeric cap in this
    # file, rather than unlimited.
    max_session_attempts: int = 3

    # Whether the answer page offers "Get a tutorial breakdown" at all.
    # When on it sits next to Submit from the first view, so a developer
    # can ask for the walkthrough without first having to answer wrong.
    # Off by default — this is additional AI-call surface (2 more calls
    # per tutorial, on top of MAX_SESSION_ATTEMPTS's own spend) that a
    # self-hoster should opt into deliberately rather than get for free.
    # Only gates *new* tutorial requests (POST /s/{token}/tutorial); a
    # tutorial already in progress when this flips off is still allowed
    # to be explained back.
    enable_tutorial: bool = False

    # Whether a failed verdict's `reasoning` uses the scathing, sarcastic
    # "grumpy" roast persona (RealGrader's comparison prompt, app/grading.py)
    # or a direct, professional tone instead. Off by default — a fresh
    # deployment (e.g. an enterprise self-hoster) gets the professional tone
    # with zero config; opt into the roast explicitly with MEANIEMODE=true.
    meaniemode: bool = False

    # Required, no default — which grader to use is exactly the kind of
    # consequential choice this project doesn't silently default (same
    # reasoning as DATABASE_URL/GRUMPY_TOKEN above). True selects
    # FakeGrader (phase 3's only implementation); false has no
    # implementation yet (see main.py's lifespan) but still needs a real
    # key configured, checked below, since that's the one part of "false"
    # this phase can actually validate ahead of phase 4 existing.
    fake_grader: bool = Field(...)
    model_api_key: str | None = None

    @field_validator("grumpy_token")
    @classmethod
    def _reject_weak_token(cls, value: str) -> str:
        if value in _PLACEHOLDER_GRUMPY_TOKENS:
            raise ValueError(
                "GRUMPY_TOKEN is still the .env.example placeholder value — "
                'generate a real one, e.g.: python -c "import secrets; '
                'print(secrets.token_urlsafe(32))"'
            )
        if len(value) < MIN_GRUMPY_TOKEN_LENGTH:
            raise ValueError(
                f"GRUMPY_TOKEN is too short ({len(value)} chars, need at "
                f"least {MIN_GRUMPY_TOKEN_LENGTH}) — generate a real one, "
                'e.g.: python -c "import secrets; '
                'print(secrets.token_urlsafe(32))"'
            )
        return value

    @field_validator("max_session_attempts")
    @classmethod
    def _reject_negative_session_attempts(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                "MAX_SESSION_ATTEMPTS must be 0 (unlimited) or a positive "
                f"integer, got {value}"
            )
        return value

    @model_validator(mode="after")
    def _require_model_key_unless_fake(self) -> Settings:
        if not self.fake_grader and not self.model_api_key:
            raise ValueError("MODEL_API_KEY is required when FAKE_GRADER=false")
        return self

    @model_validator(mode="after")
    def _validate_allowed_repos(self) -> Settings:
        repos = _parse_allowed_repos(self.grumpy_allowed_repos)
        if repos is not None:
            bad = sorted(r for r in repos if not _REPO_SHAPE_RE.match(r))
            if bad:
                raise ValueError(
                    "GRUMPY_ALLOWED_REPOS contains invalid entries (must be "
                    f"'owner/name'): {', '.join(bad)}"
                )
        return self

    @model_validator(mode="after")
    def _validate_body_size_relationship(self) -> Settings:
        if self.max_request_body_bytes < self.max_diff_bytes:
            raise ValueError(
                "MAX_REQUEST_BODY_BYTES "
                f"({self.max_request_body_bytes}) is smaller than "
                f"MAX_DIFF_BYTES ({self.max_diff_bytes}) — a max-size diff "
                "could never fit through the outer body-size cap"
            )
        if self.max_request_body_bytes < self.max_answer_bytes:
            raise ValueError(
                "MAX_REQUEST_BODY_BYTES "
                f"({self.max_request_body_bytes}) is smaller than "
                f"MAX_ANSWER_BYTES ({self.max_answer_bytes}) — a max-size "
                "answer could never fit through the outer body-size cap"
            )
        return self

    def is_repo_allowed(self, repo: str) -> bool:
        """True if `repo` may use this deployment. Unset/blank
        GRUMPY_ALLOWED_REPOS permits any repo — see _parse_allowed_repos.
        """
        allowed = _parse_allowed_repos(self.grumpy_allowed_repos)
        return allowed is None or repo in allowed


def get_settings() -> Settings:
    """Build Settings from the environment, failing fast with a clear error.

    Raises SystemExit naming the missing variable(s) rather than letting a
    raw pydantic ValidationError (or, worse, a later AttributeError/None
    deref) surface from deep inside startup.
    """
    try:
        return Settings()
    except ValidationError as exc:
        missing = [
            str(err["loc"][0]).upper()
            for err in exc.errors()
            if err["type"] == "missing" and err["loc"]
        ]
        if missing:
            raise SystemExit(
                "grumpy: missing required environment variable(s): "
                + ", ".join(missing)
            ) from exc
        raise SystemExit(f"grumpy: invalid configuration: {exc}") from exc
