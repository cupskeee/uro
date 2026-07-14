-- B8 (docs/18, D-36): participant-scoped memory — a player's out-of-world meta-knowledge that
-- SURVIVES A FORK (time-loop / roguelike / NG+). Keyed on (participant_id, world_ref) — NOT
-- branch_id — so it is deliberately OUTSIDE the branch axis: it is NOT a proj_* projection, is NOT
-- in the projector's _HANDLERS/_SNAPSHOT_TABLES, and is never touched by fork_branch's copy step, so
-- a fork neither copies nor resets it (fork-immunity is structural). It is also NOT an event and has
-- no DIRECT path into proj_claims/proj_beliefs — nothing wires it into the extractor, planner, or
-- belief-propagation, so it cannot DIRECTLY become canon or an NPC belief (structural for direct
-- wiring). It surfaces only in the narrator prompt (labelled "the world does NOT know this"); a note
-- the narrator ECHOES into prose could then be re-extracted like any narrator output — that residual
-- is fenced by narrator-tier trust (by-policy), the same fence every narrator input already has (13).
-- world_ref is the SCOPE knob: the world_id gives per-(world x participant) scope (the default; a
-- within-world fork keeps the same world_id, so it survives); a sentinel could later give global.
CREATE TABLE participant_notes (
    participant_id TEXT   NOT NULL,
    world_ref      TEXT   NOT NULL,   -- the world_id (scope); a fork stays in-world so notes persist
    key            TEXT   NOT NULL,   -- caller-supplied or sha256(text): the dedup key across loops
    text           TEXT   NOT NULL,
    pinned         BOOLEAN NOT NULL DEFAULT FALSE,   -- always-surface vs entity-triggered
    entity_refs    TEXT[]  NOT NULL DEFAULT '{}',    -- e.g. {name:vault} — surfaces when mentioned
    PRIMARY KEY (participant_id, world_ref, key)
);
