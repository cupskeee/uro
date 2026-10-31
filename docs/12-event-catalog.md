# 12 — Event Catalog

The canonical registry of domain event types. Everything the engine "knows" is a projection of these (`03-timeline-and-branching.md`, `07-persistence-and-events.md`); this file is the vocabulary. **Version 0 — living**, but with process: an event type does not exist until it has (1) an entry here, (2) a Pydantic payload model in `uro_core/domain/events.py`, (3) projector handling, and (4) an emitter whitelist entry. No inline invention of event types in code.

> **Reserved rows (honest status, 2026-07-09; updated 2026-07-10 for D-33):** several catalogued types have full plumbing (payload + projector handler) but **no live emitter yet** — they are the forward contract, not shipped behavior: `ActorPromoted`, `TerrainChanged`, `PlaceStateChanged`, `EdgeUpdated`. (`ClaimTruthChanged` became live in C5/D-34 — `expire_claims` retracts a stale module rumor to `truth=false`.) `ActorDamaged` is **legacy** (no live emitter; a replay-compat handler is retained for pre-D-30 logs — harm now flows via `SheetUpdated`). **NOW LIVE (D-33, Reaction Layer, emitter M=module):** `ThreadStateChanged`, `ClaimRecorded` (module rumors, `origin=module`), `BeliefChanged`/`EdgeAdded`/`EdgeRemoved` (module agendas) — a pack's declarative rules advance thread lifecycle + off-screen agendas, `caused_by=module`. `PlaceDestroyed` + History's own adaptation ripple stay author/test-driven (OQ-8). See [16-honesty-ledger.md](16-honesty-ledger.md) for the proven/reserved map.

## Envelope (every event)

Matches the `events` DDL in `07`:

```
event_id     ULID
commit_id    → commits            # one beat = one commit; ordering via (commit, seq)
seq          int
event_type   str                  # from this catalog
entity_refs  [entity_id]          # every entity the event touches (recall index)
world_time   {day: int, segment: morning|afternoon|evening|night}   # absolute day since world epoch;
                                                                    # world-pack calendar derives years & seasons (D-22).
                                                                    # Named eras are event-driven (History layer), not arithmetic.
caused_by    (below)
payload      {v: 1, ...}          # versioned per event type
```

`caused_by` variants: `{kind: player_action, participant_id, beat_id}` · `{kind: agenda, actor_id, thread_id?}` · `{kind: history, pass: seeding|adaptation|backfill|timeskip}` · `{kind: ruleset, encounter_id}` · `{kind: module, rule_id}` (Reaction Layer, D-33) · `{kind: external, encounter_id}` (Chronicler, D-25) · `{kind: system}`.

## Emitter whitelist

Each event type declares who may emit it — the core hallucination defense: the LLM-fed extractor can only ever *propose* types marked **X**; mechanical outcomes only ever come from the ruleset. **How it's enforced (PoC):** structurally at the source, not by a generic commit-time emitter check. The extractor's output schema (`Extraction`, `pipeline/extraction.py`) can only express actors, places, factions, threads, claims, `member_of`/`located_in` affiliations (D-49/D-50), and the ids of known threads the narration settles (D-52), so `run_gauntlet` emits only `ActorCreated`/`PlaceCreated`/`FactionCreated`/`ThreadCreated`/`EdgeAdded`/`ClaimRecorded`/`BeliefChanged` `ThreadStateChanged` (→ `resolved`, a live EMERGENT thread only), `EntityAliasAdded` (an alias on a KNOWN actor, D-56), and `ClaimTruthChanged` (→ `false`, `cause=superseded`: a fact the NARRATOR established, retired by a narrated change, D-70 — never an authored, mechanical, external or module fact) — the LLM is *incapable* of proposing a mechanical event. The ruleset/pipeline/system emitters are likewise the only code paths that mint their event types. A generic per-commit `caused_by`-vs-whitelist validator is not (yet) a separate gate; this table is the source-of-truth contract those emitters are written to.

