# Communicating with Uro — the integration reference

> This is the **verified** engine surface (matched to the code, 2026-07-11). If your game needs a
> capability not listed here, it very likely **does not exist** — do not invent an API; log it in
> your GAP REPORT (that is the whole point of this exercise). The runnable reference consumer is
> [`examples/hello_uro/hello_uro.py`](../hello_uro/hello_uro.py) — read it first.

Uro is a **headless, embeddable, game-agnostic** engine. State is an append-only log of typed
events; read-models ("projections") are rebuilt from it; branches fork the whole world. There are
**two integration postures** — pick the one your game's TASK.md specifies.

---

## Prerequisites (both postures)

```sh
docker compose up -d --wait        # Postgres + pgvector on HOST PORT 5433 (not 5432)
uv run uro db migrate              # apply migrations
```
DSN: `postgresql://uro:uro@localhost:5433/uro`. **CI/anyone must be able to run your game with the
deterministic `stub` provider — no API key.** A real model is opt-in (`openai`/`anthropic`, needs a
key in env). Never commit keys.

---

## Posture A — Embed `uro_core` as a library (in-process, Python)

The whole engine is a Python package. This is how a Python game drives Uro directly. Everything is
`async`.

```python
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.pipeline.engine import Engine
from uro_core.providers.router import ProviderRouter
from uro_core.providers.adapters.stub import StubProvider

store = PostgresEventStore("postgresql://uro:uro@localhost:5433/uro")
await store.connect(); await store.migrate()
engine = Engine(store, ProviderRouter(bindings={}, default=StubProvider()))  # no key
# ... use the API below ...
await store.close()
```

### Provider (the LLM seam) — 3 async methods
A provider is `stream(req)->AsyncIterator[str]` (narration), `complete(req)->str` (planner/
extractor JSON), `embed(texts)->list[list[float]]`. `StubProvider` is deterministic and needs no
key. To narrate with a real model: `from uro_cli.wiring import build_router;
build_router("openai", "gpt-4o", role_models={...})`. For **fully deterministic** gameplay (e.g. a
tactics game that only needs state + rumors, no prose) write your own scripted provider — see
`ScriptedProvider` in `hello_uro.py`.

### Worlds & campaigns
```python
world = await store.create_world(
    name, *, tone=[...], prompt_overrides={}, ruleset_id="", ruleset_version="",
    rule_pack={...},            # the Reaction-Layer rule pack (see below), carried inline
    extra_events=[...],         # authored seed events (actors/places/factions/threads/items)
)  # -> World(world_id, main_branch_id)

campaign = await store.start_campaign(
    world.world_id, world.main_branch_id, *, participant_id="player-1",
    new_pc_name="Ash",          # OR adopt_actor_id="a:existing" (adopt a world actor as PC)
    pc_sheet=None, ruleset_id="", ruleset_version="", starting_items=[], seed=0,
)  # -> Campaign(campaign_id, world_id, branch_id, ruleset_id, ruleset_version)
# `create_campaign(world_id, branch_id)` is the no-PC shorthand.
```
Seed events come from `uro_core.domain.events`: `actor_created(actor_id, name, tier, role,
aliases)`, `place_created`, `faction_created`, `edge_added(src, rel_type, dst)`, `thread_created(
thread_id, stakes, state)`, `item_created(item_id, name, owner_ref)`, `claim_recorded(...)`. Entity
ids are your strings (convention: `a:` actor, `p:` place, `f:` faction, `t:` thread, `i:` item).

### Playing beats (GM mode — Uro runs the loop)
```python
result = await engine.run_beat(campaign, "player-1", "I ask Mera about the smugglers")
# -> BeatResult(beat_id, narration, commit_id, extracted, checks, check_traces, suggestions)
async for chunk in engine.run_beat_stream(campaign, "player-1", intent): ...   # streaming
events = await engine.preview_beat(campaign, "player-1", intent)               # dry-run, no commit
```
The pipeline is: recall → (plan → mechanics, if a ruleset is bound) → narrate → extract durable
facts → validate → commit → project → **react** (Reaction Layer runs automatically post-commit).

### Reading state (projections — always current at the branch head)
`list_actors(branch)`, `get_actor(branch, id)`, `find_actor_by_name(branch, name)` (canonical +
alias match), `claims_about(branch, ref)`, `list_claims(branch)`, `beliefs_of(branch, actor)`,
`list_threads(branch)`, `list_edges(branch, rel_type=None)`, `edges_from(branch, src)`,
`list_places(branch)`, `get_place(branch, id)`, `get_sheet(branch, actor)`,
`items_owned_by(branch, owner)`, `get_item(branch, id)`, `current_world_time(branch)` (in-fiction
day), `is_pc(branch, actor)`, `active_pcs(branch)`, `recent_beats(branch, n)`,
`fact_consistency(branch)` (a proxy metric). Recall the narrator actually sees:
`from uro_core.pipeline.recall import assemble_recall`.

