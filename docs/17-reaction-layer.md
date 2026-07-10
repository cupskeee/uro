# 17 — Reaction Layer (pack-authored reactive behavior) — PROPOSAL

> **STATUS: Stage A COMPLETE (2026-07-10)** — INC-1..5 shipped, 242 tests green, **D-33** appended
> to `decisions.md`, phase-end system-wide cross-phase review done (3 confirmed + 1 high critic
> fixed). Supersedes **D-6**. The five open questions were DECIDED (see below).

## Decided open questions (owner, 2026-07-10)

1. **Follow-on beat = a SEPARATE ordered `caused_by=module` beat.** Not atomic with the trigger;
   the rare crash-in-between gap is tolerable/recoverable (best-effort consequence, like belief
   propagation). **As built (honest, post-review):** the reaction reads state at the *current
   branch head* right after `append_beat`; under the single-writer / round-robin (D-31) turn model,
   current head IS the trigger commit, so the party race is CONTAINED behaviorally. The stronger
   *structural* materialize-at-trigger-commit pin is a documented FUTURE refinement — the one edge
   it doesn't cover is a single participant driving two concurrent beats from two devices (the
   deterministic `trigger_commit`-keyed claim ids bound the blast radius; no canon corruption).
2. **Modules NEVER mint entities** — hold the line. "X appears" = pre-author X dormant in worldpack
   + a rule that activates/reveals it. Truly-dynamic spawning is refused → the sharpest Stage-B trigger.
3. **Cross-rule conflicts = total order by `rule_id`** (deterministic) + a `world validate` WARNING
   on two rules targeting one thread. Exclusive jurisdiction deferred until multi-pack install exists.
4. **Emitter whitelist stays STRUCTURAL-AT-SOURCE** (Action union + gauntlet, like the extractor/
   Chronicler). `caused_by=module` ships as the anchor; the append-time gate is a named future increment.
5. **Static `world validate` rule-checking ships in Stage A** (INC-2); `uro dry-run --rules` is a
   deferred fast-follow.

## The problem D-6 deferred, now due

D-6 deferred a module/scripting system on the owner's pushback: *"too much for an immature
engine."* The engine is now mature and live-validated, so that premise has expired — and a
concrete gap is due: **OQ-8** (mid-play thread consequences, the `dormant→offered→active→resolved`
thread state machine, off-screen faction/actor agendas) is unbuildable with the three existing
extension surfaces:

| Surface | What it is | Ceiling |
|---|---|---|
| **Ruleset** (D-10/D-30) | *trusted* code plugin, behind a Protocol port | mechanics only (sheets/checks/encounter) |
| **Prompt pack** (D-6) | *declarative* Jinja overrides | narrator voice only |
| **Worldpack** | *declarative* YAML + a one-shot history seed | static data; no reaction to play |

Verified: only `ThreadCreated` has a projector handler — **no engine advances thread state.** The
open quadrant is *pack-authored behavior that reacts to committed state during play*, which is
neither mechanics, nor voice, nor static data.

## The verdict: a declarative Reaction Layer now; a sandbox port reserved

Four approaches were designed and judged (security auditor + pragmatist + architect lenses):

| Approach | Avg | Verdict |
|---|---|---|
| **Declarative reaction layer** (data, no code) | **8.67** | strong ×3 |
| Restricted expression-DSL | 7.00 | strong/viable |
| Sandboxed WASM modules | 6.67 | strong/viable — XL effort, native dep, authoring friction |
| Isolated Python hooks | 6.67 | viable — ships a known-broken RestrictedPython tier |

**The decisive insight:** D-6's whole fear was a from-scratch **sandbox** — the highest-risk,
least-precedented subsystem. A declarative layer **meets that constraint structurally: there is no
author code to sandbox.** It closes the entire concrete justification on the table (OQ-8) at ~L
effort, and it keeps the load-bearing trust axis intact — **Ruleset = trusted/mechanics** vs
**Module = untrusted/behavior**. True scripting is *reserved behind a port* and refused until a
use-case demonstrably escapes the data layer's ceiling.

So: ship the **Reaction Layer** (declarative); reserve the word "module/scripting" for the deferred
sandbox tier, so the trust distinction stays legible.

## The design (Stage A — declarative)

A pack ships **data** (`rules.yaml` / `agendas.yaml`), never code. One *trusted, first-party*
interpreter in the core ring evaluates it and **returns proposals**; a trusted gauntlet turns
proposals into a bounded, safe event set; only the commit stage writes.

