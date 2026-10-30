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
| Extractor whitelist (creations — actors, places, factions, threads, claims + `member_of`/`located_in` edges, D-49/D-50 — plus two bounded mutations: resolving a live emergent thread, D-52, and an alias on a known actor, D-56) | **proven (by construction)** | the `Extraction` schema can't *express* damage/death/terrain/loss — the load-bearing anti-hallucination defense. |
| Extractor tier ceiling (new actors ≤ T1) + player-text isolation | proven | `tier=1` always; the extractor sees narration only, never the raw intent. |
| Provenance labeling (narrator→`truth=true`, dialogue→testimony+belief) | proven (by policy) | correct insofar as the extractor classifies provenance honestly. |
| Contradiction downgrade (would-be `truth=true` → `unknown`) | partial | only claims the extractor **self-flagged** vs recalled `truth=true`; a full narration-vs-state cross-check is deferred. |
| Flavor filter (atmosphere not canon) | partial | safety net depending on `durable=false` labeling; live flavor over-extraction is a known, unfixed follow-up. |
| Entity resolution (canonical name fold + partial name) | proven | `canonical_name` + SQL mirror; live stress-test showed zero splits. **D-56:** the embedding `entity_index` was MEASURED, not built — name embeddings can't separate variants from near-names (overlapping distributions on 30 labelled pairs); a deterministic partial-name rule ships instead (fresh pairs: 8/10 linked, 0/10 false merges, 0/3 ambiguous linked; `test_partial_name_rule_links_variants_and_refuses_near_names`, `test_a_partial_name_links_to_the_known_actor_and_records_an_alias`, `test_partial_names_never_link_when_ambiguous_or_conflicting` — the ambiguity guard confirmed load-bearing by mutation). **D-58 (holistic review):** the first version also linked in the SUPERSET direction and fused relatives into the named actor ("Mira's mother" → Mira, even a PC) and districts into their city ("Vel Market" → Vel); now subset-only with leading-title and possessive guards (`test_relatives_and_retitled_names_never_merge_into_a_known_actor`, each review counterexample refused). Residual risk: a new character introduced by any single name word that exactly one existing actor shares is linked unless affiliations conflict (relatives in one house don't). |
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
| Ruleset port is game-agnostic (D-30) | proven | three structurally different built-ins (`uro_basic` d20, `uro_pbta` 2d6, `uro_pool` d6 dice pool with SIMULTANEOUS exchanges — D-55) through one port+runner, the third with no PORT changes — the holistic review found the shared runner dropped all but the first effect trace per turn and a mutual knockout left no injury, both fixed (`test_a_round_close_keeps_every_strike_in_the_turn_trace`, `test_a_mutual_knockout_wounds_both_sides_and_loots_nothing`) — (`test_uro_pool.py`, incl. a full fight through the engine pipeline; live: `uro world create worlds/brinehold` → a campaign bound to `uro-pool@0` whose PC sheet is `{traits, wounds, strain, milestones}`). Free-roam check consequences still don't reach sheets (any ruleset). |
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
a full async event bus beyond the outbox's in-process publisher (the transactional outbox + NATS relay
shipped as D-57 — projections stay inline-in-txn),
`uro world delete` (privacy wipe), persisted probe reports, the per-campaign expected-head
concurrency guard, consequence-gating (D-21), the `entity_index` (OQ-3). (The **REST management
surface** is no longer fully reserved — B3/#12 shipped the authed CRUD/read core over the
`EngineStore` port; some endpoints — seed, SSE beats, world `state?at=` — stay
CLI-only. The timeline surface now ships over HTTP: branch **list** + **log** (reads), **fork** +
**marker-create** (operator-only, D-44), the **raw event log + commit detail** (operator-only,
D-45 — omniscient truth, never a player read), **dry-run** (intent-only, D-37) + **consistency**
(the T2 proxy), **pack validate** (a `.zip` upload → sufficiency grade, parse-only), **campaign end**
(operator-only, D-44), the **codex** (participant-memory get/post, self-or-admin D-39), world
**export/import** (a hash-chained bundle out / verify-chain-then-instantiate in; both operator-only —
export D-45 disclosure, import D-44 structural write; a tampered bundle → 400 before any write),
**usage telemetry** (`/usage[?stage=]` — llm_calls aggregated by stage, operator-only D-44; the
engine exposes metering, never bills; `?world=`/`?campaign=` honestly 400 — the rows aren't keyed by
world/campaign yet), the **ruleset registry** (`/rulesets` — id@version + sheet shape, any-authed)
the **world-scoped chronicle** (`/worlds/{w}/chronicle` — any named branch's recent beats,
operator-only) and the **AI world-authoring stages** (`POST /worlds/backfill` — thin-pack AI
gap-fill PREVIEW, `ai_backfill`-tagged seeds; `POST /worlds/probe[?tries=]` — judge-scored
model-capability report, **warn-not-fail** → a weak model is a 200 with `ok=false`, never an HTTP
error; both operator-only D-44 for uncapped LLM cost, pack-UPLOAD-shaped since the manifest isn't
persisted; a provider transport failure → 502, unwired provider → 501; CI stub-tested, live pass
the operator's) — BE-1..BE-10 + BE-7, #33-#42. Pack-upload *create* (with backfill-commit) and
**seed** shipped 2026-10-30 as `POST /worlds/create` and `POST /worlds/{w}/seed`, through the same
core path as the CLI (`worldpack.create`); seeding re-takes the pack, which must be the world's own,
because the world doesn't persist `manifest.history`. World **`state?at=`** shipped the same day:
`store.state_at` runs the fork's materialization into a scratch branch and always rolls it back, so
it's read-only by construction and equal to what a fork there starts from.)

**Holistic-review hardening (D-46, post-epic system-wide seam hunt).** The BE epic had never had the
project's phase-end cross-phase review; running it found a real **HIGH epistemic leak** and an
authority-consistency gap (the "real edge-case outside the happy path" the review reliably surfaces).
D-45's "by construction" non-omniscience was true only for the DEFAULT `state` sections — a player
could pass `?sections=claims,beliefs` and read `truth` values + hidden beliefs. Now **enforced, not
default:** `GET /campaigns/{c}/state` restricts a player token to a scene-safe allowlist and 403s the
omniscient sections (claims/beliefs/sheets/items/edges/counters); operators read anything. Authority
reconciled to D-44's principle: `create_world` + `time-skip` are operator-only (structural writes,
like `import`/fork/`end`). Plus scope + hygiene fixes: `start_campaign` self-or-admin, `time-skip`/
`dry-run` minted-token scope, `seed` stripped from a player read, generic `beat_failed` (no raw
exception fan-out), a zip-bomb decompressed cap, `?limit<0`/non-int-`seed`→400, `/roster` 404. All
enforced by tests (`test_holistic_review_authority_and_epistemic_fixes` on a real store).)

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
| Bounded cascade (C6) · computed cross-counter arithmetic (OQ-1) | **built (D-54 · D-53, 2026-10-30)** — awaiting the stretch's holistic review | Computed writes: `test_computed_delta_runs_an_economy_formula`, `…read_earlier_writes…`, `…fails_closed`, `…binds_counter_reads…`, `…capped_at_parse`. Cascade: `test_a_counter_change_cascades_into_a_threshold_rule`, `test_the_cascade_is_bounded`, `test_pre_cascade_rules_never_fire_in_a_cascade_hop`, `…after_a_downtime_agenda_tick` — the hop cap confirmed load-bearing by mutation, and the restriction to counter changes by removing BOTH filters (rule and event — each alone suffices, since `evaluate_rules` only matches a rule's own trigger event; the holistic review caught the earlier claim that the rule filter alone was mutation-confirmed). D-59: a no-op counter write emits nothing, so it can't drive the cascade (`test_a_no_op_counter_write_emits_nothing_and_cannot_drive_the_cascade`); a literal amount clamps as before D-53; an unsubstituted `$trigger` ref is a parse error. docs/19 asked for C6's own cross-phase review (cascade × fork-replay × export × party): that is folded into the stretch's single holistic review, not yet run. The closed grammar stays the trust fence. |

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

