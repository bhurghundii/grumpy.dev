-- V1__init.sql
-- Phase 1 schema: sessions and answers. Keep this to what phases 2 and 3
-- need; grading-metadata columns are deliberately deferred until the grader
-- exists (see build doc §5).

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    diff TEXT NOT NULL,
    question TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'passed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- Phase 2 relies on this for idempotent session creation via
-- INSERT ... ON CONFLICT (repo, pr_number, head_sha) DO NOTHING RETURNING *.
CREATE UNIQUE INDEX IF NOT EXISTS sessions_repo_pr_number_head_sha_key
    ON sessions (repo, pr_number, head_sha);

CREATE TABLE IF NOT EXISTS answers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id),
    body TEXT NOT NULL,
    passed BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS answers_session_id_idx ON answers (session_id);
