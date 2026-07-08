# 13 — Pipeline & Data Contracts

The developer-facing contracts that `05-generation-pipeline.md` (design) and `09-world-definition.md` (prompt packs) promise. **Version 0 — living**, same process discipline as the event catalog: change the contract here and in code in the same commit.

## BeatState

The single object threaded through pipeline stages:

```python
class BeatState(BaseModel):
    # identity
    beat_id: ULID
    campaign_id: ULID
    branch_head: CommitId              # the commit this beat builds on; RE-ANCHORS if an
                                       #   off-screen-sim sub-commit lands mid-beat (see below)
    dry_run: bool = False
    # input
    intent: Intent                     # {participant_id, pc_actor_id, text}
    mode: Mode                         # freeroam | encounter | downtime
    scene: SceneProjection
    # accumulated by stages
    recall: RecallBundle | None        # [1] structured + semantic + recency (04)
    plan: BeatPlan | None              # [2]
    mechanics: list[CheckResult | Effect] = []   # [3] CheckResult.outcome is a ruleset-graded
                                       #   band (D-30): d20 {failure,success}; PbtA {miss,partial,full}
    drafts: list[NarrationChunk] = []  # [4] streamed to client as produced
    proposals: list[ProposedEvent] = []# [5]
    result: CommitId | EventDiff | BeatFailure | None   # [6] diff when dry_run
    usage: list[LLMUsage] = []         # every provider call, stage-tagged
```

## Stage protocol

```python
class Stage(Protocol):
    name: str                                       # == stage_tag for metering
    async def run(self, s: BeatState) -> BeatState  # no stage commits the beat's own events except [6]
```

Stage graphs are assembled per mode (`05`). Rules:

