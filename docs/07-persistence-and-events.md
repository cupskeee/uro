# 07 — Persistence and Events

Decision on record: **Postgres as the single storage system for the MVP**, behind ports. Three databases on day one of an immature engine is how PoCs die; Postgres covers relational + vector + graph-enough, runs identically in local Docker and any managed cloud (the owner's "local and live parity" requirement), and every specialized store remains a later adapter swap.

## One database, three roles

| Role | MVP implementation | Swap-to later (same port) | Trigger to swap |
|---|---|---|---|
| Relational / event store | Postgres tables | — (Postgres stays) | — |
| Vector memory | **pgvector** extension | Qdrant (also local-Docker + cloud) | corpus ≫ 1M embeddings or recall latency hurts |
| Graph | **edge table** + recursive CTEs | Neo4j (Docker + Aura) or Apache AGE in-place | multi-hop traversals become hot/unwritable as SQL |

Ports (built so far: `EventStore`, `ProjectionQueries`, `VectorIndex`; planned: `GraphQueries`, `BlobStore`) — core code never imports psycopg/SQLAlchemy directly. A SQLite adapter was considered for zero-dep local mode and rejected (pgvector/JSONB divergence would make "local" a different engine than "live"); local = `docker compose up postgres`.

## Schema sketch (timeline core)

```sql
-- append-only; NEVER updated or deleted. Canonical event types: 12-event-catalog.md
CREATE TABLE events (
  event_id    TEXT PRIMARY KEY,          -- ULID
  commit_id   TEXT NOT NULL REFERENCES commits(commit_id),
  seq         INT  NOT NULL,             -- order within commit
  event_type  TEXT NOT NULL,             -- 'ClaimRecorded', 'ActorPromoted', ...
  entity_refs TEXT[] NOT NULL,           -- touched entity ids (index for recall)
  world_time  JSONB NOT NULL,            -- in-fiction time (calendar model: OQ-5)
  caused_by   JSONB NOT NULL,            -- player action | agenda | history | system
  payload     JSONB NOT NULL
);

CREATE TABLE commits (
  commit_id   TEXT PRIMARY KEY,
  world_id    TEXT NOT NULL,
  parent_id   TEXT REFERENCES commits(commit_id),   -- NULL = world genesis
  commit_hash TEXT NOT NULL,             -- h(parent_hash, canonical(events))
  created_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE branches (
  branch_id TEXT PRIMARY KEY, world_id TEXT NOT NULL,
  name TEXT NOT NULL, head_commit TEXT NOT NULL REFERENCES commits(commit_id),
  forked_from TEXT REFERENCES commits(commit_id)
);
-- markers: named commits. snapshots: (commit_id, state_hash, state blob) every N commits + at markers.
-- state_hash makes each snapshot blob tamper-evident on its own and lets a snapshot-rooted
-- branch export carry a trust anchor (see Export packs).
```

Projections are rebuildable caches keyed by branch (current-state tables are per-branch), versioned by projector code hash so schema evolution = replay, not migration surgery on truth. **Built so far** (Phase 1–2): `actors`, `places`, `claims`, `beliefs`, `pcs` (per-branch PC-binding — the substrate for "is this actor a PC on this branch?"), plus `memory_index`. **Planned** as later phases need them: `factions`, `threads`, `edges`, `chronicle`, `entity_index`. The `edges` table is the intended graph story for MVP — `(edge_id, branch_id, src, rel_type, dst, weight, valid_from_commit, valid_to_commit)`, per-commit validity giving "relations at commit X" for materialization. The shipped read port is `ProjectionQueries`: `(branch_id, entity_id)` **branch-current-state** methods (`get_actor`, `get_place`, `is_pc`, …); point-in-time state is reached by *forking* (nearest snapshot + replay into a new branch, `fork_branch`), not a commit-addressed read. A `state_at(branch, commit)`-shaped port with a copy-on-write adapter behind it remains the intended seam — so core needn't bake in branch-locality — but is not built yet.

**Entity-resolution index (planned — not built yet; the Phase-1 gauntlet resolves on name/alias only).** `entity_index(branch_id, entity_id, content_hash)` will point at embedded name/aliases/one-line-descriptor vectors (in the same `embeddings(content_hash, vector)` table as memory, but a *separate corpus*). A side-effecting projector writes it on `ActorCreated`/`PlaceCreated`/`FactionCreated`/`EntityAliasAdded`; entity resolution (`13`) kNN-matches a candidate name against it before accepting any `*Created`. This is what lets resolution dedup even seeded entities never yet narrated (which have zero memory chunks). Copied on fork like `memory_index`; never re-embedded.

**Fork semantics — copy-on-fork (D-20).** Creating a branch materializes the new branch's projection rows from the nearest snapshot + replay: a bounded copy, no copy-on-write cleverness. At PoC scale, paying one projection build per fork is the honest price for keeping every query simple and branch-local. Embeddings are the deliberate exception: vectors live exactly once in `embeddings(content_hash, vector)`, and only lightweight `memory_index(branch_id, content_hash, entity_refs, commit_id)` membership rows are copied. Forking never re-embeds anything, and semantic recall stays a plain branch-filtered pgvector query — instead of a commit-ancestry reachability check that no vector index can serve.

Also stored: `llm_calls` (prompt hash, response, tokens, latency, stage tag — powers usage metering, recorded-response replay, and debugging; **prunable**, never part of world truth) and `probe_reports`.

## Events in motion (bus)

MVP: **in-process async event bus** (projection updates, simulation triggers subscribe to committed beats) + a transactional **outbox table** written in the same transaction as the commit. That's the whole story until the engine is distributed. When `uro-server` scales beyond one process, an outbox relay targets **NATS JetStream** (first choice: tiny single binary locally, managed offerings live) or Redis Streams; Kafka is explicitly overkill (D-13). Because publishing already goes through the outbox port, this swap touches zero core code.

## Export packs (the sharing primitive)

Platforms build community features; the engine makes state **portable**:

- `uro export world W` → archive: world pack (definition) + full event log + markers + manifest with hash chain. Verifiable, importable on any Uro instance.
- `uro export branch B [--at COMMIT]` → snapshot-rooted variant: materialized state at a commit + events since — lets someone share "my world as of the meteor" without their full history.
- Campaign exports include the chronicle projection rendered to markdown (human-readable session/story log — what a platform would publish; what the report called "Story Log Publishing").
- Excluded always: credentials, `llm_calls`, anything session/user-scoped.

Import validates schema versions and replays. Hash verification depends on the export shape: a **full world pack** chain-verifies event-by-event back to genesis. A **snapshot-rooted branch pack** can only chain-verify the events *after* its root; the root state blob is a **trust anchor** whose `state_hash` the manifest records (so tampering is detectable) — but its provenance back to genesis is intentionally not exported. That is the whole point of sharing "my world as of the meteor" without the full history; the honesty is in saying so, not in pretending the chain reaches genesis. Fork-on-import is the default (imported state becomes a new branch root) so imports never mutate existing timelines.

## Data privacy commitments (engine-owned)

- Credentials: encrypted at rest, excluded from exports/logs (see `04-llm-integration.md`).
- Story content: lives only in this database and in requests to explicitly-bound model endpoints; no telemetry.
- Wipe: `uro world delete` hard-deletes the world's events, projections, embeddings, and llm_calls.