## Post-PoC — D-50 live tuning (2026-10-30, D-51)

Evidence: the two post-D-50 live sessions in the `uro` DB — Tamriel (13 beats) and Solara (28 beats) — plus a new scripted 15-beat session that names factions and places (`scripts/relational_live.py`), every chat role on Codex `gpt-5.6-luna`. Each beat's narration was read against what it committed; then the SAME recorded narrations were replayed through the tuned extractor (`scripts/extraction_replay.py`, same model), so the before/after differs only in the extractor.

| Replayed (same prose, same model) | threads | claims | actors | edges |
|---|---|---|---|---|
| Solara — original → tuned | 10 → **0–1** (varies run to run) | 17 → **5** (exact repeats 3 → 0) | 3 → 3 | — |
| Tamriel — original → tuned | 4 → **1** (stable) | 15 → **7–9** | — | — |
| Relational session — original → tuned | 2 → **1** | 15 → **9** | 4 (3 role nouns) → **1** | 5 (2 wrong) → **2, both right** |

| Capability | Status | Note |
|---|---|---|
| Exact restatement of a known claim or thread | **proven (structural)** | `test_restated_claim_is_not_recorded_again`, `test_exact_thread_restatement_is_dropped`, `test_same_beat_duplicates_collapse`; a dialogue restatement re-links the belief (`test_dialogue_restatement_links_belief_to_existing_claim`), a narrator confirming a rumor still commits. D-58: a dialogue repeat of a FACT attaches to the fact, module rumors are never restatement targets, and an existing belief (with its `learned_from` chain) is never overwritten (`test_restatements_respect_facts_rumors_and_existing_beliefs`). Live: "Tavi has a bracelet." was committed 4 times verbatim + once reworded — the guard catches the 3 verbatim repeats, not the rewording. |
| REWORDED restatement (the live thread failure: one plot, 8 phrasings) | **live-measured on replay (one model, three stories)** | prompt-level, via KNOWN THREADS/FACTIONS (`test_extractor_sees_known_factions_and_threads`): no replay re-proposed a known thread — incl. the relational session's trouble, asked about three ways. No similarity merge, by design — a weaker model may still reword. |
| One `null` field discards the beat's extraction | **fixed (proven)** | per-item salvage — `test_null_optional_fields_do_not_drop_the_beat`, `test_a_malformed_item_is_dropped_alone`. Whether it cost live beats is unproven: 46 extractor calls for 41 beats allows at most a few lost to exhausted re-asks; Solara beat 17 (three NPCs named, nothing committed) is the likeliest. |
| Thread granularity (a scene's suspense recorded as a plot) | **live-measured — borderline case unstable** | a first prompt pass ("a noise is NOT a thread") over-corrected (Solara 0, the canoe plot lost); the shipped wording (a passing scare becomes a thread once the characters act on it) gave Solara 1 on one run and 0 on the next — the canoe threat sits on that line. Clear plots (Tamriel's vanished villagers) land as exactly 1, every run. |
| Self-introductions recorded as claims ("Mara says that her name is Mara") | **live-measured on replay** | surfaced by the first pass (4 of 10 claims); the prompt now captures an introduction by naming the actor — 0 since. |
| Relational extraction (factions, `member_of`/`located_in`, edges) | **live-tested (one session)** | the scripted session named House Veyr, the Riverwardens, Riverkeep, The Drowned Heron, and Ser Aldren Voss: all extracted with correct names; the tuned extractor's 2 edges are both right. The original run's 2 wrong edges came from an inferred `parent` ("the tavern is in the fortress" — prompt now: only when the prose says so) and a role-noun merge (below). One session, one model. |
| Role-noun actors ("the guard") | **proven (structural)** | live, the model minted 3 role-noun actors in 15 beats despite the prompt, and a different Riverwarden merged into "guard" by name, taking a wrong location. The gauntlet now never MINTS an actor whose name is all-lowercase once its article is stripped (`test_role_noun_is_never_minted_as_an_actor`, `test_role_noun_detection`); links to an existing alias still resolve. Cost: an unnamed speaker's testimony keeps its claim but gets no belief (`test_unnamed_speaker_keeps_testimony_without_a_belief`). A capitalized description ("A Guard") isn't caught. |
| Closing a thread that play resolved | **built (D-52) — live-validated on one scenario** | "the fleeing figure" stayed open after it turned out to be Tavi. Now the extractor may resolve a live EMERGENT thread the narration settles (`test_play_resolves_a_live_emergent_thread`; authored/unknown/settled ids refused — `test_resolution_refuses_authored_unknown_and_settled_threads`, each rule confirmed load-bearing). Live: the resolution scenario resolved its thread at exactly the beat the girl was found; replaying the three recorded stories resolved nothing (0 false positives — but only 2 live threads gave it the chance). Authored threads stay out of reach by design. |
| Extractor observability | **partly fixed** (after D-51) | `llm_calls.model` now records the model serving each stage (`test_metering_records_the_model_serving_each_stage`, `test_router_reports_the_model_serving_a_role`); a narration-only fallback logs the last response's first 500 chars and a dropped item logs its content (`test_unparseable_extraction_logs_the_last_response`, `test_a_dropped_item_is_logged_with_its_content`). Still a gap: excerpts live in the process log, not Postgres; token counts aren't recorded (the provider port returns text only). |
