# 16 — Honesty ledger

*What is genuinely proven vs. what rests on deterministic-only testing, proxies, or deferrals.*
Produced by the 2026-07-09 whole-engine consolidation audit (a fan-out review over all 8 phases,
verified against the actual code/tests). The owner values not overclaiming — this is the map that
keeps the status honest. Status legend:

- **proven** — enforced by CI (deterministic tests, `import-linter`, `mypy`) or true *by construction*.
- **proxy** — a metric/mechanism that *approximates* the real thing; labeled as such in-code.
- **partial** — real but scoped/best-effort; the boundary is named, not a hard guarantee.
- **stub-only** — works in deterministic tests, but its behavior with a **live model has never been run** (CI makes no live LLM calls).
- **deferred** — named, not built.

## Bottom line (honest)

The engine is genuinely strong on its **deterministic core, and CI proves it for real**: the
hexagonal ring, projector-sole-writer + rebuild-by-replay, the extractor's *structural* whitelist,
branching/snapshots/fork + the meteor test, seeded-RNG byte-identical encounter replay, the
game-agnostic ruleset port (two structurally-opposite rulesets through one runner), the planner +
mechanics gate, export/import hash-chain verify. What does **not** rest on solid ground (and is
labeled so almost everywhere): (1) the **qualitative thesis** — one human-judged live run with
caveats; everything else is the deterministic ablation *mechanism* (bare ⇒ 0 state), which proves
plumbing, not superiority; (2) the **live behavior of every LLM-shaped capability** — probe,
backfill, belief propagation, Chronicler trust-scoping, multiplayer, and the alien ruleset *in
play* are all deterministic/stub-tested and **never run against a live model** (`scripts/postpoc_validate.sh`
is the turnkey to change that); (3) **proxies** named as such — `fact_consistency` (T2), bundle
integrity as *keyless* tamper-evidence, `history.simulate_years` (stamped, not simulated).

## Phase 1 — state engine (recall, extraction, epistemics)

| Capability | Status | Note |
|---|---|---|
| Structured (entity-triggered) recall | proven | word-boundary matching, dead-actor exclusion; `test_recall.py`. **Scope: on-stage actors + their beliefs, plus every claim about an on-stage actor/place/faction (by id) or a mentioned `name:` token; mentioned place-state; active/offered threads; the acting participant's out-of-world notes.** (docs/04's original narrower claim was superseded as these channels landed — P4 place-state, P9 module-rumor `p:`/`f:` claim matching, B8 notes.) |
| Semantic / pgvector recall | proven | branch-scoped cosine, degrades to structured-only; `test_memory.py`. Tests use the stub embedder (a bag-of-words proxy); real embeddings run live once. |
| Extractor whitelist (only actors + claims proposable) | **proven (by construction)** | the `Extraction` schema can't *express* damage/death/terrain — the load-bearing anti-hallucination defense. |
| Extractor tier ceiling (new actors ≤ T1) + player-text isolation | proven | `tier=1` always; the extractor sees narration only, never the raw intent. |
| Provenance labeling (narrator→`truth=true`, dialogue→testimony+belief) | proven (by policy) | correct insofar as the extractor classifies provenance honestly. |
| Contradiction downgrade (would-be `truth=true` → `unknown`) | partial | only claims the extractor **self-flagged** vs recalled `truth=true`; a full narration-vs-state cross-check is deferred. |
| Flavor filter (atmosphere not canon) | partial | safety net depending on `durable=false` labeling; live flavor over-extraction is a known, unfixed follow-up. |
| Entity resolution (canonical name fold) | proven | `canonical_name` + SQL mirror; live stress-test showed zero splits (the embedding `entity_index`, OQ-3, correctly NOT built). |
| Claim/belief layer (beliefs surfaced with certainty phrasing) | proven | phrasing is deterministic; a live narrator *actually hedging* on it is stub-tested, never live. |
| **Thesis: state-tracked narration > raw transcript** | **proxy** | the ablation *mechanism* (bare ⇒ 0 state) is proven; the *qualitative* win is one caveated live run (docs/live-run.md). |
| `fact_consistency` (T2) | proxy | narrator-origin `truth=true` survivors / all — a regression trend, not ground-truth verification. |
| Consequence/evidence gating (D-21) at commit | deferred | `truth=true` rests on the extractor's provenance label — "not a hard security boundary." |
| Memory compression / summarizer (synopses, journals) | deferred | `BeatResolvedPayload.synopsis` + memory-`kind` "synopsis/journal" are reserved; only "beat" is ever written. |

