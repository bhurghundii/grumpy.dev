-- V2__answers_grading_metadata.sql
-- Records which model and prompt version produced each verdict. Without
-- this, a later grading change can't be evaluated against what came
-- before it. Nullable: FakeGrader-produced rows (used in fast/free tests)
-- leave these unset rather than faking a model name.

ALTER TABLE answers ADD COLUMN model TEXT;
ALTER TABLE answers ADD COLUMN prompt_version TEXT;