Emitters: **X** = extractor (LLM-proposed, validated) · **R** = ruleset effects · **H** = History service (seeding, adaptation, and mid-play thread consequences) · **A** = Actor service (off-screen sim) · **P** = pipeline core · **S** = system/API (import, seeding, admin, fork ops) · **M** = module / Reaction Layer (D-33 — pack-authored declarative rules; a CLOSED action set gauntleted to `truth=unknown`/`origin=module` testimony + thread-state/edge changes, scope-fenced, never canon/mechanics) · **E** = external resolver (Chronicler mode, D-25 — the encounter it was handed: mechanical facts reported back (deaths/loot) commit directly; interpretive content (a feat) commits as `truth=unknown` **testimony** + witness beliefs, NOT as protected canon. *PoC: feat testimony is not yet run through the extractor gauntlet — that tier/contradiction validation is the OQ-12 refinement.*).

## Catalog v0

### World & places
| event_type | payload (beyond `v`) | emit | notes |
|---|---|---|---|
| `WorldGenesis` | world_name, tone[], prompt_overrides{}, ruleset_id, ruleset_version, ruleset_config{}, rule_pack{} | S | first commit of every world; carries the pack's narrator tone + prompt-template overrides (`09`) + its declared ruleset (`06`, D-30 — a campaign started on the world binds it) and that ruleset's settings (`ruleset_config`, D-62 — every ruleset built for the world's campaigns gets them) |
| `PlaceCreated` | place entity, tier of detail | X H S | extractor may create Sites only |
| `PlaceStateChanged` | place_id, changes{} | X H A | population, government ref, economy flavor |
| `TerrainChanged` | place_id, description, effects[] | H R S | the meteor crater; slow-layer physical change. Mid-play, History emits it as a **thread's consequence-on-resolution** (`02`), `caused_by=player_action` — this is how a player-triggered cataclysm is recorded during a campaign (not extractor/pipeline) |
| `PlaceDestroyed` | place_id, cause | H R S | as above — a thread consequence or ruleset effect, not extractor-proposed |

### Actors
| event_type | payload | emit | notes |
|---|---|---|---|
| `ActorCreated` | actor entity, tier | X H A S | extractor creates at T1 max |
| `ActorPromoted` | actor_id, from_tier, to_tier, reason | P S | reason `pinned` = manual (player/API) |
| `ActorMoved` | actor_id, from_place, to_place | X A P | |
| `ActorStateChanged` | actor_id, changes{} | X A H | status, occupation, condition flavor |
| `ActorDamaged` | actor_id, amount, source, trace | — | **LEGACY (D-30):** the pre-Phase-6 d20 runner emitted this per hit (projector reduced hp); the current runner emits harm as the ruleset's opaque final `SheetUpdated` instead. No emitter now; the payload + a replay-compat projector handler are retained only so old d20 logs still rebuild by replay |
| `ActorDied` | actor_id, cause | R H A E | ruleset-agnostic lifecycle trace → `proj_actors.status='dead'` (the authoritative death record; the projector does NOT touch the ruleset sheet, D-30) |
| `CheckResolved` | actor_id, affordance, outcome, trace | R | a free-roam check's outcome in the RULESET's own band (`success`/`failure`, `miss`/`partial`/`full`, `fail`/`success`/`triumph`), one per resolved check, in the beat's commit (`caused_by.kind=ruleset`, `beat_id`), D-64. No projection; a reaction rule can trigger on it (`where: {outcome: …}`; add `per_event: true` to fire once per check rather than once per beat), as in a heat meter |
| `SheetUpdated` | actor_id, ruleset_id, sheet | R S | ruleset-owned OPAQUE character sheet; whole-sheet replace, not an incremental patch. The sole channel mechanical harm reaches projections (D-30) — an hp system, a harm clock, conditions, all the same event. Emitted at an encounter's end (`caused_by.kind=ruleset`, `encounter_id`; the ruleset's concluded sheets, advancement included, D-63), by a free-roam check that changed its actor's sheet (`kind=ruleset`, `beat_id`, D-61), and by downtime when a branch's time skips (`kind=ruleset`, no beat: Uro Pool rest, D-63) |
| `PCBound` / `PCReleased` | actor_id, participant_id, campaign_id | S | adopt-as-PC at fork; release at campaign end (retired hero becomes NPC) |