## Phase 2 — timeline · Phase 3 — mechanics

| Capability | Status | Note |
|---|---|---|
| Branching / snapshots / fork + the meteor test | proven | `test_meteor.py` (continue/new-life/what-if from one log), copy-on-fork, sibling isolation. |
| Materialization (nearest snapshot + replay-forward) | proven | `test_branching.py` (window==1 over depth-6). |
| Deterministic time-skip (`TimeAdvanced` + honest `AdaptationApplied` header) | proven | no LLM ripple — the header is a no-op marker. |
| Mid-play thread consequences / adaptation ripple (History emitter H) | **deferred** | no engine advances thread state; the meteor's `PlaceDestroyed` is author/test-driven, not History-emitted (docs/12 & OQ-8 overstated; reconciled). |
| Ruleset port is game-agnostic (D-30) | proven | two structurally-opposite built-ins (`uro_basic` d20, `uro_pbta` 2d6) through one port+runner. |
| Seeded-RNG byte-identical replay · encounter auto-resolve (D-29) · lost-fight consequences | proven | `test_ruleset.py`/`test_encounter.py`. |
| Planner + plan-validation + mechanics gate (D-28) | proven | 15 deterministic tests; wired into every ruleset-bound beat. |
| Character progression (level-up / advance-by-failing) | **stub-only** | implemented + unit-tested on both rulesets but **no production caller** — no XP/leveling ever fires in a beat. |
| Interactive per-turn play / `legal_actions` | deferred | encounters auto-resolve (D-29); `legal_actions` has no production caller. |

## Phase 4 — worlds

| Capability | Status | Note |
|---|---|---|
| Pack parse + sufficiency grading; import (emitter S); dry-run | proven | `test_worldpack*`, `test_dryrun`. |
| Procedural history seeding (reproducible + seed-varying) | proven | pure fn of (manifest, seed). |
| `history.simulate_years` scales the sim | proxy | stamped, not a deeper simulation. |
| AI backfill · capability probes | **stub-only** | logic tested with a fake/stub provider; **never run live**. |
| `generate_population=true` actually generates a populace | **stub-only / overclaim** | the flag lets a 0-actor pack grade `runnable`, but **nothing consumes it** (reconciled: doc note + it should gate on real actors). |
| Ruleset registry (`id@version` → impl) | partial | resolves; unknown fails loudly; **version not enforced**, `config` reserved. |

## Phase 5 — server/federation · Phase 7 — multiplayer · Phase 8 — Chronicler