### Branching & time (the signature — this is what makes Uro *Uro*)
```python
fork = await store.fork_branch(world.world_id, from_ref, "what-if")  # from_ref = a commit id or marker
await store.time_skip(branch_id, days)          # advance in-fiction time (TimeAdvanced + a header)
await engine.agenda_tick(branch_id, days)       # time-skip AND fire downtime Reaction-Layer agendas
```
A fork is a full copy-on-write of world state at that commit; both lines then diverge
independently and each rebuilds by replay. `commit_id` comes from any `BeatResult`.

### Rulesets (mechanics) — pluggable
`from uro_core.rulesets import registry; registry.available()` → `{"uro-basic", "uro-pbta"}`.
`uro-basic` = d20 (hp/ac, checks, encounters). `uro-pbta` = 2d6 (stats/harm-clock/conditions/moves).
Bind one at `create_world(ruleset_id=...)`; a campaign inherits it; `Engine(store, router,
ruleset=registry.resolve(id, ver))` to run mechanics. **A custom ruleset** = implement the
`Ruleset` Protocol (`uro_core/rulesets/base.py`) — opaque `dict` sheets, graded `CheckResult`, an
encounter runner. No game vocabulary is baked into the port (proven by the two opposite built-ins).

---

## Posture B — `uro-server` over HTTP/WS (any language)

For a non-Python game, or one that wants network play. Start it:
```sh
uv run uro serve --token TOK_A --token TOK_B --provider stub --ruleset uro-basic
```
Each `--token` maps to a participant. **One ruleset per server process** (no per-campaign rebind
over the wire — a known limit).

### WS play channel — `ws://HOST:8000/campaigns/{campaign_id}/play?token=TOK`
- **You send:** `{"type": "intent", "text": "I open the vault"}`
- **You receive** (JSON frames): `beat_started{participant_id,intent}` · `narration_chunk
  {participant_id,text}` (streamed) · `beat_committed{participant_id,intent,...}` · `not_your_turn
  {participant_id,text}` (the arbiter refused — hold) · `intent_rejected{participant_id,text}` ·
  `beat_failed{participant_id,error}` · `participant_joined/left{participant_id}` ·
  `outcome_recorded{encounter_id,...}`.
- Turn order across participants is the **round-robin `PartyArbiter`** (>1 token). Only the
  turn-holder is admitted; the token rotates on commit. (This is the ONLY arbiter — see limits.)

### Chronicler endpoint — `POST /campaigns/{c}/encounters/{e}/outcome?token=TOK`
The external-gameplay posture: your game runs its own combat/gameplay and reports outcomes; Uro
records them as world memory + propagates rumors. Body = an **OutcomeBundle** (below).

`GET /healthz` → `{"status":"ok"}`. **That is the entire HTTP surface** (WS play + outcome +
health). See "What Uro does NOT have" for the management endpoints that are missing.

---

## Chronicler mode — the OutcomeBundle contract (D-25/D-32)

An external game POSTs (Posture B) or calls `distill_outcome(store, branch, bundle)` (Posture A)
after each self-run encounter. Uro distills it into committed events + witness rumors.

```jsonc
{
  "v": 1,
  "encounter_id": "e:siege-42",          // your id; claim ids derive from it (idempotent replay)
  "participants": ["a:grull","a:mera"],  // the DECLARED cast — the scope root
  "witnesses":    ["a:mera"],            // who can carry a rumor out (subset semantics apply)
  "casualties":   ["a:grull"],           // who fell
  "feats": [{"actor":"a:mera","description":"held the north gate alone"}],
  "loot":  [{"item_id":"i:sword","from_ref":"a:grull","to_ref":"a:mera"}],
  "duration_rounds": 6
}
```
**Uro is deliberately suspicious of this input (D-32). Your game must design AROUND these rules —
they are not bugs:**
- **Protection ceiling:** a PC (`is_pc`) or a **tier ≥ 2** named actor **cannot** be killed / looted
  / first-hand-witnessed by a bundle. Such a casualty is **downgraded** to a `truth=unknown`
  *testimony* claim ("X is said to have fallen"), never a committed death. Only an unprotected
  (tier 0/1) **declared combatant** actually dies. → If you want a famous NPC to die, that is canon
  the *game* can't assert; it becomes a rumor. (This is a contract to learn, and a gap to report if
  it blocks you.)
- **Participant scope:** every ref (casualties/loot/feat.actor/witnesses) must be in
  `participants`, else it is dropped. **The scope root is self-attested** — Uro trusts your bundle's
  own `participants` list (there is no parked-encounter registry yet; see limits).
- **Existence + ownership:** a casualty must exist and be alive; a loot transfer needs the item to
  exist AND `from_ref` to be its current owner.
- **Feats are testimony:** always `truth=unknown`, `origin=external`; belief-propagated to
  `witnesses` along `knows` edges with per-hop confidence decay.
- **Idempotent:** claim ids are deterministic in `(encounter_id, index)`; re-POSTing the same
  bundle upserts, it does not double-kill/loot.

