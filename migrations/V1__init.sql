-- V1__init.sql
-- The complete v1 schema: one row per (repo, PR, head commit), holding the diff,
-- the exam sheet, per-question state and the verdict status.

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    diff TEXT NOT NULL,
    -- The first question on the sheet, kept alongside `questions` so a row
    -- with no sheet still degrades to a one-question exam.
    question TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'passed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    -- The exam sheet: every question the session asks, generated at session
    -- creation with the high-level question always first. Each element is
    -- {question, start_line, end_line}.
    questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- The blind reading of the diff, computed once at creation and reused to
    -- grade every screen so the marking basis is consistent.
    interpretation TEXT,
    -- Per-question state keyed by question index (as a string); the session's
    -- verdict is derived from it. See app/db/exam.py.
    marks JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Idempotent session creation relies on this:
-- INSERT ... ON CONFLICT (repo, pr_number, head_sha) DO NOTHING RETURNING *.
CREATE UNIQUE INDEX IF NOT EXISTS sessions_repo_pr_number_head_sha_key
    ON sessions (repo, pr_number, head_sha);