### Identity & resolution
| event_type | payload | emit | notes |
|---|---|---|---|
| `EntityAliasAdded` | entity_id, alias | X P S | LIVE via X (D-56): when a partial-name mention ("Aldren") resolves to a known actor ("Ser Aldren Voss"), the gauntlet records it as an alias — auditable, and the next exact mention or recall finds the actor. Projector appends to `proj_actors.aliases` (idempotent; a no-op for non-actors). Resolution matches on name + aliases + partial name before any `*Created` is accepted (`13`); the embedding leg was rejected on data |
| `EntityMerged` | survivor_id, merged_id, rationale | P S | never extractor-emitted. Projections re-point through a **merge map**; historical events stay untouched; queries resolve merged ids to survivors. Duplicates *will* slip past resolution — this is the correction path |

### Factions & relations
| event_type | payload | emit | notes |
|---|---|---|---|
| `FactionCreated` | faction entity (incl. kind=religion) | X H S | |
| `FactionStateChanged` | faction_id, changes{} | X H A | goals, resources, territory refs |
| `EdgeAdded` / `EdgeUpdated` / `EdgeRemoved` | src, rel_type, dst, weight, attrs | X H A P S | ALL typed relations (`member_of`, `at_war_with`, `owns`, `knows`, …) — the graph is event-sourced too. `S` covers import: authored/cross-linked world-pack relations become `EdgeAdded` at `WorldGenesis` (`09`) |

### Claims & beliefs (epistemic layer)
| event_type | payload | emit | notes |
|---|---|---|---|
| `ClaimRecorded` | claim_id, statement, subject_refs, truth, origin, created_day | X H P S E M | narration-asserted → `truth=true`; character-asserted → `truth=unknown` (see `05`); `S` covers authored pack claims at import (`09`); `E` covers a Chronicler feat, committed as `truth=unknown` **testimony** (never protected canon), `origin=external` (`chronicler.py`, D-25); `M` covers module rumors (`origin=module`); `created_day` is the in-fiction birth day for `expire_claims` (C5, D-34) |
| `ClaimTruthChanged` | claim_id, truth, cause | P H M X | investigation resolves `unknown`; contradiction repair; `X` covers a narrated CHANGE of state retiring the narrator fact it supersedes to `truth=false`, `cause=superseded` (D-70 — never an authored, mechanical, external or module fact); `M` covers `expire_claims` retracting a stale MODULE rumor to `truth=false` (C5, D-34 — structurally never a `truth=true`/canon claim) |
| `BeliefChanged` | actor_id, claim_id, confidence, learned_from | X A P | rumor spread = BeliefChanged fan-out |

### Items
| event_type | payload | emit | notes |
|---|---|---|---|
| `ItemCreated` | item entity | X H R S | |
| `ItemTransferred` | item_id, from_ref, to_ref, means | X R P E | |
| `ItemStateChanged` | item_id, changes{} (incl. destroyed) | X R | |

### Threads
| event_type | payload | emit | notes |
|---|---|---|---|
| `ThreadCreated` | thread_id, stakes, state, originator, provenance | X H A P S | off-screen plots included; `S` covers authored + AI-backfilled pack conflict-seeds at import (`provenance=author\|ai_backfill`, `09`); projected to `proj_threads` |
| `ThreadStateChanged` | thread_id, to_state (`dormant|offered|active|resolved|dead`), from_state (advisory) | X M P H A | LIVE via emitter M (D-33): a pack rule advances a thread's lifecycle; and via X (D-52): the extractor moves a live (`active`/`offered`) EMERGENT thread to `resolved` when the narration settles it — never an authored thread, never another state. Projector sets `proj_threads.state` (never mints a thread) |
| `ThreadStepCompleted` | thread_id, step, outcome | P | reserved (no live emitter) |

