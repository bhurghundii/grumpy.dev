"""Configuration via pydantic-settings. Settings are built lazily by
get_settings() so validation runs at startup and tests can set env per test."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# The .env.example placeholder; public, so deploying with it is an auth bypass.
_PLACEHOLDER_GRUMPY_TOKENS = frozenset({"changeme-generate-a-real-token"})

# token_urlsafe(16), the recommended minimum, is 22 characters.
MIN_GRUMPY_TOKEN_LENGTH = 20

# Mirrors app.schemas._REPO_RE; duplicated so config imports nothing from app.
_REPO_SHAPE_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


def _parse_allowed_repos(raw: str | None) -> frozenset[str] | None:
    """None means any repo is allowed. Blank counts as unset, because
    docker-compose passes an empty string rather than omitting the variable."""
    if raw is None:
        return None
    repos = frozenset(r.strip() for r in raw.split(",") if r.strip())
    return repos or None


class SizeCheck(BaseModel):
    """Reject a PR changing more than max_changed_lines lines outside the
    `exclude` globs. 0 turns the check off."""

    model_config = ConfigDict(extra="forbid")

    max_changed_lines: int = Field(default=1000, ge=0)
    exclude: list[str] = Field(
        default_factory=lambda: ["*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum"]
    )


class EvaluatorConfig(BaseModel):
    """The parsed EVALUATOR JSON, one key per check. extra="forbid" so a typo'd
    key fails startup instead of silently loosening the gate."""

    model_config = ConfigDict(extra="forbid")

    size: SizeCheck = Field(default_factory=SizeCheck)


def _parse_evaluator(raw: str | None) -> EvaluatorConfig:
    """Blank or unset gives the defaults (same docker-compose reason as above)."""
    if raw is None or not raw.strip():
        return EvaluatorConfig()
    return EvaluatorConfig.model_validate_json(raw)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Otherwise a validation error can print MODEL_API_KEY into the log.
        hide_input_in_errors=True,
    )

    # Secrets are SecretStr so a repr or traceback shows '**********'.

    database_url: SecretStr = Field(
        ...,
        description="Postgres DSN, e.g. postgresql://user:pass@host:5432/db",
    )

    # Never derive this from the Host header; behind a proxy it would be internal.
    grumpy_base_url: str = Field(
        ...,
        description="Public base URL used to build session URLs, e.g. https://grumpy.example.com",
    )

    grumpy_token: SecretStr = Field(
        ...,
        description="Shared bearer token required on /sessions and /verdict",
    )

    grumpy_allowed_repos: str | None = Field(
        default=None,
        description=(
            "Optional comma-separated allow-list of repos ('owner/name') "
            "permitted to use this deployment's /sessions and /verdict, "
            "e.g. 'octo/repo-a,octo/repo-b'. Unset or blank permits any "
            "repo."
        ),
    )

    # Needs "Commit statuses: write". Unset means no status is posted.
    github_status_token: SecretStr | None = None

    # For GitHub Enterprise Server: https://<host>/api/v3.
    github_api_url: str = "https://api.github.com"

    log_level: str = "INFO"

    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR

    session_ttl_days: int = 7

    # POST /sessions rejects a larger diff with a 422. Sized to the model's
    # context window (~100k-130k tokens), not Postgres: a bigger diff could be
    # accepted but never graded.
    max_diff_bytes: int = 400_000

    # Hard cap on the raw body (MaxBodySizeMiddleware). Well above
    # max_diff_bytes because JSON escaping can expand a diff up to ~6x.
    max_request_body_bytes: int = 8_000_000

    # Per-answer cap, bounding storage and API spend.
    max_answer_bytes: int = 20_000

    # Includes the fixed high-level first question.
    exam_question_count: int = Field(default=5, ge=1)

    # Questions that must pass; validated to be 1..EXAM_QUESTION_COUNT.
    passingmarks: int = Field(default=3, ge=1)

    # Tries per question before the answer is revealed. 1 means no retries.
    max_question_attempts: int = Field(default=3, ge=1)

    # Sarcastic "grumpy" tone for failed marks; off gives a professional tone.
    meaniemode: bool = False

    # JSON, e.g. '{"size": {"max_changed_lines": 1000}}'. Kept as a raw string
    # because pydantic-settings would json.loads a blank value before we see it.
    evaluator: str | None = Field(
        default=None,
        description="JSON config for the checks that reject a PR before it is questioned.",
    )

    # Required, no default: choosing the grader is deliberately explicit.
    fake_grader: bool = Field(...)
    model_api_key: SecretStr | None = None

    @field_validator("grumpy_token")
    @classmethod
    def _reject_weak_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if token in _PLACEHOLDER_GRUMPY_TOKENS:
            raise ValueError(
                "GRUMPY_TOKEN is still the .env.example placeholder value — "
                'generate a real one, e.g.: python -c "import secrets; '
                'print(secrets.token_urlsafe(32))"'
            )
        if len(token) < MIN_GRUMPY_TOKEN_LENGTH:
            raise ValueError(
                f"GRUMPY_TOKEN is too short ({len(token)} chars, need at "
                f"least {MIN_GRUMPY_TOKEN_LENGTH}) — generate a real one, "
                'e.g.: python -c "import secrets; '
                'print(secrets.token_urlsafe(32))"'
            )
        return value

    @model_validator(mode="after")
    def _require_model_key_unless_fake(self) -> Settings:
        if not self.fake_grader and not (
            self.model_api_key and self.model_api_key.get_secret_value()
        ):
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
    def _validate_passingmarks(self) -> Settings:
        if self.passingmarks > self.exam_question_count:
            raise ValueError(
                f"PASSINGMARKS ({self.passingmarks}) is greater than "
                f"EXAM_QUESTION_COUNT ({self.exam_question_count}) — the sheet "
                "could never be passed"
            )
        return self

    @model_validator(mode="after")
    def _validate_evaluator(self) -> Settings:
        try:
            _parse_evaluator(self.evaluator)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
                for err in exc.errors()
            )
            raise ValueError(f"EVALUATOR is invalid: {problems}") from None
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
        """True if `repo` may use this deployment."""
        allowed = _parse_allowed_repos(self.grumpy_allowed_repos)
        return allowed is None or repo in allowed

    def evaluator_config(self) -> EvaluatorConfig:
        """The parsed EVALUATOR, already validated at startup."""
        return _parse_evaluator(self.evaluator)

    def passing_marks_for(self, question_count: int) -> int:
        """PASSINGMARKS clamped to the sheet length, which can be shorter than
        EXAM_QUESTION_COUNT for a terse diff."""
        return min(self.passingmarks, question_count)


def get_settings() -> Settings:
    """Build Settings, exiting with the names of any missing variables."""
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