| Capability | Status | Note |
|---|---|---|
| Server WS play + token auth · broadcast fan-out (two clients, same beat) | proven | via a fake `ServerDeps` (transport tested without a live DB/model). |
| Full server + real Engine over WS, end-to-end | **stub-only** | never exercised live; `scripts/postpoc_validate.sh` §Phase-7 is the manual path. |
| PartyArbiter round-robin (D-31); turn state session-only | proven | `test_party.py`; the round-robin itself is deterministic (no LLM). |
| Proposal-window (`QUEUED` live) + consensus/vote arbiters + the non-canon table-talk lane (D-38) | proven | `test_arbiter_shapes.py` + `test_server.py`; deterministic (session-only, no LLM). The non-canon guarantee is **structural** (a `table_talk`/`vote` frame calls `hub.publish` only, never `run_beat`/`append_beat` — asserted by a "run_beat untouched" test). Consensual-PvP / simultaneous / reactive-interrupt / `take_pending` stay DEFERRED behind the same port (D-38). |
| Durable turn ORDER via `pc_seats` (D-39; refines D-31) | proven | `test_session_tokens.py` (bind order from the log; adopted-PC + release/rebind) + `test_arbiter_shapes.py` (`note_joined(seats)` orders the ring + preserves the live holder across a re-seat). Deterministic. The turn **cursor** stays session-only (resets on a full restart) — a durable cursor is refused (rides `fork_branch`); resume-after-crash is a named deferral. |
| Runtime session tokens (D-39): durable, hashed, revocable, campaign-scoped; off the branch axis | proven | `test_session_tokens.py` (store: mint/revoke/list, `sha256`-only, fork-untouched, absent from `_SNAPSHOT_TABLES`) + `test_server.py` (registry restart-hydrate, operator-vs-player admit scope, WS campaign-scope reject) + `test_rest_management.py` (mint-on-join, non-operator can't seat another, revoke denies). Deterministic (no LLM). **By-policy/named residuals:** a live WS socket survives revoke (auth checked once before accept); no per-participant token cap / no revoke-on-`end_campaign`; single-process cache (cross-process coherence deferred). |
| Export/import + hash-chain verify | proven | `test_export.py`. |
| Bundle integrity | proxy | **keyless tamper-evidence**, NOT cryptographic authenticity (a re-derived internal chain). |
| Belief/rumor propagation (confidence decay, traceable) | **stub-only** | deterministic BFS tested; **never run with a live narrator**. Distortion = confidence decay only (statement garbling deferred). |
| Chronicler ingestion trust-scoping (D-32) | proven | protection ceiling / participant scope / ownership / testimony downgrade / caps / idempotent replay; `test_chronicler_hardening.py`. Out-of-cast casualties now DROP (D-41; was a rumor); loot `to_ref` protected. |
| Trusted-embedder distillation tier (D-41) | proven | `uro_core.authored.distill_authored_outcome` (`protect=_never_protected`) reuses distillation with the ceiling OFF — a Posture-A embedder's authored protected death/loot becomes real canon. Trust = module boundary: an **import-linter fence** forbids `uro_server`→`authored`/`_distill_core` (VERIFIED to bite); `OutcomeBundle` `extra='forbid'`+v-pin. `test_chronicler_hardening.py`. Structural vs the wire; the in-core-edit residual is by-policy (D-37 posture). Named residual: the trusted tier can kill a PC-bound actor (release it yourself — reachable via `append_beat` anyway). |
| Chronicler parked-encounter registry (untrusted external network game) + per-campaign endpoint authority beyond `/outcome` | deferred | RESERVED (D-41) — no untrusted network consumer exists; a corrected design is recorded (branch-scoped event-sourced, non-`cast` column, campaign-agnostic admin auth). The outcome endpoint now enforces the token→campaign scope (D-39/D-41); the wider REST authority stays deferred. |

## Invariants — by construction vs by policy

| Invariant | Status | Note |
|---|---|---|
| Hexagonal ring (core imports only ports) | **proven (by construction)** | `import-linter` KEPT in CI; now covers session/chronicler/export/metering too. |
| Rebuildable-by-replay (rebuild == original) | **proven (by construction)** | no projector handler reads wall-clock/external state; every proj_* reconstructable. |
| Projector is sole writer of proj_* | proven | |
| Extractor emitter whitelist (the LLM defense) | **proven (by construction)** | the `Extraction` schema can't express non-whitelisted events. |
| Emitter whitelist at the generic `_append`/projector boundary | **partial (by policy)** | `_append` accepts any event from any in-process emitter; the D-19 whitelist is enforced only at the extractor. Contained today solely because `distill_outcome` is the one external ingress. **Deferred: a runtime `{event_type→emitter}` gate at `_append`.** |
| Forward-only migrations | **partial (by policy)** | `migrate()` gates on version-present, no checksum — an edited migration wouldn't be detected. |

## Reserved / not built (the honest deferred list)

Event types with plumbing (factory + payload + projector handler) but **no production emitter** —
part of the forward event-catalog contract, marked reserved in docs/12: `ActorPromoted`,
`TerrainChanged`, `PlaceStateChanged`, `EdgeUpdated`. (`ClaimTruthChanged` and `EdgeRemoved` are no
longer reserved — the reaction layer emits them, module-caused: `expire_claims` retracts a stale
rumor via `claim_truth_changed` (C5, D-34/#13), and the `remove_edge` action emits `edge_removed`.)
`ActorDamaged` is **legacy** (replay-compat handler retained; the current harm path is opaque
`SheetUpdated`, D-30).
Other reserved: `TEMPLATE_API_VERSION` (pack pin),
the transactional outbox + async event bus (docs/07 — the shipped model is inline-projector-in-txn),
`uro world delete` (privacy wipe), persisted probe reports, the per-campaign expected-head
concurrency guard, consequence-gating (D-21), the `entity_index` (OQ-3). (The **REST management
surface** is no longer fully reserved — B3/#12 shipped the authed CRUD/read core over the
`EngineStore` port; some endpoints — seed/probe/export/import, SSE beats, `/usage` — stay
CLI-only. The timeline surface now ships over HTTP: branch **list** + **log** (reads), **fork** +
**marker-create** (operator-only, D-44), the **raw event log + commit detail** (operator-only,
D-45 — omniscient truth, never a player read), **dry-run** (intent-only, D-37) + **consistency**
(the T2 proxy), **pack validate** (a `.zip` upload → sufficiency grade, parse-only), **campaign end**
(operator-only, D-44) and the **codex** (participant-memory get/post, self-or-admin D-39) — BE-1..BE-9
except BE-7/BE-8, #33-#41. Pack-upload *create* + backfill/probe/seed/export/import stay CLI-only.)

**Cross-branch reads (B5/#14):** `store.query_across(branch_ids, sections)` (one query per section
via `branch_id = ANY(...)`, not N round-trips) + `diff_branches(a, b)` (added/removed/changed by PK)
+ `current_world_time_batch(branch_ids)` (each branch's in-fiction day in one recursive CTE) —
proven, read-only over `proj_*`; `test_branching.py`.

## Live validation results (2026-07-09, `scripts/postpoc_validate.sh`, default gpt-4o-mini)

First live run of the post-PoC phases. Owner ran the harness; analyzed from Postgres.

- **Phase 6 (PbtA) — binding & sheet PASS; live conflict-routing FAIL; extraction leak.**
  ✅ Campaign bound `uro-pbta`; PC sheet is genuinely PbtA (`{stats,harm,conditions,xp}`, no hp/ac);
  8 beats played, recall/extraction/memory built state (27 claims, 6 beliefs). ❌ **No mechanical
  conflict committed** — the "go aggro / seize by force" intent produced no `EncounterStarted`: the
  live planner didn't route it to the `seize_by_force` (starts-encounter) affordance (or emitted an
  unresolvable target). The engine path is proven (`test_alien_acceptance`), so this is a live
  small-model planner-routing gap on the PbtA move vocab (same class as the P3 ~50% freeform
  failure). ⚠️ **Consistency gap** (the thesis's own failure mode, surfaced): the narrator wrote a
  brawl + injuries ("bruises across your ribs") and the extractor canonized them `truth=true`, but
  the PC sheet shows `harm=0` — narration-asserted state diverged from mechanical state because no
  fight ran. ⚠️ **Flavor over-extraction confirmed live**: 21/27 claims `truth=true`, many transient
  atmosphere ("Cass is puffing on a cigar", "determination smoldering") — the known, unfixed P1.5
  follow-up, worse in a combat-flavor scene.
- **Phase 8 (Chronicler) — clean PASS.** feat `truth=unknown`/`origin=external`; Mera holds the
  war-story rumor at conf **0.272**, traceable `mera ← townsfolk(0.495) ← raider1(0.900)`; the live
  narrator **retold it hedged** ("*They say*… can you believe it?" / "the truth? Who knows?"), not
  settled fact — the confidence→phrasing→narrator path works end-to-end with a real model. (Minor:
  the narrator added extra rumor flavor — harmless as `truth=unknown` dialogue testimony.)
- **Tooling gap:** `llm_calls` stores only a `prompt_hash`, not prompts/responses — so the live
  planner-routing failure can't be pinned from Postgres alone (need response capture / a recorded
  mode to debug which failure it was).

### Live re-run (2026-07-09, after the encounter name-resolution fix)

The PbtA conflict STILL didn't fire — and the re-run revealed the real root cause, deeper than
planner routing: **entity fragmentation.** emberfell authored `Cass Holloway`; the play referenced
`Cass`; canonical matching is not substring/first-name, so recall never put the NPC on stage and
the **extractor minted a duplicate `Cass` actor** — so there was no clean, on-stage, targetable
Cass for any planner to attack. This is OQ-3 (entity resolution), surfaced live by authored full
names vs colloquial references. **Fix:** gave emberfell's NPCs colloquial `aliases` (`Cass Holloway`
→ `["Cass"]`, `Doc Venn` → `["Doc"]`) — the sanctioned mechanism (recall + `find_actor_by_name`
match aliases); automatic partial-name matching stays deferred (false-merge risk). Combined with
the encounter name-resolution fix + the planner prompt nudge, a colloquial "Cass" reference now
resolves to the authored NPC (test: `test_authored_aliases_resolve_colloquial_references`). Whether
the conflict fires end-to-end is pending the next live re-run. **Chronicler leg: still a clean PASS
on the re-run** (feat `truth=unknown`/external, Mera's rumor at conf 0.272).

### Live re-run #2 — PbtA conflict fires END-TO-END (2026-07-09, after the alias fix)

**Phase 6 is now live-validated in play**, not just deterministically. Third run, from Postgres:
no duplicate Cass (the alias folded the reference); the live planner picked the encounter
affordance and targeted `a:cass`; a **2d6 conflict committed** (`EncounterStarted` +
3× `EncounterTurnTaken` + `EncounterEnded`); and the PC sheet took **`harm=2`** — was `harm=0`
in both prior runs, so the **narration↔mechanics consistency gap is closed**: a real fight ran,
the narrator narrated the real fight ("you surge forward … the bar erupts into chaos … Cass's
eyes flash"), and the harm landed on the PbtA sheet. The three fixes that got here: encounter
ref name-resolution + the planner prompt nudge + the emberfell colloquial aliases. Chronicler
leg: still a clean PASS (feat `truth=unknown`/external, Mera's rumor at 0.272). **Status upgrade:
"the alien ruleset in play" moves from stub-only to live-validated (with default gpt-4o-mini).**

### Extraction prompt hardened against flavor over-extraction (2026-07-09)

The last remaining live-quality follow-up: the first PbtA run promoted ~21/27 claims to `truth=true`,
much of it momentary flavor (expressions, moods, sensations) despite the prompt's negative
instruction — a small model ignoring "do not extract X". Fix: rewrote `extractor.system.j2` with a
concrete KEEP/OMIT few-shot table (drawn from the actual leaks) + a "would this be true a month
from now?" durability test; sharpened the inline `durable` hint. The deterministic backstop is
unchanged and still tested (`durable=false` → dropped, `test_gauntlet_drops_flavor_claims`); this
change only improves the model's LABELING that feeds it. **Effect is prompt-side — pending the next
live re-run to measure the `truth=true` flavor ratio** (same validate-by-loop as the planner nudge).

### Live re-run #3 — flavor over-extraction MEASURABLY reduced; conflict-firing is planner-stochastic (2026-07-09)

After the extractor few-shot hardening:
- **Flavor over-extraction: clear win.** Claims/beat dropped from run-1's 27 (21 `truth=true`) to
  **13 (12 `truth=true`)**, and the `truth=true` set is now mostly durable facts ("Cass Holloway
  has scars from past brawls", "…is the claim-boss", "recent outbreak of violence between the
  Glint and the Flares"). ~4 residual flavor leaks remain (PC sensations / a momentary crowd
  reaction) — the small-model tax; notably, the PC-injury ones ("wounds on your forearms") are
  narrator-invented BECAUSE no fight fired (see below), not pure extraction error.
- **Conflict-firing REGRESSED this run (no encounter, harm=0) — but it's planner STOCHASTICITY,
  not a code regression.** The alias fix held (exactly 1 Cass actor, resolvable), so target
  resolution is solid; gpt-4o-mini simply didn't PICK the encounter affordance this time (it fired
  in run #2, not here). This cleanly isolates the residual issue to **affordance-SELECTION by a
  weak model (OQ-2)** — and the design already says the planner "needs strong structured output"
  while the harness runs gpt-4o-mini for every role. Next diagnostic: `MODEL=gpt-4o
  bash scripts/postpoc_validate.sh` (or per-role planner routing) to see if a strong planner fires
  it reliably.
- **Chronicler: clean PASS every run** (feat `truth=unknown`/external, Mera's rumor at 0.272).
- Minor: identical claims recur across beats (claims aren't content-deduped like actors are by name).

### Live re-run #4 — gpt-4o: the FULL PbtA acceptance, live (2026-07-09) — the definitive result

Ran `MODEL=gpt-4o scripts/postpoc_validate.sh`. This settles OQ-2 (planner reliability) as a
model-tier question, exactly as docs/04 predicted ("the planner needs strong structured output"):

- **Conflict fires reliably**, and — the money result — the turn outcomes are **one `miss`, one
  `partial`, one `full`**: the complete PbtA graded-outcome spectrum a binary d20 cannot express,
  live. (gpt-4o-mini, when it fired at all, gave 3× `full`.)
- **The signature persistent consequence landed: `harm=4` + `conditions=["Exposed"]`** — precisely
  what `test_alien_acceptance` asserts (a partial success leaving a durable PbtA-specific mark),
  now demonstrated with a real model.
- **The narration ENCODES the mechanics**: *"your knuckles finding purchase on his jaw … but in
  that savage moment, you're left exposed"* — the narrator narrated the 7-9 partial (hit, but
  exposed) and named the condition. Mechanics↔prose aligned; the consistency gap is fully closed
  when the fight fires.
- **Flavor over-extraction is lowest yet: 6 `truth=true` claims** (21 mini-run1 → 12 mini after the
  few-shot → 6 gpt-4o) — a stronger model also follows the extractor instructions better.
- **Chronicler: clean PASS** again (feat `truth=unknown`/external, Mera's rumor at 0.272).

**Conclusion:** every live leg of the post-PoC engine is now validated end-to-end. The residual
issues were model-tier, not engine bugs: a strong model fires the conflict, produces graded
outcomes + the alien consequence, keeps narration and mechanics aligned, and extracts less flavor.
The design's own prescription (per-role model routing — strong planner/narrator, cheap extractor)
is the cost-optimized path; the CLI exposing it is the one clean feature this surfaced (deferred).

### Two live log-warnings triaged (2026-07-09)

Owner spotted two warnings in the live runs:
- **"extractor output was not parseable JSON; committing narration-only beat"** — the narration-only
  FALLBACK is by design, but the extractor was firing it too eagerly: it did a SINGLE attempt then
  fell back, while docs/13:70-73 (and docs/04) promise "up to 2 re-asks" (the planner already does
  this). A code-vs-doc gap — one malformed response silently dropped the whole beat's state. FIXED:
  `_extract` now mirrors the planner's 3-attempt loop (1 + 2 re-asks with feedback) before the
  narration-only fallback. Tests: re-ask salvages state; exhausting re-asks still commits prose.
- **"ruleset-bound beat: PC 'a:traveler' has no sheet; checks skipped"** — benign (the WarStory leg
  is a pure Chronicler retell, no combat), but noise: `build_ruleset("")` defaults to uro-basic, so
  the unbound WarStory campaign was ruleset-bound with a sheet-less PC. FIXED in the harness: the
  WarStory leg now runs `--no-mechanics` (its correct mode — recall + extraction + narration, no
  ruleset/planner/sheet needed).

### Per-role model routing — CLI override shipped (2026-07-09)

The one feature the live validation surfaced. Per-role routing already existed via `uro.toml`
`[llm.roles]` (config-file only); added a CLI override so it needs no config file and the harness
can use it: `uro play --role-model planner=openai:gpt-4o --role-model extractor=gpt-4o-mini`.
CLI overrides win over config and fail loudly (explicit intent), vs a config role's
skip-with-warning. `scripts/postpoc_validate.sh` Leg A now routes planner+narrator to a strong
model (STRONG_MODEL, default gpt-4o) while the high-volume extractor/embedder stay cheap — the
cost-optimized path that gets reliable conflicts without paying gpt-4o for every call. Tests in
`test_wiring.py` (parse, full spec, bare-model→default-kind, CLI-beats-config, fails-loudly).
This closes the last thread the live runs opened; the ledger status for "the alien ruleset in play"
is now live-validated AND affordably runnable.

### Reaction Layer — Stage A shipped (2026-07-10, D-33, supersedes D-6)

Pack-authored reactive behavior as DECLARATIVE data (`rules.yaml`/`agendas.yaml`), no code/sandbox.

| Capability | Status | Note |
|---|---|---|
| Declarative rule interpreter + gauntlet | proven | `engines/rules.py` + `rules_gauntlet.py`, pure in-ring; `test_reaction_layer.py`. |
| Multi-ref scopes + dropped-action audit (B11, D-40) | proven | `Scope` plural forms (`factions:[a,b]`) union members (least-privilege vs `world`); a validator enforces one jurisdiction; `RULES_API_VERSION` 2→3. `run_rules_gauntlet` returns `GauntletResult(events, drops)` — every refused/partially-filtered/over-cap action records a `DroppedAction` (no more silent vanish). Action fence untouched (jurisdiction widened only). `test_reaction_layer.py`. The audit is a diagnostic (logged per pass; returned for tests) — NOT surfaced in `uro dry-run` (react doesn't run there) and NOT a committed event. |
| `for_each` / `roll_table` / `expire_claims` (C3/C4/C5, D-34/#13) | proven | bounded edge-neighbor loop with `$trigger`/`as` binding; seeded deterministic weighted pick; rumor-decay retraction (`claim_truth_changed`, migration 019 `created_day`). `RULES_API_VERSION` 3→4; recursion capped at parse + a shared node budget. `test_reaction_layer.py`. |
| Quantified/relational triggers — `$trigger`-aware `when` + `per_event` (RL-6, D-42/#25) | proven | a condition's entity-ref slots bind `$trigger.<field>` from the trigger payload, evaluated PER matching event (a true existential: "ANY member died"); `per_event` fires once per match (count-each, rides forks). Trust untouched (conditions are reads). Parse fences the ref (ref slot only + string-scalar field); an unbound/null ref fails the whole `when` closed. `RULES_API_VERSION` 4→5; v1–v4 byte-identical. `test_reaction_layer.py`. |
| Trust fence (no canon/mechanics/mint from a pack) | **proven (by construction)** | the closed `Action` Pydantic union cannot NAME a mechanical/lethal/canon event; gauntlet forces `truth=unknown`/`origin=module`, scope-fences, never mints an actor. No author code runs → the sandbox is structural. |
| Thread lifecycle (dormant→active→resolved…) | proven | `ThreadStateChanged` (emitter M) advances `proj_threads.state`; the OQ-8 FSM that had no engine. |
| Thread state reaches the narrator | proven | recall surfaces active/offered threads into the prompt ("ACTIVE THREADS"), so a module thread-activation influences the story, not just the projection — closes the gap the review implied. Place-state recall shipped (B4/#14 — a PLACES block + on-stage place/faction claim matching). |
| Post-commit reaction + downtime agenda hooks | proven | `Engine.react` (from `_finish` AND the Chronicler path) + `Engine.agenda_tick` (at time-skip). Deterministic, replay-safe (never re-run), exception-isolated. |
| `rules_api_version` pin | proven | enforced on the `RulePack` model → holds at parse, runtime, and import. |
| Inline `WorldGenesis` carry | proven | rule pack travels with the world (export/import self-contained). |
| Structural party-race pin (materialize-at-trigger-commit) | **partial (by policy)** | `react` reads current head; safe under single-writer round-robin (D-31). The structural pin is a documented future refinement (one uncovered edge: a single participant driving two concurrent beats from two devices). |
| Off-screen accumulating counters (faction state) | **shipped in Phase 10 (D-34)** — see below | supersedes the earlier "the declarative grammar can't express counters" deferral: engine-owned event-sourced counters landed once the games surfaced the use-case. |
| Sandboxed scripting tier (WASM) | deferred (reserved, sharper gate) | `ports/module.py` reserved now for UNBOUNDED computation / entity-minting that counters (D-34) + the closed grammar still can't express — no longer "any computation-shaped use-case" (that one arrived and shipped as counters). |

## Post-PoC Phase 10 — the computation layer (2026-07-12, D-34, docs/19)

Engine-owned numeric state, event-sourced so it forks by construction — the fix for shadow game-code counters that didn't ride `fork_branch` (the games' evidence, docs/18).

| Capability | Status | Note |
|---|---|---|
| Integer counters (`CounterChanged`→`proj_counters`) | **proven (by construction)** | migration 015; in the projector `_HANDLERS`+`_SNAPSHOT_TABLES`, so counters fork / replay / snapshot / export like any projection. `CondCounter` / `CondCounterCompare` / `CondCountEdges`; `world` scope. |
| `append_and_react` one-call authored-commit path | proven | commits + reacts in one exception-isolated call (docs/18 B1). |
| Counter RMW concurrency | **partial (by policy)** | `adjust_counter` is the first non-idempotent read-modify-write; a per-branch in-process `_react_lock` serializes concurrent `react()` passes. Cross-process serialization (`expected_head`) is future. |
| `for_each` (one bounded loop + `$trigger` binding) · `roll_table` (seeded weighted pick) · `expire_claims` (rumor decay) | proven | C3/C4/C5 (D-34, #13): recursive grammar (leaf-only, capped `_MAX_NESTED`/`_MAX_FANOUT`/`_MAX_TRANSLATE`), deterministic/baked pick, migration 019 `created_day`. `test_reaction_layer.py`. Trust fences: neighbor/subject scope-fenced through binding; `expire_claims` structurally never retracts canon; `_substitute` never touches `do`. |
| Bounded cascade (C6) · computed cross-counter arithmetic (OQ-1) | deferred | C6 touches `react()`'s single-hop invariant (its own review); OQ-1 economy-formula arithmetic still staged (docs/19). The closed grammar stays the trust fence. |

## Post-PoC — participant memory (2026-07-14, D-36, docs/18 B8 / #7)

A player's out-of-world notes that survive a fork (time-loop / roguelike / NG+) — a caller-owned lane keyed on `(participant_id, world_ref)`, not the branch.

| Capability | Status | Note |
|---|---|---|
| Fork-survival + reset-immunity | **proven (by construction)** | `participant_notes` is NOT a projection, NOT in `_SNAPSHOT_TABLES`, never in `fork_branch`/`_copy_memory`/`_materialize_into` — a fork copies only branch-keyed rows. `test_participant_memory.py` (fork-survival + not-in-snapshot). |
| Canon-safety — DIRECT wiring | **proven (structural, by absence)** | nothing reads a note into the extractor/planner/`proj_claims`/`proj_beliefs`/belief-propagation (grep-verified; `test_note_never_becomes_canon_or_a_belief`). |
| Canon-safety — the narration→extract echo | **partial (by policy)** | a note surfaces in the narrator prompt (labelled "the world does NOT know this"); if the narrator ECHOES it, the echo is extractable like any narrator output — the same narrator-tier-trust residual EVERY narrator input has (docs/13), not a new hole. Untestable deterministically (the stub never echoes). |
| Dedup / idempotent (caller key + `sha256` fallback) · recall selection (pinned + entity-triggered) | proven | deterministic; `test_participant_memory.py`. |
| Event-sourced participant journal (audit / forget / confidence-decay) · separate participant export · global cross-world scope | deferred (reserved) | behind the same port on an evidence gate — grow when a 2nd game evidences the need (D-36; validate-before-building). |

## Post-PoC — client-supplied plan (2026-07-14, D-37, docs/18 B9 / #8)

A caller-supplied `BeatPlan` drives the planner→mechanics gate deterministically (no LLM) — for CI mechanics coverage + keyless/embedded consumers.

| Capability | Status | Note |
|---|---|---|
| Deterministic free-roam check via `run_beat(..., plan=…)` | **proven** | `test_deterministic_plan.py`: an injected plan resolves a check (`checks==1`) with the stub planner a no-op (`checks==0`), byte-identical across seeds (G-3). |
| Deterministic ENCOUNTER via a supplied plan, rounds surfaced in `check_traces` | **proven** | `test_deterministic_plan.py::…start_an_encounter…`: a supplied attack plan resolves a full fight; `check_traces` is non-empty and `checks==len(check_traces)` (the phase-end review's combat-empty gap, fixed). |
| Same-fence guarantee (supplied plan ≡ LLM plan) | **proven (by construction)** | one `validate_plan` (affordance fence + D-21 trigger coverage) for both paths; an unknown affordance raises `PlannerError` (`test_invalid_supplied_plan_is_rejected`). No second validator. |
| Trust posture — `plan=` is a TRUSTED in-process input | **by design (documented)** | the caller is inside the trust boundary (it can drive the store directly), so NO D-32 protection ceiling is applied. The ceiling fences the EXTERNAL Chronicler POST (`distill_outcome`); a future network-exposed `plan=` MUST route through it first — this is not that path (D-37). |
| Not gated under no-ruleset / `--bare` | **by design (now loud)** | correctly runs no mechanics (nothing to gate / ablation integrity), but LOGS a warning rather than silently voiding a supplied plan (review fix). |
| Wired to `serve`/CLI · rule-based fallback planner · `CheckResolved` event trace | deferred (reserved) | library API only for the MVP; the fallback planner + a per-check event are reserved behind #8 (D-37). |
