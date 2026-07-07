-- Phase 4 review fix: threads become real state. World-pack conflict seeds (authored) and AI
-- backfill (provenance=ai_backfill) now commit as ThreadCreated events at import (emitter S),
-- so the machine's inventions are reviewable committed state — not a discarded in-memory model.
CREATE TABLE proj_threads (
    branch_id   TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    stakes      TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'dormant',
    provenance  TEXT NOT NULL DEFAULT 'author',   -- author | ai_backfill (docs/09)
    PRIMARY KEY (branch_id, thread_id)
);
