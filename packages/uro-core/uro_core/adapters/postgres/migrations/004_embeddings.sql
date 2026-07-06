-- Phase 1 semantic memory (docs/04, 07). pgvector-backed.
-- Vectors live once, keyed by content hash (dedup); per-branch membership rows point
-- at them and carry the recallable text. Copy-on-fork of memberships is a later increment.
-- Unbounded `vector` (dimension set by the deployment's embedder) + exact search for now;
-- an HNSW index with a pinned dimension arrives when scale demands it.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE embeddings (
    content_hash TEXT PRIMARY KEY,
    vector       vector NOT NULL
);

CREATE TABLE memory_index (
    memory_id    TEXT PRIMARY KEY,
    branch_id    TEXT NOT NULL,
    content_hash TEXT NOT NULL REFERENCES embeddings(content_hash),
    kind         TEXT NOT NULL,              -- 'beat' | 'synopsis' | 'journal' | …
    text         TEXT NOT NULL,              -- the recallable snippet
    entity_refs  TEXT[] NOT NULL DEFAULT '{}',
    commit_id    TEXT NOT NULL
);
CREATE INDEX memory_index_branch_idx ON memory_index(branch_id);
