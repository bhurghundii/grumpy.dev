-- V3__answers_reasoning.sql
-- Every GradeResult (FakeGrader and RealGrader alike) has carried a
-- `reasoning` string since phase 3, and nothing has ever persisted it —
-- a developer who gets FAILED has no way to see why. This stores it.
--
-- Nullable for the same reason model/prompt_version are (V2): existing
-- rows predate this column and shouldn't be backfilled with a lie.

ALTER TABLE answers ADD COLUMN reasoning TEXT;
