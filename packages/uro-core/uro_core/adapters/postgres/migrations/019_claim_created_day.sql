-- C5 (docs/19, D-34): temporal state on claims, so a rule can EXPIRE a stale rumor (RL-8). A claim
-- gains its in-fiction birth day (from the beat's world_time), and `expire_claims` retracts a
-- module rumor older than N days (truth → false). NOT branch-scoped meta — it is an ordinary
-- projection column, so it forks/replays/snapshots with proj_claims by construction (it is already
-- in _SNAPSHOT_TABLES). Default 0 for pre-C5 claims (they read as "day 0", i.e. always old enough).
ALTER TABLE proj_claims ADD COLUMN created_day INT NOT NULL DEFAULT 0;
