-- V6__paste_guard.sql
-- Whether app/static/nopaste.js — the paste guard on the answer and
-- explain-back textareas — was running when a submission was made. The
-- script adds a hidden `js_active` field to the form; its absence means
-- JavaScript was off, the script was blocked, or the POST didn't come from
-- the page at all (app/web.py:_log_if_unguarded).
--
-- Recorded, never enforced: nothing reads these to decide pass/fail. The
-- guard is friction, and anyone determined can retype an answer anyway.
--
-- Nullable rather than NOT NULL DEFAULT false: a row written before this
-- migration is "unknown", not "submitted without the guard". Same
-- nullable-for-legacy-rows reasoning as V2__answers_grading_metadata.sql,
-- V4__tutorials.sql and V5__tutorial_steps.sql.

ALTER TABLE answers ADD COLUMN IF NOT EXISTS js_active BOOLEAN;
ALTER TABLE tutorials ADD COLUMN IF NOT EXISTS explanation_js_active BOOLEAN;
