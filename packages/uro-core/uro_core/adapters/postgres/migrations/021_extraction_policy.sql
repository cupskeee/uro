-- D-49: the instance-level emergent-extraction policy — which categories play may CREATE. Like
-- the model-connection registry (020), this is instance/deployment config, OFF the event/branch
-- axis: NOT a projection, NOT event-sourced, never touched by fork_branch/import_world/export_world.
-- A single row (id='singleton'); the store upserts it and reads it per beat. Defaults all-on, so a
-- pre-existing instance behaves exactly as before (actors + claims, now also places).
CREATE TABLE extraction_policy (
    id             TEXT        NOT NULL PRIMARY KEY DEFAULT 'singleton',
    extract_actors BOOLEAN     NOT NULL DEFAULT TRUE,
    extract_places BOOLEAN     NOT NULL DEFAULT TRUE,
    extract_claims BOOLEAN     NOT NULL DEFAULT TRUE,  -- includes beliefs; the engine relies on these
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO extraction_policy (id) VALUES ('singleton');
