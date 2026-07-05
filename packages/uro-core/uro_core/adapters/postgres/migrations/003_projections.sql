-- Phase 1 projections (docs/02, 03, 07). Rebuildable read-models DERIVED from the
-- event log — never written directly, only by the projector, in the same transaction
-- as the commit that produced the events. Keyed by (branch_id, entity_id).
-- Phase 1 has a single branch (main); copy-on-fork arrives in Phase 2.

CREATE TABLE proj_actors (
    branch_id TEXT NOT NULL,
    actor_id  TEXT NOT NULL,
    name      TEXT NOT NULL,
    tier      INT  NOT NULL,
    role      TEXT NOT NULL DEFAULT '',
    aliases   TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (branch_id, actor_id)
);

CREATE TABLE proj_claims (
    branch_id    TEXT NOT NULL,
    claim_id     TEXT NOT NULL,
    statement    TEXT NOT NULL,
    subject_refs TEXT[] NOT NULL DEFAULT '{}',
    truth        TEXT NOT NULL,          -- true | false | unknown (engine ground truth)
    origin       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (branch_id, claim_id)
);
CREATE INDEX proj_claims_subject_idx ON proj_claims USING GIN (subject_refs);

CREATE TABLE proj_beliefs (
    branch_id    TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    claim_id     TEXT NOT NULL,
    confidence   DOUBLE PRECISION NOT NULL,   -- how strongly the actor holds the claim
    learned_from TEXT,
    PRIMARY KEY (branch_id, actor_id, claim_id)
);
CREATE INDEX proj_beliefs_actor_idx ON proj_beliefs (branch_id, actor_id);