**Contract**
- **Observe (read-only):** `evaluate_rules(store: ProjectionQueries, *, rules, trigger_events, world_time, rng) -> list[Action]` — the same read-port-in / *proposals*-out shape as `run_gauntlet` / `distill_outcome`. Conditions are a **closed, pure, total** expression grammar over a narrowed facade of `ProjectionQueries` (`actor(id).tier|status|is_pc`, `thread(id).state`, `edge_exists`, `world.day`, matches over the triggering events). No loops, no assignment, no user functions, no recursion.
- **Emit:** a **closed Pydantic tagged-union of `Action`s** — `set_thread_state` / `complete_thread_step` / `offer_thread` / `create_thread(dormant, provenance=module)`, `record_rumor` (→ `ClaimRecorded` forced `truth=unknown`, `origin=module`), `spread_belief` (→ a capped belief fan-out), `add_edge`/`remove_edge` over a **whitelisted non-authoritative** rel-set (`knows`/`at_war_with`/`allied_with` — never `owns`/`rules`). *(Critic fix: the interpreter returns `Action`, **never** `DomainEvent` — that closed union is the structural fence; letting rule data build raw events would void it.)* The union **cannot name** `ActorDamaged`/`ActorDied`/`ItemTransferred`/`SheetUpdated`/`ActorCreated`/`PlaceDestroyed` or a `truth=true` claim.
- **Lifecycle (two fixed hooks, both post-commit, both run once at live-play and never on replay):** (1) **post-beat** reaction pass from `Engine._finish`, fed the just-committed events, accepted actions gauntleted and committed as a follow-on `caused_by=module` beat; (2) **downtime/agenda tick** at `store.time_skip`'s boundary, keyed off in-fiction `world.day`. One pass per trigger; module events are inert to the pass (bounded termination).
- **Declared in a pack:** `rules.yaml`/`agendas.yaml` beside `manifest.yaml`; `worldpack/parse.py` gains validated `RulePack` models with an **enforced `rules_api_version`** (closes the reserved-but-unenforced `TEMPLATE_API_VERSION` gap). *(Critic fix — P-module × P5: the rule **content** is stamped **inline** into `WorldGenesis` like `prompt_overrides` already is, not just an id+hash — else reactions can't fire after export→import on a host without the pack files.)*

**Trust model — no sandbox because no code runs.** Two structural layers + a content gauntlet,
mirroring how D-19 is *actually* enforced (at the source):
1. **Structural (parse-time):** the `Action` union physically cannot express a mechanical/lethal/canon event.
2. **Content gauntlet** (`run_rules_gauntlet`, a new pure fn beside `run_gauntlet`/`distill_outcome`): forces `truth=unknown` testimony; a **protection ceiling** — *(critic fix: per-action, not blanket)* — **kill/loot/bind/mutate** of an `is_pc`-or-`tier≥2` actor is refused (reusing `chronicler._is_protected`), but a **rumor about** or an `at_war_with` **edge between** T2 rulers is allowed (agendas are centrally about powerful actors); **scope-fenced** to the rule's declared jurisdiction (thread stakeholders / faction members / place occupants — the generalization of D-32 participant-scoping); **entity-resolve, never mint** (creation stays the extractor's T1 privilege); **caps** on actions-per-pass and fan-out; **deterministic idempotent ids** (`m:{trigger_commit}:{rule_id}:{i}`).
3. **Provenance:** add `"module"` to the `CausedByKind` literal with a rule-id discriminator — auditable, un-launderable as trusted `agenda`/`history`, and the eventual anchor for D-32's deferred append-time whitelist. *(Critic fix: `spread_belief` needs its own `caused_by=module`; it can't reuse `propagate_belief` verbatim, which hardcodes `agenda`.)*

**Determinism model.** The interpreter is a pure function of *(pinned rule table, the projection
view, a seeded `Rng`, `world_time`, the trigger events)*; its whole effect is captured as events and
it is **never re-run on replay/fork** — so the meteor test and byte-identical fork replay hold.
*(Critic fixes: it is classified like `distill_outcome`, not `seed_history` — safe on replay only
because effects are baked into events, not because it's reproducible from pinned inputs. The
`Rng` seed and projection view are pinned to the **trigger commit**, not "current head" — else
concurrent party play (P7) diverges. The "ban floats" idea was wrong: `BeliefChanged.confidence`
and `EdgeAdded.weight` are already float fields and export/import hash-verify already works with
them; the real rule is the interpreter must not **compute** new float noise — reusing the existing
float-carrying events as-is is fine.)* A deterministic **fuel/size/int-magnitude budget** bounds
cost and fails closed (proposals dropped, the beat still commits).

## What this deliberately does NOT do (honest ceiling)

- **No code sandbox is built** — no RestrictedPython, WASM, subprocess, or `exec`/`eval`. That's the point.
- **The DSL never grows toward Turing-completeness** — no loops/assignment/functions/recursion/float arithmetic. Genuine computation is **refused**, not faked with accumulating operators.
- **No mechanics** — sheets/checks/damage stay the trusted ruleset's opaque `SheetUpdated` path.
- **No canon** — cannot assert `truth=true`, create/promote actors, or kill/loot/bind a protected actor.
- *(Critic-flagged honest limits)* **Not deep off-screen simulation** — it does thread FSMs + trigger→bounded-consequence + rumor/edge agendas, **not** accumulating faction state (armies/resources/territory), which would need bespoke projections a module can't define. And **it cannot mint entities** — "a wandering merchant appears when the thread activates" is a common content-mod need that this layer **refuses**; if that's wanted, it's the strongest candidate to *open* Stage B (or a narrow future `create_actor(from_pack_template, tier=1)` action, itself a decision).
- **No REST surface / rule-execution reports / pack marketplace.**

