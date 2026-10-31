-- D-66: complete the metering record. A call now carries the world and branch it served (so usage
-- can be filtered by world or campaign — docs/08 had deferred that filter for want of exactly these
-- columns) and, for a STRUCTURED (non-streamed) call, the provider's full response: the raw
-- extractor/planner/judge/backfill JSON was only ever logged as a 500-char excerpt on failure.
-- Narration isn't stored here (it is already the beat's BeatResolved). Still operational data, off
-- the branch axis: never projected, forked, or exported, and prunable like the rest of llm_calls.
-- Rows before this migration have NULLs — their world is simply unknown.
ALTER TABLE llm_calls ADD COLUMN world_id TEXT;
ALTER TABLE llm_calls ADD COLUMN branch_id TEXT;
ALTER TABLE llm_calls ADD COLUMN response TEXT;
CREATE INDEX llm_calls_world_idx ON llm_calls (world_id, created_at);
CREATE INDEX llm_calls_branch_idx ON llm_calls (branch_id, created_at);
