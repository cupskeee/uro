# 12 — Event Catalog

The canonical registry of domain event types. Everything the engine "knows" is a projection of these (`03-timeline-and-branching.md`, `07-persistence-and-events.md`); this file is the vocabulary. **Version 0 — living**, but with process: an event type does not exist until it has (1) an entry here, (2) a Pydantic payload model in `uro_core/domain/events.py`, (3) projector handling, and (4) an emitter whitelist entry. No inline invention of event types in code.

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

`caused_by` variants: `{kind: player_action, participant_id, beat_id}` · `{kind: agenda, actor_id, thread_id?}` · `{kind: history, pass: seeding|adaptation|backfill|timeskip}` · `{kind: ruleset, encounter_id}` · `{kind: system}`.

## Emitter whitelist

Each event type declares who may emit it — this is a hard validation rule at commit time and a core hallucination defense: the LLM-fed extractor can only ever *propose* types marked **X**; mechanical outcomes only ever come from the ruleset.

Emitters: **X** = extractor (LLM-proposed, validated) · **R** = ruleset effects · **H** = History service (seeding, adaptation, and mid-play thread consequences) · **A** = Actor service (off-screen sim) · **P** = pipeline core · **S** = system/API (import, seeding, admin, fork ops) · **E** = external resolver (Chronicler mode, D-25 — the encounter it was handed: the opening handoff/parking event, then the mechanical facts reported back; interpretive content distilled then gauntlet-validated).

## Catalog v0

### World & places
| event_type | payload (beyond `v`) | emit | notes |
|---|---|---|---|
| `WorldGenesis` | manifest snapshot, pack hash, seed | S | first commit of every world |
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
| `ActorDamaged` | actor_id, amount, source, trace | R E | mechanical only — never extractor |
| `ActorDied` | actor_id, cause | R H A E | |
| `SheetUpdated` | actor_id, ruleset_id, sheet | R S | ruleset-owned character sheet; the PoC records the whole sheet per update (whole-sheet replace), not an incremental patch |
| `PCBound` / `PCReleased` | actor_id, participant_id, campaign_id | S | adopt-as-PC at fork; release at campaign end (retired hero becomes NPC) |

### Identity & resolution
| event_type | payload | emit | notes |
|---|---|---|---|
| `EntityAliasAdded` | entity_id, alias | X P S | "Old Weck" = "Weck the fisher" = "the old fisherman"; resolution matches on name + aliases + embedding before any `*Created` is accepted (`13`) |
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
| `ClaimRecorded` | claim_id, statement, subject_refs, truth, origin | X H P | narration-asserted → `truth=true`; character-asserted → `truth=unknown` (see `05`) |
| `ClaimTruthChanged` | claim_id, truth, cause | P H | investigation resolves `unknown`; contradiction repair |
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
| `ThreadCreated` | thread entity | X H A P | off-screen plots included |
| `ThreadStateChanged` | thread_id, from→to (`dormant|offered|active|resolved|dead`), cause | P H A | |
| `ThreadStepCompleted` | thread_id, step, outcome | P | |

### Play & campaign
| event_type | payload | emit | notes |
|---|---|---|---|
| `CampaignStarted` | campaign_id, branch_id, party[], ruleset@ver, seed | S | |
| `CampaignEnded` | campaign_id, outcome, marker_ref | S | usually paired with a marker |
| `SceneEntered` | place_id, present_actors[], mode | P | |
| `ModeChanged` | from, to (`freeroam|encounter|downtime`), cause | P R | |
| `BeatResolved` | beat_id, participant_id, intent_text, synopsis, narration | P | exactly one per beat commit; the chronicle's raw material |
| `TimeAdvanced` | from, to, reason | P H | downtime / travel / time-skip on fork |

### Encounter (ruleset-driven)
| event_type | payload | emit | notes |
|---|---|---|---|
| `EncounterStarted` | encounter_id, participants[], initiative[] | R E | E: the parking handoff that opens an external-resolver-owned encounter (Uro decides *to* park via `ModeChanged` P/R; `EncounterStarted` marks the boundary where authority passes to E) |
| `EncounterTurnTaken` | encounter_id, actor_id, action, result, trace | R | trace = human-readable roll math for narration; external resolvers report bundles, not turns |
| `EncounterEnded` | encounter_id, outcome | R E | E: outcome bundle — participants, witnesses, casualties, notable feats |

### History
| event_type | payload | emit | notes |
|---|---|---|---|
| `HistorySeeded` | seed, simulated_years, era_summary | H | seeding also emits ordinary Created/Edge events; this is the header |
| `AdaptationApplied` | trigger_refs, scope, summary | H | header for an adaptation pass — at fork/time-skip AND after a major in-play event (OQ-8); ripple events carry `caused_by=history, pass=adaptation` |

## Rules of the catalog

1. **One beat commit contains exactly one `BeatResolved`** plus its side-effect events; simulation/seeding commits contain no `BeatResolved`.
2. **Markers are refs, not events** (`03`) — creating a marker doesn't touch the log.
3. **Probe reports, llm_calls, sessions are not events.** They're operational data (`07`), never world truth.
4. **Payload evolution:** bump `v`, keep old projector paths until a projection rebuild retires them. Never mutate the meaning of an existing `(event_type, v)`.
5. **Extractor ceiling:** X-emitted events are proposals that must survive the full gauntlet — whitelist, schema, tier, entity resolution, provenance & consequence gating, contradiction (`13-contracts.md`). Anything mechanical, lethal, or slow-layer (damage, death, terrain) is structurally impossible for the extractor to emit.
6. **Player text is never evidence.** Proposals ground in generated prose only; `truth=true` additionally requires narrator (not dialogue) provenance and consequence-gating clearance — the trust model in `13-contracts.md`.
7. **External resolvers are authoritative only inside the encounter they were handed.** Their mechanical facts (damage, deaths, transfers) commit directly with `caused_by` tracing the encounter; everything *interpretive* — a feat becoming a claim, a reputation forming — goes through distillation + the gauntlet with **witness-scoped beliefs**. No surviving witnesses, no world knowledge (the truth is recorded; nobody holds it).
