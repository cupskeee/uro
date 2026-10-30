-- D-57: the transactional OUTBOX (docs/07) — every commit made by the engine (a beat, a campaign
-- start, a time-skip, a reaction, a world's genesis) writes one row here IN THE SAME TRANSACTION as
-- the commit, so a published message exists if and only if the commit does. A relay drains pending
-- rows to a publisher (NATS JetStream, or an in-process subscriber) and stamps `published_at` —
-- at-least-once; the NATS adapter dedups server-side on the commit id. Like llm_calls and
-- session_tokens this is OPERATIONAL, not world truth: off the branch axis, not a proj_* table, not
-- in _HANDLERS/_SNAPSHOT_TABLES, never copied by fork_branch, never exported. The row carries only
-- the commit's coordinates; the relay reads the events themselves from `events` when it publishes.
-- import_world does not backfill rows: an imported world's prior history is reachable through
-- export, and every commit made after the import is published as normal.
CREATE TABLE outbox (
    seq          BIGSERIAL   PRIMARY KEY,           -- global publish order
    world_id     TEXT        NOT NULL,
    branch_id    TEXT        NOT NULL,
    commit_id    TEXT        NOT NULL UNIQUE REFERENCES commits (commit_id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);
CREATE INDEX outbox_pending_idx ON outbox (seq) WHERE published_at IS NULL;
