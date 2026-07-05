-- Phase 0 timeline core (docs/03, 07). Append-only events + hash-chained commits.
-- Projections, snapshots, edges, pgvector, and the full event catalog arrive later.

CREATE TABLE worlds (
    world_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE commits (
    commit_id   TEXT PRIMARY KEY,
    world_id    TEXT NOT NULL REFERENCES worlds(world_id),
    parent_id   TEXT REFERENCES commits(commit_id),   -- NULL = world genesis
    commit_hash TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX commits_parent_idx ON commits(parent_id);

CREATE TABLE branches (
    branch_id   TEXT PRIMARY KEY,
    world_id    TEXT NOT NULL REFERENCES worlds(world_id),
    name        TEXT NOT NULL,
    head_commit TEXT REFERENCES commits(commit_id),
    forked_from TEXT REFERENCES commits(commit_id)
);

-- append-only; NEVER updated or deleted. Canonical event types: docs/12.
CREATE TABLE events (
    event_id    TEXT PRIMARY KEY,
    commit_id   TEXT NOT NULL REFERENCES commits(commit_id),
    seq         INT  NOT NULL,
    event_type  TEXT NOT NULL,
    entity_refs TEXT[] NOT NULL DEFAULT '{}',
    world_time  JSONB NOT NULL,
    caused_by   JSONB NOT NULL,
    payload     JSONB NOT NULL
);
CREATE INDEX events_commit_idx ON events(commit_id);
CREATE INDEX events_type_idx ON events(event_type);

CREATE TABLE campaigns (
    campaign_id TEXT PRIMARY KEY,
    world_id    TEXT NOT NULL REFERENCES worlds(world_id),
    branch_id   TEXT NOT NULL REFERENCES branches(branch_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
