# 05 — Generation Pipeline

> **Living document** (owner feedback: "not a fixed flow"). The stage *contracts* are stable; the stage *graph* is expected to be rearranged, split, and tuned throughout development. See OQ-2.

## Beats, not turns

Play advances in **beats**: one player intent, resolved. In **free-roam** there is no turn structure — the player acts whenever, like at a real table. Structured turns exist only inside **encounter mode** (combat/initiative), where the bound ruleset owns the loop. Modes:

| Mode | Cadence | Owner |
|---|---|---|
| `freeroam` | beat per player intent | pipeline |
| `encounter` | initiative-ordered turns; each turn is a constrained beat | ruleset state machine, pipeline renders |
| `downtime` | time-skip ("a month passes"); off-screen simulation commits | History + Actor services |

Mode transitions are themselves pipeline decisions (the mechanics gate detects "this action starts combat") and are recorded as events.

## The beat pipeline

```
player intent
   │
   ▼
[1] CONTEXT ASSEMBLY        structured recall + semantic recall + recency window
   │                        (see 04-llm-integration.md)
   ▼
[2] PLAN (planner role)     classify intent; decide beat structure: outcome shape,
   │                        which actors react, whether mechanics are implicated,
   │                        whether a thread advances. Structured output, always.
   ▼
[3] MECHANICS GATE          if the plan implicates rules (persuade → check;
   │  (ruleset, no LLM)     attack → encounter mode; travel → time cost):
   │                        ruleset resolves rolls/outcomes deterministically
   │                        with seeded RNG. Results become inputs to narration.
   ▼
[4] GENERATE                specialized calls per plan: scene dressing (on entry),
   │  (narrator/dialogue)   NPC dialogue (per speaking actor, voiced via their
   │                        profile), outcome prose weaving mechanics results in.
   ▼
[5] EXTRACT & CANONICALIZE  extractor role parses generated prose for state-worthy
   │                        content → proposed events (new claims, new T1 actors,
   │                        belief changes, item transfers). Promotion rules below.
   ▼
[6] COMMIT                  validated events batched into one beat commit;
                            projections update; response streams to client.
```

Stages are composable units behind a common interface (`async run(BeatState) -> BeatState`; full contract, schemas, and failure semantics in `13-contracts.md`), assembled into a graph per mode — encounter beats skip scene dressing; downtime beats replace [2]–[4] with simulation passes. Between [2] and [3] sits **plan validation** — deterministic, no LLM: targets must exist, presupposed facts must not be false, ruleset trigger categories must be honored (D-21) — with a planner re-ask on failure; it is the only point where replanning is possible, because nothing has streamed yet. *(Phase-3 status: the affordance fence + D-21 trigger coverage + actor-ref existence are enforced; place/item existence and the presupposed-facts check are deferred.)* Streaming: [4] streams to the client while [5] runs on the buffered output, so perceived latency ≈ narration latency.

[4] is itself a small fan-out: scene dressing (on entry), one dialogue call per speaking actor, and outcome prose. These sub-calls are **mutually independent** — each renders solely from `BeatState` (plan, mechanics, recall, recent_beats via the strict-undefined template contracts in `13`), never from a sibling call's output — which is exactly why multi-speaker dialogue calls run concurrently (`13` latency budget) and each chunk can stream as produced. The client-visible interleaving of concurrent chunks is a rendering choice, pinned when multi-speaker beats are built (Phase 3-era), not now (`05` is "not a fixed flow", OQ-2).

## Promotion rules (ephemeral vs. canonical)

The report's key insight, kept and sharpened. Generated prose constantly invents things; only some become world truth:

- **Named entity introduced** ("Eorl, the bandit lord") → `ActorCreated` at T1.
- **Player interacts with an extra repeatedly / pins them** → `ActorPromoted` T0→T1→T2. Any random NPC can become important (owner requirement); importance is *behavior-driven*, never pre-authored.
- **Claim asserted by narration as true** ("the cellar door is locked") → `ClaimRecorded`, `truth=true`.
- **Claim asserted by a character in dialogue** → `ClaimRecorded` with `truth=unknown` + `BeliefChanged` for the speaker — NPCs can lie without corrupting world truth.
- **Claim asserted by the player or their PC** → testimony, exactly like NPC dialogue: `truth=unknown` + listener beliefs, never `truth=true`. Raw player text never reaches the extractor at all (trust model, `13-contracts.md`).
- **Pure flavor** (weather color, incidental gestures) → not canonicalized; survives only in the beat log.

The extractor proposes; the gauntlet (`13-contracts.md`) disposes: whitelist, schema, tier ceilings, **entity resolution** (a newly named "Old Weck" is first matched against existing actors by name/alias — the embedding leg is planned — plausible matches link instead of spawning duplicates), **provenance & consequence gating** (player text is never evidence; protected-state changes need mechanics backing), then contradiction checks — all strictly downgrade-or-drop, since narration has already streamed. This is the hallucination *and* injection defense: nothing becomes fact without surviving [5]+[6], and nobody can talk the world into truth. What the engine does *not* do is judge content acceptability — that's provider/platform territory (`00-vision.md`).

## Off-screen simulation (simulate-on-observation)

When a place/actor is observed after a gap, the Actor service resolves what T3 agents' agendas produced in the interim (structured, cheap: agenda + elapsed time + world events → outcome events), commits it **as a separate, preceding commit** (`03`) — the beat then re-anchors `branch_head` onto it (`13`) and proceeds against updated state. "The blacksmith's daughter has been missing since last season" costs nothing until someone walks into the smithy. Depth/cadence is OQ-4.

## Dry-run mode (the "testing sandbox")

Any beat (or seeding run) can execute with `commit=false`: full pipeline, streamed output, and a **diff of would-be events** instead of a commit. Uses: creators testing how the AI plays their lore before anyone plays it; engine development (inspect stage IO); regression testing (recorded-response replay through changed pipeline code). Exposed as `uro dry-run` and via API flag.

## API sketch

Transport detail lives in `08-api-and-sessions.md`; the pipeline surfaces as:

- `POST /campaigns/{id}/beats` — submit intent `{actor_id, text}` → streamed narration + `beat_commit_id`.
- `POST /campaigns/{id}/beats?dry_run=true` — same, plus `proposed_events[]`, no commit.
- `GET /campaigns/{id}/scene` — current scene projection (place, present actors, mode, hooks).
