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

Projections are rebuildable caches keyed by branch (current-state tables are per-branch), versioned by projector code hash so schema evolution = replay, not migration surgery on truth. **Built so far** (Phase 1–5): `actors` (incl. an `alive|dead` status, migration 012), `places`, `claims`, `beliefs`, `pcs` (per-branch PC-binding), `sheets`, `items` (Phase 3), `factions`, `edges`, `threads` (Phase 4 — migrations 010/011). These are all rebuilt by REPLAY (the projector's handlers). `memory_index` is the one exception — it is written by the `add_memory` port during live beats (it needs the non-deterministic embedder), so replay cannot reconstruct it: instead it is **copied on fork** and **carried in the export bundle** (with its content-hash-shared `embeddings`), so a forked or imported world keeps its long-range recall. **Planned** as later phases need them: `chronicle`, `entity_index`. The shipped `edges` table is CURRENT-STATE — `(branch_id, src, rel_type, dst, weight, attrs)`, keyed by `(branch_id, src, rel_type, dst)`; the fuller design adds per-commit validity (`valid_from_commit`, `valid_to_commit`) for "relations at commit X" — **deferred** (copy-on-fork already materializes current state per branch). The shipped read port is `ProjectionQueries`: `(branch_id, entity_id)` **branch-current-state** methods (`get_actor`, `get_place`, `is_pc`, …); point-in-time state is reached by *forking* (nearest snapshot + replay into a new branch, `fork_branch`), not a commit-addressed read. A `state_at(branch, commit)`-shaped port with a copy-on-write adapter behind it remains the intended seam — so core needn't bake in branch-locality — but is not built yet.

**Entity resolution — two layers.** *Shipped:* the gauntlet resolves on **canonical** name/alias — case-folded, whitespace-collapsed, and leading article stripped (`extraction.canonical_name`, mirrored in `find_actor_by_name`) — so an actor extracted as "the woman" and later mentioned as "woman" (or "The Duke"/"the Duke") resolves to ONE entity instead of splitting (a real duplication a live model produces). *Planned (the embedding kNN index, OQ-3, for SEMANTIC matches like "hooded stranger" ≈ "stranger" that canonicalization can't catch):* `entity_index(branch_id, entity_id, content_hash)` will point at embedded name/aliases/one-line-descriptor vectors (in the same `embeddings(content_hash, vector)` table as memory, but a *separate corpus*). A side-effecting projector writes it on `ActorCreated`/`PlaceCreated`/`FactionCreated`/`EntityAliasAdded`; entity resolution (`13`) kNN-matches a candidate name against it before accepting any `*Created`. This is what lets resolution dedup even seeded entities never yet narrated (which have zero memory chunks). Copied on fork like `memory_index`; never re-embedded.

**Fork semantics — copy-on-fork (D-20).** Creating a branch materializes the new branch's projection rows from the nearest snapshot + replay: a bounded copy, no copy-on-write cleverness. At PoC scale, paying one projection build per fork is the honest price for keeping every query simple and branch-local. Embeddings are the deliberate exception: vectors live exactly once in `embeddings(content_hash, vector)`, and only lightweight `memory_index(branch_id, content_hash, entity_refs, commit_id)` membership rows are copied. Forking never re-embeds anything, and semantic recall stays a plain branch-filtered pgvector query — instead of a commit-ancestry reachability check that no vector index can serve.

Also stored: `llm_calls` (prompt hash, response, tokens, latency, stage tag — powers usage metering, recorded-response replay, and debugging; **prunable**, never part of world truth) and `probe_reports`.

## Events in motion (bus)

**As built (honest):** projections are updated by the projector **inline, in the same transaction as the commit** (`store._append` → `apply_event`) — the rebuild-by-replay invariant holds and nothing races, but there is **no async event bus and no `outbox` table yet** (both were the MVP sketch below; NOT built). Live streaming to clients is the server's per-connection `SessionHub` fan-out, not an outbox relay.

*Planned (not built):* an **in-process async event bus** + a transactional **outbox table** so simulation triggers/relays subscribe to committed beats; when `uro-server` scales beyond one process, an outbox relay targets **NATS JetStream** or Redis Streams (Kafka is overkill, D-13). Publishing would move behind an outbox port so the swap touches zero core code.

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
- Wipe (planned, NOT built): a `uro world delete` that hard-deletes the world's events, projections, embeddings, and llm_calls — the privacy commitment; no such command ships yet.
