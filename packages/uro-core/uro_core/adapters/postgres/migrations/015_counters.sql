-- Computation Layer INC-C1 (docs/19, D-34): pack-authored numeric state as engine-owned, event-
-- sourced integer counters. A CounterChanged event UPSERTs a row here; the projector is the sole
-- writer, and because proj_counters joins projector._HANDLERS + _SNAPSHOT_TABLES it forks/replays/
-- snapshots/exports by construction (the fix for shadow-state that did not ride fork_branch —
-- Sable G-1/G-10). value is BIGINT (integer only — no float, for cross-platform arithmetic
-- determinism). created_day is preserved on UPSERT (updated_day moves) so claim/counter age math
-- works. Branch-scoped like every projection → per-branch isolation + copy-on-fork inherited.
CREATE TABLE proj_counters (
    branch_id    TEXT   NOT NULL,
    scope_ref    TEXT   NOT NULL,   -- the entity the counter hangs on (a faction/thread/place id)
    key          TEXT   NOT NULL,   -- the counter name (e.g. "tension", "heat", "influence")
    value        BIGINT NOT NULL DEFAULT 0,
    created_day  INT    NOT NULL DEFAULT 0,
    updated_day  INT    NOT NULL DEFAULT 0,
    PRIMARY KEY (branch_id, scope_ref, key)
);
