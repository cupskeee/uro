-- B10 (docs/18, D-39): runtime session-token management. A durable, hashed, revocable token
-- registry so a player added to a RUNNING server (bind_pc / uro campaign join) can AUTHENTICATE
-- without a server restart (the --token argv map is frozen at launch). Like participant_notes
-- (017), this is deliberately OFF THE BRANCH AXIS: it is NOT a proj_* projection, is NOT in the
-- projector's _HANDLERS/_SNAPSHOT_TABLES, and is never touched by fork_branch / import_world — a
-- token authenticates a HUMAN to the server, independent of which what-if branch they explore, so a
-- fork must neither copy nor reset it (structural, mirrored by a fork-exclusion test). It is also
-- NOT event-sourced (D-31/D-39 kept: no session/turn/token state enters the log — nothing new
-- rides fork_branch). Only sha256(token) is stored; the plaintext is returned once at mint and
-- never at rest. Durability is REQUIRED: an in-process-only registry would lose runtime-minted
-- tokens on restart while proj_pcs survives, permanently locking out every runtime-added player.
CREATE TABLE session_tokens (
    token_hash     TEXT        NOT NULL PRIMARY KEY,  -- sha256(token); plaintext never stored
    participant_id TEXT        NOT NULL,
    campaign_id    TEXT        NOT NULL,
    revoked        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX session_tokens_participant_idx ON session_tokens (participant_id, campaign_id);