- **Atomicity:** the beat's *own* events reach the store only through the commit stage [6], in one transaction with the outbox write. A failed beat commits nothing of its own — streamed prose the player already saw simply never became canon (client marks the beat failed/retryable). One carve-out: the Actor service's simulate-on-observation may write a **separate, preceding** commit mid-beat (`05`, `03`) — atomic in its own right, not the beat's events.
- **Concurrency (DESIGN; the enforcement is NOT built yet):** the intended rule is at most **one in-flight beat per campaign** after `TurnArbiter` admission (`08`), different campaigns/branches fully concurrent. *PoC status:* `append_beat` does NOT check an expected head or serialize per-campaign — single-writer safety currently rests on convention (the CLI/embedded loop is serial; the server serializes a connection's own beats but not across connections). An expected-head guard + per-campaign in-flight lock is the intended hardening.
- **Plan validation (pre-narration, deterministic, no LLM):** between [2] and [3] the plan is checked against state and ruleset: referenced targets must exist, presupposed facts must not be `false`, and if the intent matches a ruleset-declared **trigger category** the plan must invoke that affordance (D-21). Violations re-ask the planner. This is the *only* point where replanning happens — nothing has streamed yet. *(Phase-3 status: the affordance-vocabulary fence + D-21 trigger coverage + **actor**-ref existence are enforced; place/item existence and the presupposed-facts check are deferred — there is no place/item registry yet.)*
- **Post-narration validation is downgrade-only:** problems found at [5] can only downgrade a proposal (claim → belief) or drop it with a logged warning. No replanning, no prose regeneration after streaming begins — the player has already read it.
- **Time:** the planner proposes `time_cost` (in segments); the commit stage emits `TimeAdvanced` when > 0.

## Trust model (who gets to make things true)

Canon corruption via prompt injection is a first-class threat: player text feeds the narrator, narrator prose feeds the extractor, and the extractor writes state. The evidence hierarchy:

| Tier | Source | May become |
|---|---|---|
| 0 | Engine state, ruleset results, world-pack seeds | Authoritative; only events change it |
| 1 | External resolver telemetry (Chronicler mode outcome bundles, D-25) | Mechanical facts (deaths, transfers) commit directly *within the encounter's declared domain*; interpretations (feats → claims) commit as **`truth=unknown` testimony** + witness-scoped beliefs, never protected canon. *PoC: the extractor gauntlet is not yet applied to feat testimony (OQ-12)* |
| 2 | Narrator output (scene/outcome prose) | `truth=true` claims — subject to the gauntlet below |
| 3 | Dialogue output (any character speaking) | Testimony: `ClaimRecorded truth=unknown` + `BeliefChanged` for speaker/listeners |
| 4 | Player intent text / PC speech | **Never evidence.** |

Enforcement:

- **Raw player text never reaches the extractor.** The extractor's context is generated prose + plan + mechanics results; player intent enters only as the planner's structured paraphrase (`intent_class`, targets).
- **PC assertions are testimony**, exactly like NPC dialogue: "I tell them I'm the rightful king" may yield a claim at `truth=unknown` plus listener beliefs sourced to the PC — never `truth=true`.
- **Provenance check:** a `truth=true` proposal must carry an evidence span from *narrator* output (not dialogue, not intent) and must not merely restate something a character asserted this beat.
- **Consequence gating (D-21) — DESIGN; NOT BUILT YET (Phase-3 status):** proposals touching protected state — item transfers into the party's possession, `truth=true` claims, faction-level edges, changes to T2+ actors — are *meant to* require an attached supporting `CheckResult`/`Effect` or an existing-state basis, else downgrade. The shipped gauntlet does NOT do this check (`extraction.py` — whitelist/schema/tier/entity-resolution/provenance/contradiction only); a `truth=true` claim currently rests on the extractor's narrator-provenance label alone. Until this ships, treat protected-canon integrity as best-effort, not a hard boundary.
- The narrator itself is injectable (players can say things engineered to steer it); provenance + consequence gating are why that buys flavor and at most low-stakes, ungated state — never *protected* canon (`truth=true`, party loot, faction edges, T2+ actors). The bounded residue is honest: a phrasing that nudges a T1 extra's disposition can commit as an ordinary `ActorStateChanged`, i.e. low-stakes canon, because the trigger list has gaps (D-21) and gating only guards protected state. That is an accepted leak, not a hole the docs pretend closed.

## Structured output policy

Roles `planner` and `extractor` **must** return schema-valid JSON: schema goes in the request (adapter-native structured output when available, prompt-embedded otherwise); responses validate against the Pydantic model; up to **2 re-asks** with the validation error attached.

- Planner exhausts re-asks → **beat fails** (a beat without a plan is unrunnable).
- Extractor exhausts re-asks → beat commits with `BeatResolved` only, flavor-uncanonicalized, warning logged. Story continues; state integrity is never sacrificed to keep prose.

Default sampling per role (deployment-overridable): narrator 0.9 · dialogue 0.8 · planner 0.2 · extractor 0.1 · summarizer 0.3. Seeds pass through when the provider supports them.

## BeatPlan v0 (planner output)

```jsonc
{
  "intent_class": "dialogue | action | movement | examine | meta",
  "targets": ["actor:...", "place:...", "item:..."],       // entity refs on stage
  "speakers": ["actor:..."],                               // who gets a dialogue call in [4]
  "triggers": ["change_disposition", ...],                 // trigger categories the intent hits;
                                                           //   EACH must be invoked by a mechanic
                                                           //   below — deterministic D-21 check
  "mechanics": [{"affordance": "persuade", "actor": "...", "target": "...", "context": "..."}],
  "mode_transition": null,                                  // or {"to": "encounter", "cause": "..."}
  "time_cost": 0,                                           // segments
  "narration_directives": "one line of pacing/tone guidance for [4]",
  "suggestions": ["2-4 short next-action hints, affordance-grounded — returned with the beat result (D-23)"]
}
```

`mechanics[].affordance` must name an affordance the bound ruleset declared (`06`) — unknown affordances fail validation, which is how the planner is fenced into the ruleset's vocabulary. Conversely, if the intent matches an affordance's declared **trigger category**, the plan must invoke it — enforced deterministically at plan validation (D-21). Caveat on the guarantee: plan validation is deterministic and sees only the planner's *own* structured paraphrase (`intent_class`/`targets`), not raw intent — so a misclassified action can dodge a trigger. Consequence gating is the independent backstop, but it only guards **protected state**. Net: *high-stakes* rules can't be bypassed by phrasing; low-stakes state the trigger list misses is a bounded, accepted leak (Trust model above, D-21) — not flavor, but not protected canon either.

## ProposedEvent v0 (extractor output)

```jsonc
{
  "event_type": "ClaimRecorded",          // must be X-whitelisted (12-event-catalog.md)
  "payload": { ... },                     // must validate against that type's payload model
  "confidence": 0.0-1.0,                  // extractor's STATE-WORTHINESS confidence (drop gate); < 0.5 → dropped.
                                          //   Distinct from any payload confidence — e.g. a BeliefChanged
                                          //   payload's belief-strength, which is never gated by this field.
  "evidence": "verbatim span from the GENERATED prose that grounds this (never player text)",
  "resolution": { "ref": "actor:..." }    // *Created only: matched existing entity, or {"new": true}
}
```

Validation gauntlet, in order: emitter whitelist → payload schema → tier ceiling (ActorCreated ≤ T1, PlaceCreated = Site only) → **entity resolution** (`*Created` proposals matched against existing entities by name and alias — the embedding leg is designed but not built yet, see below; a plausible match links instead of creating — the anti-duplicate contract, with `EntityMerged` (`12`) as the after-the-fact correction) → **provenance** (narrator → `truth=true`; dialogue → testimony) — **consequence gating is designed but NOT built yet** (trust model above) → contradiction check against `truth=true` claims and hard state. Everything past this point is downgrade-or-drop, never replan.

**Planned — not built yet; the shipped gauntlet resolves on name/alias only.** The embedding leg of entity resolution matches against a **dedicated entity index** — the embedded name/aliases/one-line descriptor of every existing entity — NOT the memory-recall corpus (`04`, `07`). The candidate's name is embedded on the fly and kNN'd against that index; only after the proposal is accepted is the new entity added to it. This is what lets resolution dedup even a world-pack-seeded entity that has never been narrated (so has zero memory chunks). Maintained by a side-effecting projector on `ActorCreated`/`PlaceCreated`/`FactionCreated`/`EntityAliasAdded`; forking copies the membership rows and never re-embeds (mirrors `memory_index`, `07`).

## Prompt template contracts

The INTENDED per-template context contract (Jinja2, `StrictUndefined` — shipped — so a template referencing an uninjected/misnamed variable fails loudly). Packs override bodies, never contracts (`09`). The `TEMPLATE_API_VERSION` constant is the intended pin anchor but is **reserved, not yet enforced** (no manifest version field, no check).

| Template (intended) | Receives (intended) |
|---|---|
| `narrator.style.j2` | `world` {name, tone[]}, `scene` {place, present[], mode}, `plan`, `mechanics[]` (incl. trace strings), `recall`, `recent_beats[]` |
| `dialogue.style.j2` | `world`, `actor` {profile, tier}, `beliefs_in_scope[]`, `relationships` (to party & present), `plan` (speaker slice), `recent_beats[]` |
| `planner.hints.j2` | `world`, `scene`, `affordances[]` (from ruleset), `active_threads[]`, `recall` — appended to the planner system prompt |
| `extractor.hints.j2` | whitelist summary, world naming conventions — appended to extractor system prompt |
| `summarizer.style.j2` | beats-to-compress, target length, `world.tone` |

**Shipped (Phase 4):** the default templates are `narrator.system.j2` (receives `style` = the world's tone string), `planner.system.j2`, and `extractor.system.j2` (no injected variables yet). A pack overrides one by shipping a file of the **same name** under `prompts/`. The richer per-template context above is the target the contract grows into.

## Provider request contract

`CompletionRequest` = messages + `role` + `stage_tag` + optional `schema` + sampling params + optional seed. Every response records to `llm_calls` (`07`): prompt hash, model, tokens in/out, latency, stage_tag — powering `GET /usage`, recorded-response replay, and the test fixtures in `14-development-guide.md`.

## Latency budget

Placeholder targets (see `11` placeholder constants) — a design constraint, not an SLA, but a beat that blows it 2× is a bug, not a shrug: **p50 first narration token < 2s · p50 beat committed < 10s · p95 < 20s · context assembly < 300ms.** Budget-driven design consequences: dialogue calls for multiple speakers run concurrently; extraction overlaps narration streaming; suggestions come out of the planner call, never a separate one; recall queries are indexed, never scanned. Stage-tagged metering (`07` `llm_calls`) is how the budget gets measured from Phase 0.

## Failure taxonomy

| Failure | Where | Behavior |
|---|---|---|
| Provider error/timeout | any LLM stage | adapter retries (expo backoff ×3) → beat fails, retryable |
| Schema invalid after re-asks | [2] | beat fails | 
| Schema invalid after re-asks | [5] | commit flavor-only, warn |
| Contradiction unresolved | [5] | downgrade/drop proposal, warn |
| Ruleset exception | [3] | beat fails (ruleset bugs must be loud, never narrated around) |
| Commit conflict (branch head moved by *another* writer) | [6] | *intended:* prevented by the concurrency rule, else assert & fail loudly — **NOT built yet** (no expected-head check; see Concurrency). The beat's *own* off-screen-sim sub-commit is expected head movement, not a conflict |

A failed beat returns `BeatFailure {stage, reason, retryable}` over the API; the CLI renders it and offers retry (which is a brand-new beat — there is no partial-beat resume).
