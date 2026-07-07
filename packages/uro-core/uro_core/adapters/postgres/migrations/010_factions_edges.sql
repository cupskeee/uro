-- Phase 4 inc 4.2: factions + the graph edge table (docs/02, 07). Deferred since Phase 1
-- ("places/edges when needed") — World seeding needs them: dynasties are factions and wars
-- are `at_war_with` edges, so seed 42 vs 43 differ here on identical geography.
--
-- Edges are the whole graph story for MVP. The PoC keeps CURRENT-STATE edges keyed by
-- (src, rel_type, dst); the per-commit temporal validity (valid_from/valid_to for point-in-time
-- reads, docs/07) is deferred — copy-on-fork already materializes current state per branch.
CREATE TABLE proj_factions (
    branch_id   TEXT NOT NULL,
    faction_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'faction',   -- faction | religion (docs/02)
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (branch_id, faction_id)
);

CREATE TABLE proj_edges (
    branch_id TEXT NOT NULL,
    src       TEXT NOT NULL,
    rel_type  TEXT NOT NULL,   -- member_of | located_in | at_war_with | rules | owns | knows | …
    dst       TEXT NOT NULL,
    weight    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    attrs     JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (branch_id, src, rel_type, dst)
);
CREATE INDEX proj_edges_src_idx ON proj_edges (branch_id, src);
CREATE INDEX proj_edges_dst_idx ON proj_edges (branch_id, dst);
