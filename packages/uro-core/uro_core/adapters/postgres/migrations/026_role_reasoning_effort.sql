-- A role binding may set how hard a reasoning model thinks (D-71). Measured on the replayed Ashfall
-- session (2026-11-01, gpt-5.6-luna via Codex): the extractor at effort `low` ran 4.0 s on average
-- against 6.6 s at the default, with the same claims recorded; `none` was faster still but recorded
-- atmosphere as fact. NULL = the provider's default (no effort sent).
ALTER TABLE role_bindings ADD COLUMN reasoning_effort TEXT;
