-- V4__tutorials.sql
-- Tutorial-breakdown flow: after a wrong-but-not-yet-terminal answer, a
-- developer can request an AI-generated step-by-step breakdown of the
-- diff, then explain it back to be graded on a looser rubric than the
-- main answer grader (see app/grading.py's _EXPLAIN_BACK_SYSTEM_PROMPT).
-- Passing or failing the explain-back both unlock a fresh attempt at the
-- original question — this is not a "keep retrying the tutorial until you
-- pass" design.
--
-- explanation_body/passed/reasoning are nullable: a freshly generated
-- tutorial has no explain-back yet ("in progress"). model/prompt_version
-- nullable for the same FakeGrader/legacy-row reason as
-- V2__answers_grading_metadata.sql.

CREATE TABLE IF NOT EXISTS tutorials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id),
    breakdown TEXT NOT NULL,
    explanation_body TEXT,
    passed BOOLEAN,
    reasoning TEXT,
    model TEXT,
    prompt_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tutorials_session_id_idx ON tutorials (session_id);

-- At most one "in progress" (unexplained) tutorial per session at a time.
-- GET /s/{token} relies on this to decide whether to show the explain-back
-- form; also closes the race where two concurrent
-- POST /s/{token}/tutorial calls could both persist an in-progress row.
CREATE UNIQUE INDEX IF NOT EXISTS tutorials_session_in_progress_key
    ON tutorials (session_id) WHERE explanation_body IS NULL;
