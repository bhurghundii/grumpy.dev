-- The exam sheet: the full set of questions a session asks, generated at
-- session creation (app/grading.py's generate_exam) with the high-level
-- question always first. The older single `question` column is kept and
-- still holds that first question, so a row from before this migration
-- degrades to a one-question sheet rather than breaking.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS questions jsonb NOT NULL DEFAULT '[]'::jsonb;