## Build sequence (Stage A)

*(Critic fix: INC-1 alone ships no end-to-end value; fold one hardcoded rule in to retire the real
risk — the post-commit follow-on-beat hook — before building the DSL.)*

1. **INC 1 — safe landing zone + the risky hook, proven with ONE hardcoded rule.** Add `ThreadStateChanged`/`ThreadStepCompleted` events + projector handlers + snapshot-whitelist columns + a forward-only migration; add `CausedByKind="module"` + `module_cause()`. Wire the **post-beat hook** in `Engine._finish` driving a single *hardcoded, trusted* thread-FSM rule. Test: a player-caused `ActorDied` advances a thread `dormant→active` **and survives a fork** (meteor-style). This proves the hook + replay/fork of a module-caused beat before any pack data exists.
2. **INC 2 — pack format + parse + version pin.** `RulePack`/`AgendaPack` models, the closed grammar + `Action` union, parse-time validation, enforced `rules_api_version`, `WorldGenesis` inline content stamping. Two example packs (a real rule in `worlds/ashfall`; a thin pack that fails version-check).
3. **INC 3 — interpreter + gauntlet.** `engines/rules.py` `evaluate_rules(...) -> list[Action]` (pure, in-ring, ports only) + the fuel budget; `engines/rules_gauntlet.py` `run_rules_gauntlet` (protection ceiling, scope fence, resolve-never-mint, forced testimony, caps, deterministic ids). Golden-file determinism + drop-protected + drop-out-of-scope + fork idempotency tests. Add the **no-rules fast short-circuit** so rule-less worlds pay ~nothing per beat *(critic-flagged multi-campaign cost)*.
4. **INC 4 — downtime/agenda tick** at `time_skip` (seeded edges/rumors; seed 42≠43; byte-identical replay).
5. **INC 5 — phase-end SYSTEM-WIDE cross-phase review** (P-reaction × P0..P8 + invariants), then append **D-33** superseding D-6 and reconcile docs (10/11/12/13/15/16; OQ-8 narrowed to "mechanism closed, deep simulation still open").

**Stage B (RESERVED — do not build):** a `ports/module.py` Protocol + a **WASM/Wasmtime** adapter
(never arbitrary Python), reusing INC-3's gauntlet + INC-1's vocabulary unchanged. Refused until a
documented refusal-log shows authors genuinely need computation the data layer can't express.

## Open questions to pin before building

1. **Follow-on-beat transactionality:** same atomic append as the trigger beat, or a clearly-ordered separate `caused_by=module` beat? (Separate is simpler for replay/fork; pin in INC-4.)
2. **Entity minting:** hold the "modules never create entities" line, or add a narrow `create_actor(from_pack_template, tier=1)` action? (Affects the merchant-appears use-case.)
3. **Cross-pack rule interaction:** total order by `rule_id` is deterministic but arbitrary — acceptable, or is jurisdiction exclusive?
4. **Append-time emitter whitelist (deferred at D-32):** `CausedByKind="module"` is the natural anchor — build it now, or keep relying on structural-at-source?
5. **`uro dry-run --rules` / `uro world validate` rule-checking** shipped with Stage A, or later?

## Draft decision (proposed — appended to `decisions.md` only on acceptance)

> **D-33 — Pack-authored reactive behavior: a declarative Reaction Layer now, a sandboxed scripting
> port reserved (supersedes D-6).** A pack ships DATA (`rules.yaml`/`agendas.yaml`), evaluated by one
> trusted first-party interpreter (`engines/rules.py`) that reads via `ProjectionQueries`, evaluates a
> closed/pure/total/no-float grammar, and returns `Action` proposals; a trusted `run_rules_gauntlet`
> (reusing `chronicler._is_protected` per-action) validates them into a bounded safe event set at the
> Phase-8/D-32 bar (testimony-only, no PC/T2+ kill-loot-bind, scope-fenced, capped, deterministic
> ids); only the commit stage writes. No author code runs, so the from-scratch sandbox is met
> structurally. `CausedByKind="module"` gives honest provenance; two trusted thread events +
> projector handlers + a migration ship first; rule content is carried inline in `WorldGenesis` so
> imports stay self-contained. A `ports/module.py` sandbox tier (WASM-preferred, never
> RestrictedPython) is RESERVED and refused until a computation-shaped use-case escapes the data
> layer's ceiling. Reverses D-6's "not yet" for the declarative tier; keeps its caution for scripting.
