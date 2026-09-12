"""Request/response shapes for the phase-2 endpoints.

head_sha/base_sha require a full 40-character hex SHA — that's what GitHub
Actions actually provides (github.sha and PR head/base SHAs are always full
SHA-1 hashes), and it's a deliberate choice, not an oversight: the unique
index and verdict lookups are exact-string matches, not prefix resolution,
so accepting short SHAs alongside full ones would let the same commit
silently produce two unrelated sessions.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, ValidationInfo, field_validator

_REPO_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class CreateSessionRequest(BaseModel):
    repo: str
    pr_number: int = Field(gt=0)
    head_sha: str
    base_sha: str
    diff: str

    @field_validator("repo")
    @classmethod
    def _validate_repo(cls, value: str) -> str:
        if not _REPO_RE.match(value):
            raise ValueError("repo must be in 'owner/name' form, with both parts non-empty")
        return value

    @field_validator("head_sha", "base_sha")
    @classmethod
    def _validate_sha(cls, value: str, info: ValidationInfo) -> str:
        if not _SHA_RE.match(value):
            raise ValueError(f"{info.field_name} must be a 40-character hex SHA")
        return value

    @field_validator("diff")
    @classmethod
    def _validate_diff(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("diff must not be empty")
        return value
