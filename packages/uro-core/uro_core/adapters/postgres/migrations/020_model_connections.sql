-- D-47 (docs/20): instance-level, DB-backed model-connection registry. LLM provider config moves
-- out of `uro.toml [llm.roles]` + the `--provider` flag + env into the DB, configurable over the
-- uro-server API (from uro-cli / uro-loom). Like session_tokens (018) and participant_notes (017),
-- these are deliberately OFF THE BRANCH/EVENT AXIS: NOT proj_* projections, NOT in the projector's
-- _HANDLERS/_SNAPSHOT_TABLES, and NEVER touched by fork_branch / import_world / export_world — this
-- is instance/deployment configuration, not world state, so a fork must neither copy nor reset it,
-- and an exported world must never carry another instance's provider credentials.

-- Credentials, split from connections so one credential can back several connections and the auth
-- lifecycle (refresh/expiry/re-auth) is independent of the model catalog. access_token/refresh_token
-- are Fernet CIPHERTEXT (app-level, under an env KEK URO_SECRET_KEY that lives OUTSIDE the DB — else
-- a dump defeats the encryption); plaintext is never at rest. NULL access_token = a keyless provider
-- (e.g. a local Ollama endpoint or the stub).
CREATE TABLE provider_credentials (
    id            TEXT        NOT NULL PRIMARY KEY,   -- ULID
    provider      TEXT        NOT NULL,               -- the provider kind this credential is for
    access_token  TEXT,                               -- Fernet ciphertext (NULL = keyless)
    refresh_token TEXT,                               -- Fernet ciphertext (OAuth)
    auth_mode     TEXT        NOT NULL DEFAULT 'api_key',  -- api_key | oauth_pkce | oauth_device
    last_refresh  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per connected provider endpoint. cached_models is the discovered catalog, each entry
-- carrying its modality (chat | embedding | unknown) so an embedder binding can be validated
-- (populated by a later `refresh`; NULL until then). The soft link to a credential is ON DELETE SET
-- NULL: deleting a credential UNLINKS the connections that used it (they become unauthenticated),
-- never deletes them, and a connection cannot point at a credential id that does not exist while
-- linked — integrity without a surprise cascade.
CREATE TABLE model_connections (
    id            TEXT        NOT NULL PRIMARY KEY,   -- ULID
    name          TEXT        NOT NULL,
    provider      TEXT        NOT NULL,               -- anthropic | openai | openai_compat | local | stub | ...
    base_url      TEXT,                               -- per-connection endpoint override
    auth_id       TEXT        REFERENCES provider_credentials (id) ON DELETE SET NULL,
    is_enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    cached_models JSONB,                              -- [{"id": ..., "modality": "chat|embedding|unknown"}]
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Which connection+model backs each engine role. `role` is the PK (one binding per role per
-- instance). The reserved `default` role IS the ProviderRouter default (the fallback for any
-- unbound role). ON DELETE CASCADE: deleting a connection unbinds its roles, which then fall back to
-- `default` — the expected direction (you removed the thing they pointed at).
CREATE TABLE role_bindings (
    role          TEXT        NOT NULL PRIMARY KEY,   -- default | narrator | extractor | planner | embedder | dialogue | judge
    connection_id TEXT        NOT NULL REFERENCES model_connections (id) ON DELETE CASCADE,
    model         TEXT        NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX model_connections_auth_idx ON model_connections (auth_id);
