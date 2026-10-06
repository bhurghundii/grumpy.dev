"""Settings validation: GRUMPY_TOKEN strength and the GRUMPY_ALLOWED_REPOS
allow-list. Pure pydantic validation, no DB involved — deliberately doesn't
pull in the Testcontainers-backed grumpy_env fixture chain, since none of
this needs a real Postgres.
"""

from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def _set_required_env(monkeypatch: pytest.MonkeyPatch, *, token: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("GRUMPY_BASE_URL", "https://grumpy.example.com")
    monkeypatch.setenv("GRUMPY_TOKEN", token)
    monkeypatch.setenv("FAKE_GRADER", "true")


def test_placeholder_token_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token="changeme-generate-a-real-token")
    with pytest.raises(ValidationError, match="placeholder"):
        Settings()


def test_short_token_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token="short-token-12")  # 14 chars
    with pytest.raises(ValidationError, match="too short"):
        Settings()


def test_generated_token_is_accepted(monkeypatch) -> None:
    # Mirrors tests/conftest.py's grumpy_env fixture generation — a
    # regression proof that the new length floor doesn't break it.
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    Settings()


def test_get_settings_wraps_weak_token_as_system_exit(monkeypatch) -> None:
    _set_required_env(monkeypatch, token="too-short")
    with pytest.raises(SystemExit, match="GRUMPY_TOKEN"):
        get_settings()


def test_allowed_repos_unset_permits_any_repo(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.is_repo_allowed("anything/at-all")


@pytest.mark.parametrize("raw", ["", "  ", ",", " , , "])
def test_allowed_repos_blank_string_permits_any_repo(monkeypatch, raw) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", raw)
    settings = Settings()
    assert settings.is_repo_allowed("anything/at-all")


def test_allowed_repos_permits_listed_repos(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", "octo/repo-a,octo/repo-b")
    settings = Settings()
    assert settings.is_repo_allowed("octo/repo-a")
    assert settings.is_repo_allowed("octo/repo-b")


def test_allowed_repos_rejects_unlisted_repo(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", "octo/repo-a,octo/repo-b")
    settings = Settings()
    assert not settings.is_repo_allowed("evil/other")


def test_allowed_repos_trims_whitespace_around_entries(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", " octo/repo-a , octo/repo-b ")
    settings = Settings()
    assert settings.is_repo_allowed("octo/repo-a")
    assert settings.is_repo_allowed("octo/repo-b")


def test_allowed_repos_rejects_malformed_entry(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", "not-a-valid-repo")
    with pytest.raises(ValidationError, match="not-a-valid-repo"):
        Settings()


def test_body_size_defaults_are_generous(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.max_request_body_bytes > settings.max_diff_bytes
    assert settings.max_request_body_bytes > settings.max_answer_bytes


def test_max_request_body_bytes_below_max_diff_bytes_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "100")
    with pytest.raises(ValidationError, match="MAX_REQUEST_BODY_BYTES"):
        Settings()


def test_max_request_body_bytes_below_max_answer_bytes_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    # Above max_diff_bytes (400_000) but below the max_answer_bytes
    # default (20_000) is impossible, so raise max_diff_bytes too — this
    # isolates the second half of the validator from the first.
    monkeypatch.setenv("MAX_DIFF_BYTES", "100")
    monkeypatch.setenv("MAX_ANSWER_BYTES", "1000")
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "500")
    with pytest.raises(ValidationError, match="MAX_REQUEST_BODY_BYTES"):
        Settings()


def test_max_session_attempts_default_is_three(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.max_session_attempts == 3


def test_max_session_attempts_zero_is_accepted_as_unlimited(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "0")
    settings = Settings()
    assert settings.max_session_attempts == 0


def test_negative_max_session_attempts_is_rejected(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "-1")
    with pytest.raises(ValidationError, match="MAX_SESSION_ATTEMPTS"):
        Settings()


def test_enable_tutorial_defaults_to_false(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.enable_tutorial is False


def test_enable_tutorial_can_be_turned_on(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    settings = Settings()
    assert settings.enable_tutorial is True


def test_meaniemode_defaults_to_false(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.meaniemode is False


def test_meaniemode_can_be_turned_on(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("MEANIEMODE", "true")
    settings = Settings()
    assert settings.meaniemode is True


def test_strictness_defaults_to_standard_for_every_repo(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    settings = Settings()
    assert settings.strictness_for("octo/anything") == "standard"


def test_grading_strictness_sets_the_default(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRADING_STRICTNESS", "strict")
    settings = Settings()
    assert settings.strictness_for("octo/anything") == "strict"


def test_repo_strictness_overrides_the_default(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRADING_STRICTNESS", "strict")
    monkeypatch.setenv("GRUMPY_REPO_STRICTNESS", " octo/scratch = Lenient , octo/core=standard ")
    settings = Settings()
    assert settings.strictness_for("octo/scratch") == "lenient"
    assert settings.strictness_for("octo/core") == "standard"
    assert settings.strictness_for("octo/other") == "strict"


@pytest.mark.parametrize("raw", ["", "  ", ","])
def test_repo_strictness_blank_string_means_no_overrides(monkeypatch, raw) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRUMPY_REPO_STRICTNESS", raw)
    settings = Settings()
    assert settings.strictness_for("octo/anything") == "standard"


@pytest.mark.parametrize(
    "raw", ["octo/repo=sloppy", "octo/repo", "not-a-repo=lenient", "octo/repo="]
)
def test_repo_strictness_rejects_malformed_entry(monkeypatch, raw) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRUMPY_REPO_STRICTNESS", raw)
    with pytest.raises(ValidationError, match="GRUMPY_REPO_STRICTNESS"):
        Settings()


def test_grading_strictness_rejects_unknown_level(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("GRADING_STRICTNESS", "sloppy")
    with pytest.raises(ValidationError, match="grading_strictness"):
        Settings()


def test_evaluator_unset_uses_defaults(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    size = Settings().evaluator_config().size
    assert size.max_changed_lines == 1000
    assert "*.lock" in size.exclude


@pytest.mark.parametrize("raw", ["", "   "])
def test_evaluator_blank_string_uses_defaults(monkeypatch, raw) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("EVALUATOR", raw)
    assert Settings().evaluator_config().size.max_changed_lines == 1000


def test_evaluator_partial_json_keeps_other_defaults(monkeypatch) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("EVALUATOR", '{"size": {"max_changed_lines": 3000}}')
    size = Settings().evaluator_config().size
    assert size.max_changed_lines == 3000
    assert "*.lock" in size.exclude


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"size": {"max_changed_line": 5}}',  # typo: must not silently default
        '{"sizes": {}}',
        '{"size": {"max_changed_lines": -1}}',
        '{"size": {"max_changed_lines": "lots"}}',
        '{"size": {"exclude": "*.lock"}}',
    ],
)
def test_evaluator_rejects_invalid_config(monkeypatch, raw) -> None:
    _set_required_env(monkeypatch, token=secrets.token_urlsafe(16))
    monkeypatch.setenv("EVALUATOR", raw)
    with pytest.raises(SystemExit, match="EVALUATOR is invalid"):
        get_settings()
