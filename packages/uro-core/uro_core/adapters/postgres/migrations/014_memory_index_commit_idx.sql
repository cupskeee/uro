-- Gap-report fix (Hollowloop G-10, found by a fork-per-loop game at 500 forks): the fork hot path
-- `_copy_memory` filters `memory_index WHERE commit_id = ANY(...)`, but the only index on the table
-- was on `branch_id` (migration 004) — so every fork did a Seq Scan over ALL of memory_index, which
-- holds one row per beat of EVERY world in the database. EXPLAIN ANALYZE showed ~17k rows discarded
-- per fork (30-60% of fork time), and the cost grows with total beats ever played, not with the
-- world being forked. One index removes the scan.
CREATE INDEX IF NOT EXISTS memory_index_commit_idx ON memory_index(commit_id);
