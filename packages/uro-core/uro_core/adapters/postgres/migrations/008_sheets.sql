-- Phase 3 inc 3.2: character-sheet projection (docs/06, 12). The store keeps the sheet
-- opaquely as JSONB; the pipeline reads it via the shared port Sheet (a d20-shaped contract
-- for now, OQ-13) and the ruleset owns its semantics. Written by the projector on
-- SheetUpdated (emitter R S),
-- carried per-branch and rebuilt on replay like every other projection. The mechanics gate
-- reads a PC/NPC sheet here to build a CheckRequest.
CREATE TABLE proj_sheets (
    branch_id  TEXT NOT NULL,
    actor_id   TEXT NOT NULL,
    ruleset_id TEXT NOT NULL DEFAULT '',
    sheet      JSONB NOT NULL,
    PRIMARY KEY (branch_id, actor_id)
);
