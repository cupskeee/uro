# 13 — Pipeline & Data Contracts

The developer-facing contracts that `05-generation-pipeline.md` (design) and `09-world-definition.md` (prompt packs) promise. **Version 0 — living**, same process discipline as the event catalog: change the contract here and in code in the same commit.

## BeatState

The single object threaded through pipeline stages:

```python
class BeatState(BaseModel):
    # identity
    beat_id: ULID
    campaign_id: ULID
    branch_head: CommitId              # the commit this beat builds on
    dry_run: bool = False
    # input
    intent: Intent                     # {participant_id, pc_actor_id, text}
    mode: Mode                         # freeroam | encounter | downtime
    scene: SceneProjection
    # accumulated by stages
    recall: RecallBundle | None        # [1] structured + semantic + recency (04)
    plan: BeatPlan | None              # [2]
    mechanics: list[CheckResult | Effect] = []   # [3]
    drafts: list[NarrationChunk] = []  # [4] streamed to client as produced
    proposals: list[ProposedEvent] = []# [5]
    result: CommitId | EventDiff | BeatFailure | None   # [6] diff when dry_run
    usage: list[LLMUsage] = []         # every provider call, stage-tagged
```

## Stage protocol

```python
class Stage(Protocol):
    name: str                                       # == stage_tag for metering
    async def run(self, s: BeatState) -> BeatState  # pure w.r.t. world state; ONLY [6] commits
```

Stage graphs are assembled per mode (`05`). Rules:

- **Atomicity:** nothing reaches the event store except through the commit stage, in one transaction with the outbox write. A failed beat commits nothing — streamed prose the player already saw simply never became canon (client marks the beat failed/retryable).
- **Concurrency:** at most **one in-flight beat per campaign**, enforced by the engine after `TurnArbiter` admission (`08`). Different campaigns/branches are fully concurrent.
- **Plan validation (pre-narration, deterministic, no LLM):** between [2] and [3] the plan is checked against state and ruleset: referenced targets must exist, presupposed facts must not be `false`, and if the intent matches a ruleset-declared **trigger category** the plan must invoke that affordance (D-21). Violations re-ask the planner. This is the *only* point where replanning happens — nothing has streamed yet.
- **Post-narration validation is downgrade-only:** problems found at [5] can only downgrade a proposal (claim → belief) or drop it with a logged warning. No replanning, no prose regeneration after streaming begins — the player has already read it.
- **Time:** the planner proposes `time_cost` (in segments); the commit stage emits `TimeAdvanced` when > 0.

## Trust model (who gets to make things true)

Canon corruption via prompt injection is a first-class threat: player text feeds the narrator, narrator prose feeds the extractor, and the extractor writes state. The evidence hierarchy:

| Tier | Source | May become |
|---|---|---|
| 0 | Engine state, ruleset results, world-pack seeds | Authoritative; only events change it |
| 1 | External resolver telemetry (Chronicler mode outcome bundles, D-25) | Mechanical facts commit directly *within the encounter's declared domain*; interpretations (feats → claims, reputations) are distilled, then gauntlet-validated with witness-scoped beliefs |
| 2 | Narrator output (scene/outcome prose) | `truth=true` claims — subject to the gauntlet below |
| 3 | Dialogue output (any character speaking) | Testimony: `ClaimRecorded truth=unknown` + `BeliefChanged` for speaker/listeners |
| 4 | Player intent text / PC speech | **Never evidence.** |

Enforcement:

- **Raw player text never reaches the extractor.** The extractor's context is generated prose + plan + mechanics results; player intent enters only as the planner's structured paraphrase (`intent_class`, targets).
- **PC assertions are testimony**, exactly like NPC dialogue: "I tell them I'm the rightful king" may yield a claim at `truth=unknown` plus listener beliefs sourced to the PC — never `truth=true`.
- **Provenance check:** a `truth=true` proposal must carry an evidence span from *narrator* output (not dialogue, not intent) and must not merely restate something a character asserted this beat.
- **Consequence gating (D-21):** proposals touching protected state — item transfers into the party's possession, `truth=true` claims, faction-level edges, changes to T2+ actors — require an attached supporting `CheckResult`/`Effect` or an existing-state basis, else downgrade.
- The narrator itself is injectable (players can say things engineered to steer it); provenance + consequence gating are why that only ever buys flavor, never canon.

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
  "mechanics": [{"affordance": "persuade", "actor": "...", "target": "...", "context": "..."}],
  "mode_transition": null,                                  // or {"to": "encounter", "cause": "..."}
  "thread_hooks": [{"thread": "...", "development": "..."}],
  "time_cost": 0,                                           // segments
  "narration_directives": "one line of pacing/tone guidance for [4]",
  "suggestions": ["2-4 short next-action hints, affordance-grounded — returned with the beat result (D-23)"]
}
```

`mechanics[].affordance` must name an affordance the bound ruleset declared (`06`) — unknown affordances fail validation, which is how the planner is fenced into the ruleset's vocabulary. Conversely, if the intent matches an affordance's declared **trigger category**, the plan must invoke it — enforced deterministically at plan validation (D-21). The rules cannot be bypassed by phrasing.

## ProposedEvent v0 (extractor output)

```jsonc
{
  "event_type": "ClaimRecorded",          // must be X-whitelisted (12-event-catalog.md)
  "payload": { ... },                     // must validate against that type's payload model
  "confidence": 0.0-1.0,                  // < 0.5 → dropped, logged
  "evidence": "verbatim span from the GENERATED prose that grounds this (never player text)",
  "resolution": { "ref": "actor:..." }    // *Created only: matched existing entity, or {"new": true}
}
```

Validation gauntlet, in order: emitter whitelist → payload schema → tier ceiling (ActorCreated ≤ T1, PlaceCreated = Site only) → **entity resolution** (`*Created` proposals matched against existing entities by name, alias, and embedding; a plausible match links instead of creating — the anti-duplicate contract, with `EntityMerged` (`12`) as the after-the-fact correction) → **provenance & consequence gating** (trust model above) → contradiction check against `truth=true` claims and hard state. Everything past this point is downgrade-or-drop, never replan.

## Prompt template contracts

Every template receives exactly these context objects (Jinja2, strict-undefined so a missing variable fails loudly). Packs override bodies, never contracts (`09`). `template_api_version: 0`.

| Template | Receives |
|---|---|
| `narrator.style.j2` | `world` {name, tone[]}, `scene` {place, present[], mode}, `plan`, `mechanics[]` (incl. trace strings), `recall`, `recent_beats[]` |
| `dialogue.style.j2` | `world`, `actor` {profile, tier}, `beliefs_in_scope[]`, `relationships` (to party & present), `plan` (speaker slice), `recent_beats[]` |
| `planner.hints.j2` | `world`, `scene`, `affordances[]` (from ruleset), `active_threads[]`, `recall` — appended to the planner system prompt |
| `extractor.hints.j2` | whitelist summary, world naming conventions — appended to extractor system prompt |
| `summarizer.style.j2` | beats-to-compress, target length, `world.tone` |

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
| Commit conflict (branch head moved) | [6] | impossible by concurrency rule; assert & fail loudly |

A failed beat returns `BeatFailure {stage, reason, retryable}` over the API; the CLI renders it and offers retry (which is a brand-new beat — there is no partial-beat resume).
