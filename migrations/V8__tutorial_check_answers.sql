-- V8__tutorial_check_answers.sql
-- The developer's answers to each tutorial step's check question
-- (app/grading.py:TutorialStep.check_question). Each step asks a short
-- question that has to be answered in the developer's own words before the
-- walkthrough lets them move on; once answered, the page reveals the
-- model's reference answer beside theirs. Never graded: this only records
-- that they wrote something, and what.
--
-- Shape: an object keyed by 1-based step number as a string, e.g.
-- {"1": {"body": "...", "js_active": true}}. The questions and reference
-- answers themselves live in `steps` (V5); this holds only what the
-- developer wrote. Write-once per step (app/tutorials.py:record_check_answer)
-- — after the reveal, rewriting an answer would just be copying it.
--
-- Nullable: NULL and '{}' both mean "nothing answered yet", and rows
-- written before this migration have no checks to answer. Same
-- nullable-for-legacy-rows reasoning as V5__tutorial_steps.sql and
-- V6__paste_guard.sql.

ALTER TABLE tutorials ADD COLUMN IF NOT EXISTS check_answers JSONB;
