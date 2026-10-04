-- V7__answers_strictness.sql
-- The grading strictness (lenient/standard/strict) each answer was graded
-- at — GRADING_STRICTNESS, or the repo's GRUMPY_REPO_STRICTNESS override.
-- The level is resolved from config at grading time, so without this a
-- verdict can't be explained once the config has changed since.
--
-- Nullable: rows written before this migration were graded at what is now
-- called 'standard', but weren't recorded as such. Same
-- nullable-for-legacy-rows reasoning as V2__answers_grading_metadata.sql.

ALTER TABLE answers ADD COLUMN IF NOT EXISTS strictness TEXT;