Belief propagation: a feat/rumor fans out from witnesses along `knows` edges; confidence decays per
hop (a third-hand rumor is low-confidence, which the narrator renders as hedged "has heard a
rumor"). **Zero surviving witnesses → nothing propagates** (the world only remembers what someone
lived to tell).

---

## The Reaction Layer — pack-authored reactive rules (D-33), DECLARATIVE only

A world can carry `rule_pack` (inline dict at `create_world`, or `rules.yaml`/`agendas.yaml` in a
pack). Rules are **data, not code** — a closed grammar the engine evaluates. Two hooks: **post-beat**
(fires on the events a beat committed) and **downtime agendas** (fire on a day cadence at
`agenda_tick`).

```jsonc
{
  "rules_api_version": 1,
  "rules": [{
    "id": "feud-wakes-on-death",
    "trigger": {"event": "ActorDied"},                       // an event_type the beat committed
    "when": {"kind": "thread_state", "thread": "t:feud", "state": "dormant"},
    "then": [{"do": "set_thread_state", "thread": "t:feud", "to": "active"}],
    "scope": {"thread": "t:feud"}                            // jurisdiction (see below)
  }],
  "agendas": [{
    "id": "houses-drift-to-war", "every_days": 30,
    "then": [{"do":"add_edge","src":"f:red","rel":"at_war_with","dst":"f:blue"}],
    "scope": {"faction": "f:pact"}
  }]
}
```
- **Conditions** (`when`): `thread_state`, `actor_tier`, `actor_is_pc`, `edge_exists`, `world_day`,
  and `all`/`any`/`not`. Int/string/bool only. No arithmetic beyond comparisons, **no counters, no
  loops, no variables, no accumulating state.**
- **Actions** (`then`): `set_thread_state`, `create_thread`, `record_rumor` (→ `truth=unknown`
  testimony), `spread_belief`, `add_edge`/`remove_edge` over `knows`/`at_war_with`/`allied_with`
  only. The action union is **structurally incapable** of naming a mechanical/lethal/canon event
  (no damage, death, item transfer, actor creation, or `truth=true`) — that's the trust fence.
- **Scope** = the rule's jurisdiction, exactly one of `{thread|faction|place}`. Every ref an action
  touches must be inside it (a thread's id, a faction's members, a place's occupants) or it's
  dropped. **A rule touching both a thread AND an actor must split into two scoped rules** (a real
  ergonomics wrinkle — report it if it bites).
- **There is NO author-code / scripting tier.** If your world logic needs computation (counting
  armies, accumulating resources, multi-step planning, weighted random tables), the declarative
  grammar **cannot** express it. That is the reserved (unbuilt) WASM tier. **Do not fake it — record
  exactly what you needed in your GAP REPORT.** Your refusal log is the evidence that decides whether
  Uro builds the scripting tier.

---

## What Uro does NOT have yet (honest — you WILL hit these; that's the experiment)

Log each one you hit in your GAP REPORT with the detail your TASK.md asks for.

1. **No parked-encounter registry.** The Chronicler scope root is self-attested (your bundle's own
   `participants`). Uro can't verify an external game only touched actors it was authorized to.
2. **No game↔world time mapping.** You POST outcomes but must advance world time yourself
   (`time_skip`); there's no formal mapping of your game clock/turns to `world_time`.
3. **No REST management surface.** Only WS `/play` + the outcome endpoint + `/healthz` exist. There
   is **no** HTTP way to list/create worlds, browse branches, read state, query the chronicle, join
   a session, or manage campaigns. Use Posture A (the library) or the `uro` CLI for all of that.
4. **One arbiter: round-robin.** No proposal-window, consensus, GM-player, simultaneous-action, or
   consensual-PvP arbiter. Turn state is per-connection/session, not event-sourced.
5. **Server binds one ruleset per process.** No per-campaign ruleset over the wire.
6. **Reaction Layer is declarative-only** (no scripting tier — see above).
7. **Rumor distortion is confidence-decay only** — the *statement* text does not garble hop to hop.
8. **Place-state is not assembled into the narrator prompt** (active threads now are; place-state
   isn't). Places exist as state you can read, but the narrator won't see place changes.
9. **Character progression** (XP/level/advance) exists in the rulesets but nothing triggers it
   automatically in the beat loop.
10. **Entity resolution is canonical-name + alias only** — no embedding/`entity_index`. Distinct
    actors with the same colloquial name need authored `aliases`, or they fragment.
11. **No append-time emitter whitelist** — event provenance is enforced at the sources, not at the
    commit boundary (a by-policy invariant).
12. **Branching/materialization is Postgres-backed with a snapshot every ~50 commits** — untested at
    the scale of hundreds/thousands of forks; graph/vector stores are Postgres, not specialized.

## Your deliverables (every game)

1. The game (runnable with the `stub` provider, no key; a real model optional behind a flag).
2. A `GAP_REPORT.md` — the actual scientific output (format in your TASK.md).
3. A short `README.md` — what it is, how to run it, which Uro postures/features it exercises.
