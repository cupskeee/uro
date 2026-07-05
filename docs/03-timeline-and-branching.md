# 03 — Timeline and Branching

This is the engine's signature capability and the reason it exists as a PoC. Decision on record: **branch anywhere** (not just from campaign endings).

## The model, in git terms

| git | Uro | Notes |
|---|---|---|
| repository | **World** | One timeline universe |
| commit | **Beat commit** — the batch of domain events produced by one resolved beat | Append-only, hash-chained, parent-linked |
| branch (ref) | **Branch** — a named head pointer | `main` created at world seeding; campaigns play on branches |
| tag | **Marker** — named point (campaign start/end, "the meteor falls") | Cheap, immutable |
| checkout | **Materialization** — building world state at any commit | Served by snapshots + replay |
| fork | **Branch from any commit** | The meteor scenarios |
| merge | **Not supported.** | Divergent world histories don't merge; this is a feature. Re-evaluate only if a real need appears. |

## Event sourcing mechanics

1. **Events are the source of truth.** Every state change — a claim recorded, a belief spread, a border moved, an actor promoted, a crater formed — is a typed domain event with `event_id`, `commit_id`, `branch_id`, `world_time`, `caused_by` (player action / actor agenda / history-engine / system), and a payload.
2. **Commits batch events per beat.** One resolved player beat (or one off-screen simulation step) = one commit. Commits chain by parent pointer; a branch is just a movable head.
3. **Projections** (current-state tables per entity type, the edge table, the chronicle, embeddings) are derived, rebuildable caches keyed by `(branch, commit)` validity ranges. Never written directly.
4. **Snapshots** — full materialized state serialized every N commits (N≈50, tune later) and at every marker. Materializing an arbitrary commit = nearest snapshot ≤ commit + replay forward. Branching from an arbitrary commit is therefore O(replay window), not O(history).
5. **World time ≠ wall time.** Events carry in-fiction time; downtime/travel can skip months in one commit. Calendar model is OQ-5.

## What a fork carries

When a new campaign branches from commit `X` on world `W`:

**Carried (it's the same world):** all places and physical state (craters included), all actors and their beliefs/relationships at `X`, factions, borders, religions, wars, all claims, the entire chronicle up to `X` — NPCs in the new campaign can *remember and retell* the previous campaign's deeds as history.

**Not carried (it was that campaign's, not the world's):** the old party's PCs revert to ordinary world actors (the retired hero is now an NPC someone else might meet — or play, see below), campaign-scoped threads that never touched the world go dormant, session/UI state.

**Chosen at fork time:** new PCs (created fresh, or **adopt an existing world actor as your PC** — "continue as the ruler who caused the meteor" is exactly this), new seed for the RNG, optional prompt-pack override, optional time-skip (History service simulates the gap and commits it before play starts — "50 years later" as a first-class operation).

### The meteor test (canonical acceptance scenario)

Campaign A ends with a player-caused meteor destroying the city of Vel. Marker `campaign-a-end` is created. The engine must support all three of these with no special-case code:

1. **Continue:** Campaign B branches from `campaign-a-end`, same player adopts their old PC, plays the aftermath as the person responsible.
2. **New life:** Campaign C branches from the same marker; a different player creates a farmer PC scraping by in Vel's ruined hinterland. The Narration service describes the crater because `PlaceDestroyed(Vel)` is simply true on this branch.
3. **What-if:** Campaign D branches from a commit *before* the meteor decision and goes differently. Branches A′ and D coexist; neither contaminates the other.

This scenario is the roadmap Phase 2 acceptance test (`10-roadmap.md`).

## History adaptation across the fork

Physical/political changes must propagate into *future generation*, not just sit in the log (owner feedback on both World and History engines). Two distinct writes here, easy to conflate:

1. **The direct consequence** of a player action — the meteor itself — is emitted by the History service as a **thread's consequence-on-resolution** (`02`, `12`): when the Saltborn ritual thread resolves, History commits `TerrainChanged`/`PlaceDestroyed(Vel)` with `caused_by=player_action`. This is the mid-play emitter path the meteor needs; it is *not* the extractor or pipeline (barred by the `12` whitelist), and it happens *during* Campaign A, before the marker.
2. **The ripple** — at fork/continue time and after such major in-play events, the History service runs an **adaptation pass** (`AdaptationApplied`, `caused_by=history, pass=adaptation`): query claims/threads invalidated or newly implied ("Vel's market festival thread → dead"; "refugee crisis thread → spawned"), commit the adjustments, scoped to ~2 edge-hops (OQ-8).

Generation then just reads current state; it doesn't need to know anything "changed."

## Replayability with different seeds

A world pack + a seed deterministically drives procedural steps (history seeding choices, RNG). Same pack, different seed → sibling world with different dynasties on the same geography. This is *world-level* replay, orthogonal to branch-level forking.

## Storage sketch

See `07-persistence-and-events.md` for DDL. Core tables: `events` (append-only), `commits`, `branches`, `markers`, `snapshots`, plus projection tables. Hash-chaining commits (`commit_hash = h(parent_hash, events)`) gives integrity cheaply and makes export packs verifiable (world packs to genesis; snapshot-rooted branch packs from their trust-anchored root forward — `07`). Fork cost = a branch ref + a copy-on-fork projection build from the nearest snapshot (`07`); embedding vectors are shared by content hash and never recomputed.