### Computation (numeric state)
| event_type | payload | emit | notes |
|---|---|---|---|
| `CounterChanged` | scope_ref, key, to_value (int, ABSOLUTE), created_day, updated_day | M | LIVE via emitter M (D-34, Computation Layer): a pack rule sets a bounded integer counter. Projector UPSERTs `proj_counters` (preserves created_day). Structurally non-canon (writes only `proj_counters`); scope-fenced, magnitude-capped. Triggerable since C6 (D-54): a rule on `CounterChanged` runs in the bounded reaction cascade after a module pass (at most `_MAX_CASCADE_HOPS` hops; only CounterChanged-triggered rules take part, so pre-C6 packs are unchanged). |

### Play & campaign
| event_type | payload | emit | notes |
|---|---|---|---|
| `CampaignStarted` | campaign_id, branch_id, party[], ruleset_id, ruleset_version, seed | S | pins the governing ruleset id **and version** (D-30) so a later play/fork rebinds the same ruleset via the registry (`06`) |
| `CampaignEnded` | campaign_id, outcome, marker_ref | S | usually paired with a marker |
| `SceneEntered` | place_id, present_actors[], mode | P | |
| `ModeChanged` | from, to (`freeroam|encounter|downtime`), cause | P R | |
| `BeatResolved` | beat_id, participant_id, intent_text, synopsis, narration | P | exactly one per beat commit; the chronicle's raw material |
| `TimeAdvanced` | from, to, reason | P H | downtime / travel / `time-skip` (in place) / `time-skip on fork` |

### Encounter (ruleset-driven)
| event_type | payload | emit | notes |
|---|---|---|---|
| `EncounterStarted` | encounter_id, participants[] | R E | no `initiative` field (removed D-30 — turn ordering is ruleset-internal; d20 has initiative, PbtA does not). E: the parking handoff that opens an external-resolver-owned encounter (Uro decides *to* park via `ModeChanged` P/R; `EncounterStarted` marks the boundary where authority passes to E) |
| `EncounterTurnTaken` | encounter_id, actor_id, action, result, trace | R | trace = human-readable roll math for narration; external resolvers report bundles, not turns |
| `EncounterEnded` | encounter_id, outcome | R E | E: outcome bundle — participants, witnesses, casualties, notable feats |

### History
| event_type | payload | emit | notes |
|---|---|---|---|
| `HistorySeeded` | seed, simulated_years, era_summary | H | seeding also emits ordinary Created/Edge events; this is the header |
| `AdaptationApplied` | trigger_refs, scope (`timeskip` in place, `fork-timeskip` on a fork), summary | H | header for an adaptation pass — at fork/time-skip AND after a major in-play event (OQ-8); ripple events carry `caused_by=history, pass=adaptation` |

## Rules of the catalog

1. **One beat commit contains exactly one `BeatResolved`** plus its side-effect events; simulation/seeding commits contain no `BeatResolved`.
2. **Markers are refs, not events** (`03`) — creating a marker doesn't touch the log.
3. **Probe reports, llm_calls, sessions, and outbox rows are not events.** They're operational data (`07`), never world truth — an outbox row (D-57) only points at a commit whose events it publishes.
4. **Payload evolution:** bump `v`, keep old projector paths until a projection rebuild retires them. Never mutate the meaning of an existing `(event_type, v)`.
5. **Extractor ceiling:** X-emitted events are proposals that must survive the full gauntlet — whitelist, schema, tier, entity resolution, provenance & consequence gating, contradiction (`13-contracts.md`). Anything mechanical, lethal, or slow-layer (damage, death, terrain) is structurally impossible for the extractor to emit.
6. **Player text is never evidence.** Proposals ground in generated prose only; `truth=true` additionally requires narrator (not dialogue) provenance and consequence-gating clearance — the trust model in `13-contracts.md`.
7. **External resolvers are authoritative only inside the encounter they were handed.** Their mechanical facts (deaths, transfers) commit directly with `caused_by` tracing the encounter; everything *interpretive* — a feat becoming a claim — commits as **`truth=unknown` testimony** with **witness-scoped beliefs** (an external bundle cannot assert protected canon). No surviving witnesses, no world knowledge (the testimony is recorded; nobody holds it). *PoC: the testimony is not yet re-validated through the extractor gauntlet (OQ-12); the scope guarantee it currently enforces is "external prose is never `truth=true`".*
