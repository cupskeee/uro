-- Phase 2 timeline substrate (docs/03, 07). Markers, snapshots, and the places
-- projection — the machinery for branch-from-any-commit and materialization.
--
-- `depth` = generation from genesis (genesis = 0, each child = parent + 1). Commits
-- form a tree (one parent each; branches share prefixes), so depth is well-defined.
-- It gives cheap "snapshot every N commits", ordered replay, and git-log numbering
-- without walking ancestry per beat. Backfilled here for any pre-Phase-2 commits.

ALTER TABLE commits ADD COLUMN depth INT NOT NULL DEFAULT 0;

WITH RECURSIVE gen AS (
    SELECT commit_id, 0 AS depth FROM commits WHERE parent_id IS NULL
    UNION ALL
    SELECT c.commit_id, gen.depth + 1
    FROM commits c JOIN gen ON c.parent_id = gen.commit_id
)
UPDATE commits SET depth = gen.depth FROM gen WHERE commits.commit_id = gen.commit_id;

CREATE INDEX commits_depth_idx ON commits(depth);

-- Markers: named, immutable refs to a commit (campaign-a-end, "the meteor falls").
-- Refs, not events (docs/12 rule 2) — creating one never touches the log.
CREATE TABLE markers (
    marker_id  TEXT PRIMARY KEY,
    world_id   TEXT NOT NULL REFERENCES worlds(world_id),
    name       TEXT NOT NULL,
    commit_id  TEXT NOT NULL REFERENCES commits(commit_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (world_id, name)
);

-- Snapshots: full materialized projection state at a commit, serialized every N
-- commits and at markers. Keyed by commit_id — state-at-a-commit is branch-independent
-- (a commit's ancestry is immutable), so a snapshot serves any branch that passes
-- through it. Materializing commit X = nearest ancestor snapshot ≤ X + replay forward;
-- branching is therefore O(replay window), not O(history). state_hash makes each blob
-- tamper-evident on its own (docs/07, the export-pack trust anchor).
CREATE TABLE snapshots (
    commit_id  TEXT PRIMARY KEY REFERENCES commits(commit_id),
    state_hash TEXT NOT NULL,
    state      JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Places projection (docs/02). Deferred in Phase 1 ("places/edges when needed");
-- the meteor test needs it — PlaceDestroyed(Vel) is simply true on the aftermath
-- branch and false on a what-if forked before the strike.
CREATE TABLE proj_places (
    branch_id   TEXT NOT NULL,
    place_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'site',    -- region | settlement | site (docs/02)
    status      TEXT NOT NULL DEFAULT 'active',  -- active | destroyed
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (branch_id, place_id)
);
