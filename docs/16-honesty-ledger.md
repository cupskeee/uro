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
| Extractor whitelist (creations — actors, places, factions, threads, claims + `member_of`/`located_in` edges, D-49/D-50 — plus three bounded mutations: resolving a live emergent thread, D-52, an alias on a known actor, D-56, and retiring a narrator-established fact the narration changes, D-70) | **proven (by construction)** | the `Extraction` schema can't *express* damage/death/terrain/loss — the load-bearing anti-hallucination defense. |
| Extractor tier ceiling (new actors ≤ T1) + player-text isolation | proven | `tier=1` always; the extractor sees narration only, never the raw intent. |
| Provenance labeling (narrator→`truth=true`, dialogue→testimony+belief) | proven (by policy) **+ in code for double-quoted speech (D-73)** | when the gauntlet is given the narration (the engine always passes it), a `narrator` claim whose words come mostly from a character's `"…"`/`“…”`/`«…»` lines, and less than half from the narrator's own text, is recorded as testimony (`test_a_characters_quoted_line_is_testimony_whatever_its_label`). A short title-case quote is a name, not speech (`test_quotes_that_are_not_speech_stay_narration`). Single-quoted and reported speech still rest on the extractor's label. |
| Contradiction downgrade (would-be `truth=true` → `unknown`) | partial | only claims the extractor **self-flagged** vs recalled `truth=true`; a full narration-vs-state cross-check is deferred. |
| Flavor filter (atmosphere not canon) | partial (prompt) **— measured, D-73** | still the extractor's judgment (`durable`), backed by a closing check in its prompt. On replays, `gpt-5.6-luna` records no atmosphere as fact; `gpt-4o-mini` went from ~24 such claims to ~4 across nine sessions (five held out). A small model still lets some through. |
| Entity resolution (canonical name fold + partial name) | proven | `canonical_name` + SQL mirror; live stress-test showed zero splits. **D-56:** the embedding `entity_index` was MEASURED, not built — name embeddings can't separate variants from near-names (overlapping distributions on 30 labelled pairs); a deterministic partial-name rule ships instead (fresh pairs: 8/10 linked, 0/10 false merges, 0/3 ambiguous linked; `test_partial_name_rule_links_variants_and_refuses_near_names`, `test_a_partial_name_links_to_the_known_actor_and_records_an_alias`, `test_partial_names_never_link_when_ambiguous_or_conflicting` — the ambiguity guard confirmed load-bearing by mutation). **D-58 (holistic review):** the first version also linked in the SUPERSET direction and fused relatives into the named actor ("Mira's mother" → Mira, even a PC) and districts into their city ("Vel Market" → Vel); now subset-only with leading-title and possessive guards (`test_relatives_and_retitled_names_never_merge_into_a_known_actor`, each review counterexample refused). Residual risk: a new character introduced by any single name word that exactly one existing actor shares is linked unless their factions conflict (since D-68 a stated place no longer blocks a partial link) (relatives in one house don't). |
| Claim/belief layer (beliefs surfaced with certainty phrasing) | proven | phrasing is deterministic; a live narrator *actually hedging* on it is stub-tested, never live. |
| **Thesis: state-tracked narration > raw transcript** | **proxy** | the ablation *mechanism* (bare ⇒ 0 state) is proven; the *qualitative* win is one caveated live run (docs/live-run.md). |
| `fact_consistency` (T2) | proxy | narrator-origin `truth=true` survivors / all but superseded ones (D-70) — a regression trend, not ground-truth verification. |
| Consequence/evidence gating (D-21) at commit | deferred | apart from the quoted-speech check (D-73), `truth=true` rests on the extractor's provenance label — "not a hard security boundary." |
| Memory compression / summarizer (synopses, journals) | deferred | `BeatResolvedPayload.synopsis` + memory-`kind` "synopsis/journal" are reserved; only "beat" is ever written. |

## Phase 2 — timeline · Phase 3 — mechanics

| Capability | Status | Note |
|---|---|---|
| Branching / snapshots / fork + the meteor test | proven | `test_meteor.py` (continue/new-life/what-if from one log), copy-on-fork, sibling isolation. |
| Materialization (nearest snapshot + replay-forward) | proven | `test_branching.py` (window==1 over depth-6). |
| Deterministic time-skip (`TimeAdvanced` + honest `AdaptationApplied` header) | proven | no LLM ripple — the header is a no-op marker. |
| Mid-play thread consequences / adaptation ripple | **authored, by design (D-88)** | no engine sweep: a cataclysm's ripple is the world's reaction rules on the event (`test_the_meteor_ripple_is_authored`: one rule on `PlaceDestroyed` kills the festival plot, creates a (dormant) refugee plot, records the news as a rumor and bumps a grief counter on a named contact's `knows`-neighbours; a pre-strike fork has none of it; export/import keeps it). |
| Ruleset port is game-agnostic (D-30) | proven | three structurally different built-ins (`uro_basic` d20, `uro_pbta` 2d6, `uro_pool` d6 dice pool with SIMULTANEOUS exchanges — D-55) through one port+runner, the third with no PORT changes — the holistic review found the shared runner dropped all but the first effect trace per turn and a mutual knockout left no injury, both fixed (`test_a_round_close_keeps_every_strike_in_the_turn_trace`, `test_a_mutual_knockout_wounds_both_sides_and_loots_nothing`) — (`test_uro_pool.py`, incl. a full fight through the engine pipeline; live: `uro world create worlds/brinehold` → a campaign bound to `uro-pool@0` whose PC sheet is `{traits, wounds, strain, milestones}`). Free-roam check consequences still don't reach sheets (any ruleset). |
| Seeded-RNG byte-identical replay · encounter auto-resolve (D-29) · lost-fight consequences | proven | `test_ruleset.py`/`test_encounter.py`. |
| Planner + plan-validation + mechanics gate (D-28) | proven | 15 deterministic tests; wired into every ruleset-bound beat. |
| Character progression (level-up / advance-by-failing) | proven | Free-roam (D-61): a check returns its actor's changed sheet and the engine commits it. Fights and downtime (D-63): the `conclude_encounter` and `downtime` hooks apply each built-in's advancement and rest from the world's settings (D-62), to PCs only for advancement. Covered by `test_uro_pool.py`, `test_advancement.py` (each ruleset, the runner, an engine fight, the time-skip pass), `test_ruleset_config.py`, and the server and CLI time-skip tests. |
| Interactive per-turn play / `legal_actions` | deferred | encounters auto-resolve (D-29); `legal_actions` has no production caller. |

## Phase 4 — worlds

| Capability | Status | Note |
|---|---|---|
| Pack parse + sufficiency grading; import (emitter S); dry-run | proven | `test_worldpack*`, `test_dryrun`. |
| Procedural history seeding (reproducible + seed-varying) | proven | pure fn of (manifest, seed). |
| `history.simulate_years` scales the sim | proxy | stamped, not a deeper simulation. |
| AI backfill · capability probes | **stub-only** *(superseded: live-validated once in "the v0.7 live run", below)* | logic tested with a fake/stub provider; **never run live**. |
| `generate_population=true` actually generates a populace | **reconciled (D-81)** | it never generated anyone up front, and still doesn't: the flag now means the people are made in play (the narrator names them, the extractor records them, D-49/D-50), and it counts only while the instance's extraction policy lets play create actors — otherwise the pack is refused as insufficient (`test_a_pack_relying_on_play_for_its_people_needs_play_to_make_them`). |
| Ruleset registry (`id@version` → impl) | partial | resolves; unknown fails loudly; **version not enforced**, `config` reserved. |

## Phase 5 — server/federation · Phase 7 — multiplayer · Phase 8 — Chronicler

| Capability | Status | Note |
|---|---|---|
| Server WS play + token auth · broadcast fan-out (two clients, same beat) | proven | via a fake `ServerDeps` (transport tested without a live DB/model). |
| Full server + real Engine over WS, end-to-end | **stub-only** *(superseded: live-validated in "the v0.7 live run" and the later passes, below)* | never exercised live; `scripts/postpoc_validate.sh` §Phase-7 is the manual path. |
| PartyArbiter round-robin (D-31); turn state session-only | proven | `test_party.py`; the round-robin itself is deterministic (no LLM). |
| Proposal-window (`QUEUED` live) + consensus/vote arbiters + the non-canon table-talk lane (D-38) | proven | `test_arbiter_shapes.py` + `test_server.py`; deterministic (session-only, no LLM). The non-canon guarantee is **structural** (a `table_talk`/`vote` frame calls `hub.publish` only, never `run_beat`/`append_beat` — asserted by a "run_beat untouched" test). Consensual-PvP / simultaneous / reactive-interrupt / `take_pending` stay DEFERRED behind the same port (D-38). |
| Durable turn ORDER via `pc_seats` (D-39; refines D-31) | proven | `test_session_tokens.py` (bind order from the log; adopted-PC + release/rebind) + `test_arbiter_shapes.py` (`note_joined(seats)` orders the ring + preserves the live holder across a re-seat). Deterministic. The turn **cursor** stays session-only (resets on a full restart) — a durable cursor is refused (rides `fork_branch`); resume-after-crash is a named deferral. |
| Runtime session tokens (D-39): durable, hashed, revocable, campaign-scoped; off the branch axis | proven | `test_session_tokens.py` (store: mint/revoke/list, `sha256`-only, fork-untouched, absent from `_SNAPSHOT_TABLES`) + `test_server.py` (registry restart-hydrate, operator-vs-player admit scope, WS campaign-scope reject) + `test_rest_management.py` (mint-on-join, non-operator can't seat another, revoke denies). Deterministic (no LLM). **By-policy/named residuals:** a launch `--token` can't be revoked at runtime (a revoke closes the revoked player's open sockets, 2026-10-30); no per-participant token cap / no revoke-on-`end_campaign`; single-process cache (cross-process coherence deferred). |
| Export/import + hash-chain verify | proven | `test_export.py`. |
| Bundle integrity | proxy | **keyless tamper-evidence**, NOT cryptographic authenticity (a re-derived internal chain). |
| Belief/rumor propagation (confidence decay, traceable) | **stub-only** *(superseded: retold by a live narrator in "the v0.7 live run", below)* | deterministic BFS tested; **never run with a live narrator**. Distortion = confidence decay only (statement garbling deferred). |
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
| Emitter whitelist at the generic `_append`/projector boundary | **closed tiers proven (D-89); open tiers by source** | `_append` and `import_world` refuse an event whose `external` or `module` cause its producer can't make (`test_provenance.py`, every rule action kind and both distillers run through it). The open causes (`system`, `player_action`, `ruleset`, `history`, `agenda`) aren't checked: an embedder holds root. |
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
`EngineStore` port; since the 2026-10-30 parity pass (D-60) only SSE beats stay unbuilt, and the
outbox relay, `db migrate` and the testing flags are CLI-only by nature. The timeline surface now ships over HTTP: branch **list** + **log** (reads), **fork** +
**marker-create** (operator-only, D-44), the **raw event log + commit detail** (operator-only,
D-45 — omniscient truth, never a player read), **dry-run** (intent-only, D-37) + **consistency**
(the T2 proxy), **pack validate** (a `.zip` upload → sufficiency grade, parse-only), **campaign end**
(operator-only, D-44), the **codex** (participant-memory get/post, self-or-admin D-39), world
**export/import** (a hash-chained bundle out / verify-chain-then-instantiate in; both operator-only —
export D-45 disclosure, import D-44 structural write; a tampered bundle → 400 before any write),
**usage telemetry** (`/usage[?stage=]` — llm_calls aggregated by stage, operator-only D-44; the
engine exposes metering, never bills; `?world=`/`?campaign=` filter to the calls that served them
since D-66 — calls metered before 0.7 carry no world and count only in the unfiltered total), the **ruleset registry** (`/rulesets` — id@version + sheet shape, any-authed)
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
| Wired to `serve`/CLI · rule-based fallback planner | deferred (reserved) | library API only for the MVP; the fallback planner is reserved behind #8 (D-37). The per-check event shipped as `CheckResolved` (D-64, below). |
| `CheckResolved`: a check outcome reaction rules can answer (D-64) | proven | `test_check_resolved.py`: each check commits its outcome in the ruleset's band, and a `per_event` heat meter counts two failures a beat. |
| Ruleset plugins via the `uro.rulesets` entry point (D-65) | proven | `test_plugins.py`: discovery, every contract failure, duplicate ids, a required setting, a world playing the example plugin end to end; plus a real `pip install` of `examples/plugins/uro-coinflip` into a fresh venv. The contract is also enforced on `register()` and by the `Engine`. |

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
| Extractor observability | **partly fixed** (after D-51) | `llm_calls.model` now records the model serving each stage (`test_metering_records_the_model_serving_each_stage`, `test_router_reports_the_model_serving_a_role`); a narration-only fallback logs the last response's first 500 chars and a dropped item logs its content (`test_unparseable_extraction_logs_the_last_response`, `test_a_dropped_item_is_logged_with_its_content`). **Closed by D-66:** every structured call's full response is stored in `llm_calls` (`uro calls`, `GET /llm-calls`) and the OpenAI, Anthropic and Codex adapters report token counts (`test_metering.py`, mock transports; a live pass is the owner's). |

## Post-PoC — the v0.7 live run (2026-10-31)

The first live pass over the capabilities that had only run against stubs, run by the owner on
v0.7.0 (runbook: `docs/live-run.md` §8) and read back from Postgres. Models: the registry's
`gpt-5.6-luna` (ChatGPT/Codex) for chat and `text-embedding-3-large`, and `--provider openai`
(`gpt-4o-mini`) for backfill, probe and the war story.

| Capability | Status | Evidence |
|---|---|---|
| Full server + real Engine over WebSocket | **live-validated (two sessions)** | 11 beats through `uro serve` + `uro connect` on Ashfall, 1 more through a second server. Found on the way: #129 had broken `serve` (the store never opened; every connect was a 500), fixed in #130 with a test that drives the real `serve` wiring. Second session (2026-11-01, the fourth pass): six party beats through `uro serve` on the registry's providers, no errors. |
| Live commits across processes (D-67) | **live-validated** | relay + two servers: the client printed the CLI's in-place time-skip and the beat played through the other server; every commit reached the outbox and was published. |
| Token metering (D-66) | **live-validated** | every Codex chat call carried tokens in and out; OpenAI embeddings and `gpt-4o-mini` calls too; per-world and per-campaign filters correct. The extractor is the costliest stage (~55% of chat input tokens) and the slowest (7 s mean, 13.8 s worst) — and the next beat waits for it. |
| Flavor recorded as fact | **much improved, live** | Ashfall (12 beats, `gpt-5.6-luna`): one narrator fact recorded true ("Old Weck has milky eyes", a lasting trait); 14 dialogue claims as testimony. In July: 21 of 27 claims true, many passing moments. |
| Reported news dropped as "not durable" | **found live → prompt fixed, replay pending** | 2 of 16 proposed claims dropped, both plot news a character reported ("a woman came asking after you", "the Duke means to seal the waterfront"). The prompt now keeps reported news and stated plans as durable testimony. |
| AI backfill · capability probes | **live-validated once** | probe: structured output 3/3, violence and horror both engaged. Backfill filled Thornwood's conflict as an `ai_backfill` thread — written as game design ("players must decide…"); the worldsmith prompt now asks for the world's own voice. |
| Belief/rumor retold by a live narrator | **live: hedged ✓ — laundering found → fixed (the context is structural; skipping a reworded retelling is prompt-guarded)** | Mera retold the third-hand rumor as hearsay ("there's talk of…"). But the retelling became a NEW claim she believed at 0.8 beside the 0.27 rumor: the extractor's KNOWN CLAIMS left out the on-stage characters' beliefs the narrator is shown, and `confidence` was undefined (the model gave its own certainty). Now the extractor sees those beliefs (structural, `test_a_retold_rumor_stays_a_rumor`); whether it then skips a REWORDED retelling is up to the model — only an exact restatement is deduped structurally (D-58). `confidence` means how sure the speaker sounds (prompt). Replay confirmed both (below). `gpt-4o-mini` also recorded sayings as claims and a place named "the tavern" — the prompt now excludes both. |
| Why a model call failed | **fixed (proven)** | a planner call failed in 22 ms and left nothing to read; failed calls now record their exception types and HTTP status, never the exception text (`test_a_failure_is_described_by_type_never_by_its_text`, migration 025) — for every call kind, a cancelled call, and the Codex adapter's own HTTP errors (`test_every_kind_of_call_records_its_failure`, `test_a_codex_http_error_records_its_status`, added by the phase-end review). |
| `uro connect` prompt | **fixed (proven)** | the prompt came back at once, above the streaming narration, and was gone when the beat committed — the owner retyped an intent and played it twice. It now waits for each beat to settle (`test_the_prompt_waits_for_each_beat_to_settle`). |
| Leg 4 baseline (`relational_live.py`, 15 beats) | **recorded** | 3 places, a faction with membership edges, 2 named actors, 1 thread; no claim dropped as "not durable"; 5 beats recorded nothing (beat 12 lost what the strangers said they want). The before-image for replaying the prompt changes. |

**Replayed (2026-10-31, `scripts/extraction_replay.py`, `gpt-5.6-luna`, after #132).** Each world's
recorded narration replayed onto a fork taken just before its first beat:

| World | Original → replay | Read |
|---|---|---|
| War story (Leg 3) | Mera's retelling: a new claim at 0.8 → **nothing new**; her belief stays 0.272 with its `learned_from` trail. Sayings 3 → 0; "the tavern" place gone; new hearsay recorded at 0.25–0.35. | fixed — on a different model than the original's `gpt-4o-mini` |
| Ashfall (Leg 1) | the two dropped news claims **kept**; claims 14 → 15; **threads 2 → 0** | the news instruction said "record it as a dialogue claim", and both original threads had started as reported news; reworded to say news that raises a plot is also a thread |
| Relational (Leg 4) | beat 12's gap filled; beat 8's rumors become the barkeep's testimony, not narrator facts; thread 1 → 1; claims 13 → 24, one fact **three times** in new words | the repeats named no subject ("the Watch"), so recall never surfaced them to the extractor; it now also sees the branch's latest 15 claims (`test_the_extractor_sees_the_latest_claims_whatever_their_subject`) and is asked to name a claim's subject as the KNOWN lists do. |

**Replayed again after #134 (2026-10-31, same model):**

| World | Round 1 → round 2 | Read |
|---|---|---|
| Ashfall | threads 0 → **3** (the boats, the Duke's plan, the woman asking after the PC); both news claims still kept; claims 15 → 17 | threads restored (the original had 2) |
| Relational | "the Watch says it's frightened talk": 3 rewordings → **once**; beat 12's gain kept; claims with no subject 12 → 7 of 24; faction named "River Watch" consistently | repeats fixed; more of what characters say is recorded than in the original (13 claims), some of it low-value ("no one agrees on what is happening") — testimony, `truth=unknown`, but it grows the state; watch it |

Cost: the latest-claims context adds about 10–13% extractor input tokens; mean extractor latency
did not rise (4.5–5 s). One replay per world — thread creation in particular varies run to run.

**Phase-end review (2026-10-31, targeted at #129–#135, 9 Opus agents).** 11 confirmed + 1 from the
critic, all fixed: `uro connect` released its prompt on ANOTHER player's identical intent (intents
now carry an id the server echoes as `intent_id`); a second replay picked the first replay's fork as
its source; the recent-claims window was cut before dropping module rumors and re-recorded claim
ids, so a reaction pass or a replayed bundle could empty it; a cancelled call was recorded as a
success; Codex HTTP errors carried no status; the lifespan skipped `on_event` hooks an embedder
attached and could skip `shutdown` (the store close) when the feed's close failed; an in-place
time-skip's AdaptationApplied still said `fork-timeskip`; and this ledger had called the rumor fix
"structural" (corrected above).

## Post-PoC — the second live pass (2026-10-31, v0.8.0)

What the first pass didn't cover (runbook `docs/live-run.md` §9): a two-player party, Uro Pool, a
world rule in live play, and the extractor on `gpt-4o-mini` (by replay).

| Capability | Status | Evidence |
|---|---|---|
| A two-player party over WebSocket | **live-validated (two sessions)** | `uro serve` with two tokens: player-1 and player-2 alternated strictly over six beats, each as their own PC. The "not your turn" path wasn't exercised live — player-2's identical intent arrived 1.4 s after player-1's beat committed, a legitimate turn; it stays covered by `test_another_players_identical_intent_never_settles_ours`. Second session (2026-11-01): two scripted clients alternated six beats; player-2's out-of-turn intent got `not_your_turn` with its own `intent_id`, and each client received the other's commits. |
| A world rule in live play, voiced by the narrator | **live-validated** | Ashfall's `failures-get-talked-about`: player-2's failed sneak committed the module rumor, and asked what people were saying, Mera answered "someone tried their luck down there and failed". First reaction ever to fire in a live session. |
| Physical risk rolled | **found live → fixed (D-69)** | a forced lock (Uro Basic) and a row through rising surf (Uro Pool) went unrolled: no built-in affordance covered physical danger, so the narrator decided (Basic) or never resolved (Pool) the outcome. Every built-in now declares a `danger` affordance (`test_every_builtin_rolls_for_physical_danger`). |
| Uro Pool in live play | **played, mechanics unexercised** | five beats, no check could fire (above); the pool, push, strain and wounds remain proven only by deterministic tests until a pass with `endure`. |
| Extractor on `gpt-4o-mini` | **instructions don't hold** | replayed: the retelling recorded again (0.7), the three sayings, "the tavern", passing moments and one line of dialogue as narrator fact, no threads. Keep a stronger model on the extractor role; the structural backstops below don't depend on it. |
| Placeholders and common nouns as entities | **found live → fixed (structural, D-68)** | `located_in: "unknown"` created a place called "unknown" and the role "unknown" was stored; "the tavern" became a place (`test_placeholders_and_common_nouns_never_become_entities`). |
| A known person split by a new location | **found live → fixed (structural, D-68)** | the authored Duke Halbrecht and Old Weck were created again for being "in the Brinehouse" (inside Vel, where they're authored), and Mera split the same way. Location no longer splits identity — only a different faction does (`test_a_known_person_seen_somewhere_new_is_the_same_person`). |

**Phase-end review (targeted at #139–#140, 7 Opus agents):** 7 confirmed + 1 from the critic, all
fixed: with a place out of identity, a stated place no longer picked the right namesake (it is now
a tie-break among namesakes whose factions agree, `test_a_stated_place_still_picks_the_namesake_who_is_there`);
Ashfall's rule recorded the same rumor on every failed check and each copy reached the narrator
(each statement is now listed once, `test_a_repeated_rumor_is_listed_once`); the placeholder check
was untested for actors and factions (`test_capitalized_placeholders_never_become_entities`); and
docs said partial-name links need "affiliations" to agree (now factions), credited D-69 to D-68, and
attributed the D-66/D-67 review to D-68/D-69.

## Post-PoC — the third live pass (2026-10-31, v0.9.0)

Run by Claude this time, on the owner's request, with the model registry's credentials (decrypted
by the `URO_SECRET_KEY` in `.env`): scripted sessions through the real engine, the `gpt-4o-mini`
replay, and two live WebSocket clients against `uro serve`.

| Capability | Status | Evidence |
|---|---|---|
| Physical danger rolled (D-69) | **live-validated, all three rulesets** | Uro Pool `endure` fired on rowing the shoals, climbing the hull and hauling a seal (3 successes); Uro Basic `athletics` on a wall climb and a forced lock (2 failures); PbtA `act_under_pressure` on a bridge sprint (a partial, narrated as a cost). |
| Uro Pool in play | **live-validated** | a strike opened a simultaneous exchange: two rounds, Maren took 3 wounds and was out of the fight, she pushed once (+1 strain), and Wren earned a milestone. A free-roam push stayed unseen (every free-roam roll succeeded). |
| A world rule, repeated, shown once | **live-validated** | Ashfall's rule fired on both failed checks; at the Brinehouse Mera mentioned the rumor once. |
| "Not your turn" in a live party | **live-validated** | two WebSocket clients: player-2 sent player-1's exact words mid-beat and got `not_your_turn` with its own `intent_id`; player-1's frames carried player-1's. |
| A known person seen somewhere new (D-68) on `gpt-4o-mini` | **live-validated** | the Ashfall replay that had duplicated Duke Halbrecht and Old Weck now keeps one of each. The same replay still records passing moments as narrator fact. |
| **A change of state** | **found live → fixed (D-70), live-validated** | Wren broke the Drowned Barque's seal (a successful check; the narration says it snapped open). The extractor recorded "the hold has been opened" and flagged it as contradicting "the Barque is sealed" (true), so the gauntlet downgraded the NEW fact to `unknown` and kept the old one; the next two beats narrated the hold as sealed. The contradiction rule protects canon from the narrator, but it also stopped the world from changing. Now the extractor lists a changed fact in `supersedes`, and a fact the NARRATOR established is retired to false; authored, mechanical, external and module facts stay protected (`test_a_narrated_change_retires_the_fact_it_supersedes`, `test_only_a_narrator_fact_can_be_superseded`). Whether a model uses `supersedes` instead of `contradicts` is prompt-guarded; the guard on what may be retired is structural. Live after the fix (`gpt-5.6-luna`): in a fresh Brinehold session "the hold hatch is sealed" was retired when Wren tore the seal free, and two beats later Tobin "peers into the open hold". (Replaying the OLD narration can't show this: its later beats were written while the bug kept the hold sealed, so the replay faithfully superseded the change back.) A second check covered the remaining cases live: barring the hatch again with no roll retired "open" (history: sealed → open → sealed, the old two false) and the next beats kept it barred; Tobin's warning about axes stayed testimony and retired nothing; and on a copy of Ashfall with an authored "the cellar door is kept locked, and only Mera holds the key", narration of the door standing open was refused as superseding a protected fact (recorded `unknown`) while the narrator stayed consistent (Mera unlocked it with her key, and later confirmed she alone holds it). A shutter opened and closed was never recorded at all: the extractor treats it as a passing state. Found on the way: recall showed any claim retracted to false — including a rumor C5's `expire_claims` had retired — as an "unverified rumor"; retracted claims are no longer recalled. |
| Narration beyond the mechanics | **observed, minor** | a failed Uro Basic climb was narrated as a shoulder injury no mechanic recorded; a round that dealt 2 wounds was narrated "without drawing blood"; asked to speak with River, the narrator answered through an unnamed digger. |

**Phase-end review of D-70 (targeted at #144–#147, 5 Opus agents):** 5 confirmed + 2 from the
critic, all fixed: a superseding claim downgraded by a protected fact still retired the narrator
fact it superseded, leaving nothing true (the new claim's truth is now settled first,
`test_a_downgraded_change_retires_nothing`); a fact retired and restated in one beat recorded no true
claim (`test_a_fact_retired_and_restated_in_one_beat_stays_true`); a retired fact or an expired rumor
still reached the narrator through an on-stage character's belief
(`test_a_retracted_claim_never_reaches_the_narrator`); the T2 proxy counted a superseded fact as a
failure (`test_a_superseded_fact_does_not_count_against_consistency`); and the extractor's
trust-boundary statements (its docstring, the whitelist rows in docs/12 and this ledger, CLAUDE.md)
still said it had two bounded mutations — retiring a narrator fact is the third.

## Post-PoC — headless CLI providers (2026-11-01, D-72)

| Capability | Status | Evidence |
|---|---|---|
| `claude_cli` / `codex_cli` run the logged-in CLIs | **live-validated (two sessions)** | three Ashfall beats with narrator/dialogue/planner on `claude -p` and the extractor on `codex exec` at `low`: claims extracted, a check rolled, the world rule fired, no errors. ~2× the HTTP providers' latency. Deterministic tests drive a fake CLI (`test_cli_providers.py`). Second session (2026-11-01, after the review fixes): five Brinehold (Uro Pool) beats with narrator/dialogue/planner on `claude -p` sonnet and the extractor on `codex exec` at `low` — four checks rolled, the broken seal recorded, every call metered with its model, no errors; told after 11–17 s, saved after 18–30 s. |
| The lockdown (tools off, a fresh empty directory per call, prompt on stdin, a scrubbed environment) | **proven (structural) + live-checked** | the flags are asserted in tests; live, a planted canary file outside the directory leaked through `codex exec` with only its read-only sandbox, and did not leak through either adapter (rechecked after the review, with the wider Codex feature list). |
| Codex's tools are off | **by construction for the features Codex 0.163 lists; backstop beyond** | each tool feature is disabled and checked against `codex features list` before the first call (a retired name refuses to run, tested). A tool behind a feature name added later isn't on the list: a tool event in the output fails the call (tested). |
| The CLI never sees Uro's secrets | **proven (test) + live** | the environment is an allowlist; a test plants `ANTHROPIC_API_KEY`/`URO_SECRET_KEY` and the fake CLI sees neither. Live, with bogus API keys in Uro's environment, both CLIs still answered on their own logins. |

## Post-PoC — extraction quality, measured (2026-11-01, D-73)

Three open quality items from the live runs, each measured before anything was changed. Replays use
recorded narration (`scripts/extraction_replay.py`): four tuning sessions (33 beats) and five
held-out ones (40 beats).

| Item | Status | Evidence |
|---|---|---|
| Atmosphere recorded as fact | **model-dependent → reduced (prompt)** | `gpt-5.6-luna` (the bound extractor): no atmosphere among its narrator facts, with or without the closing check (six sessions replayed with it: news, threads and lasting changes kept). `gpt-4o-mini`: ~24 atmosphere claims across the nine sessions without the check; with it, ~4 and ~6 in two runs (the five held out: ~14 without, 1 and ~4 with). Reported plans and rumors were kept (compared with the baseline on each held-out session; one rumor came back as a thread instead). Its sayings ("Names are poor currency in Brackenford") still become testimony claims, as before. |
| A character's quoted line recorded as narrator fact | **found → fixed (in code)** | `gpt-4o-mini` did it ~5 times per run. The quoted-speech check (as revised by its review) flags 10 of the instance's 183 historical narrator claims, all of them speech, and no others; quoted names, inch marks and wrapped lines are not speech. |
| Planner re-ask latency | **closed by data** | as of 2026-11-01, 100 planner calls against 101 narrator and dialogue calls since 2026-10-01 across all models (89 against 88 on `gpt-5.6-luna`): about one per beat, so re-asks are rare. |
| A borderline thread varies by run | **accepted** | two `gpt-5.6-luna` runs on the same narration record the same plots grouped differently (2 vs 3 threads; 1 vs 2). Merging similar threads could fuse distinct plots (D-51). |

## Post-PoC — typed API responses (2026-11-01, D-74)

| Capability | Status | Evidence |
|---|---|---|
| `/openapi.json` describes every response | **proven** | `test_openapi_describes_every_response`: all 56 routes (61 with D-95's `/pacing`, D-99's three removals and D-102's thread state) have a response schema, none a bare object. |
| A handler can't send an undeclared key | **proven (structural) for every shape a handler builds** | the envelopes, the state rows and the core models a route returns whole are strict (`additionalProperties: false`, asserted for every route's top-level response), and FastAPI validates every response; the whole suite reaches all 56 routes with a response (measured by recording each validated response). A core model nested in an envelope is a dump of itself and is not checked for extra keys. Mutations caught: an extra key, a dropped row column, an untyped route, a route back on a non-strict model, optional keys sent as `null`, the export losing `v`. |
| State rows match the projections | **proven** | `test_state_rows_match_the_projection_catalog` (models vs `_SNAPSHOT_TABLES`) and a real-store read of all eleven sections. |
| WebSocket frames | **documented, not in OpenAPI** | OpenAPI has no WebSocket model; docs/08 lists the frames and `test_server.py` pins their key sets (BE-11). |

## Post-PoC — the fourth live pass (2026-11-01, run by Claude)

Three surfaces that had one live session each got a second, after D-72's review fixes and with
D-73 in place. Run through the model registry (Codex `gpt-5.6-luna`; OpenAI embeddings) and the
logged-in CLIs; no role binding was changed.

| Capability | Status | Evidence |
|---|---|---|
| A party over WebSocket | **live-validated** | Ashfall, `uro serve` with two tokens, two scripted clients: six alternating beats; `beat_narrated` after 6–11.5 s, `beat_committed` after 11–19 s; an out-of-turn intent got `not_your_turn`; each client saw the other's commits. |
| Extraction on live narration (D-73) | **live-validated** | the party session recorded Mera's and the dockhands' news as testimony and only lasting changes as narrator fact (drag marks under the pier, a snapped watch-lantern, a barrel lid torn off); no atmosphere. |
| The CLI providers | **live-validated** | a Brinehold session on `claude -p` + `codex exec`, after the D-72 review fixes (scrubbed environment, Codex feature check): five beats, four Uro Pool checks, the seal's opening recorded, all calls metered. |

## Post-PoC — recall of callbacks (2026-11-01, D-75, OQ-3 narrowed)

| Capability | Status | Evidence |
|---|---|---|
| Thematic callbacks recalled | **proxy (measured offline)** | a benchmark of 7 facts planted in a live session × named/thematic/oblique callbacks, ranked among 172 played beat memories: 20/21 in the top 4 with the intent embedded (18/21 narration-only). One session's plants; ranks measured, not the narrator's use of them. |
| Memory slots outside the recency window | **proven** | `test_recent_beats_never_starve_the_memory_slots` (it fails on the old code); before, mid-scene queries kept 0-2 of 4 memories (measured). |
| A word only the intent said recalls the beat | **proven** | `test_a_word_only_the_intent_said_recalls_the_beat` (the live fear case, deterministic). |
| `uro memory reembed` on a mixed instance | **guarded** | a memory whose vector has another dimension than the embedder's is skipped and reported, never mixed in (`test_reembed_never_mixes_two_dimensions_on_a_branch`); the same dimension from a different model can't be detected, so run it on the worlds played with the registry's embedder. Metered (`test_uro_memory_reembed_is_metered`). |
| Recall through the engine, after the build | **live-validated (one session)** | `uro memory reembed` on the benchmark session's world (20 re-embedded; a second run 0), then all 21 callbacks through the engine's real `_recall`: 21/21 recalled, 4 of 4 memories delivered on every query (2.56 before the slot fix), no intent text shown to the narrator. Per session, which is what the engine searches; the 172-memory scale figure is the offline one above. |
| Old memories re-embedded | **proven (operator step)** | `uro memory reembed` (`test_reembed_moves_old_memories_to_the_intent_vector`: correct, idempotent, scoped to a world). Until it runs, a mixed index recalls old memories worse (16/21 measured). |

## Post-PoC — off-screen simulation (2026-11-01, D-76, OQ-4 narrowed)

| Capability | Status | Evidence |
|---|---|---|
| The world moves while nobody watches | **proven (deterministic), at explicit time-skips only** | `test_the_world_moved_while_you_were_away`: a 30-day skip on Ashfall heats the Saltborn feud once, surfaces the salt-road plot, and carries a rumor from the docks to a roadhouse two hops inland, in the next region (0.9 / 0.495 / 0.272, stopping at the floor), once — the next week's skip heats the feud again without re-telling it; before the skip the narrator can't tell it at all; a fork from before the skip has none of it; export/import keeps it. |
| Rumors spread in ordinary play | **only where a world defines it** | along `knows` edges a pack authors (seeds, or a rule's/agenda's `add_edge`; the extractor never adds one), from a world rule's or agenda's `spread_belief` or a Chronicler outcome's witnesses. A rumor an NPC tells the player, or a deed witnessed in a normal beat, spreads to no one (deferred, D-76). |
| A re-told rumor | **overwrites, by design today** | each `spread_belief` (and each Chronicler outcome) seeds its witnesses at 0.9 and re-tells the chain, upserting every belief on it — a stronger belief can be lowered, and a dead witness still "sees" it. Packs keep a spread one-off themselves (Ashfall gates its own on a counter). |
| Agenda outcomes never contradict | **by authorship, not checked** | no LLM generates an outcome, so a contradiction between places can only be the pack's own (two agendas setting one thread to different states both commit, the last wins). |
| The clock follows play | **solo play, since D-77** (below) | until D-77 only an explicit time-skip moved it (one ever, measured); play rarely passes time (~3 of 209 narrations). |
| `world_time.segment` | **schema-only** | never written: every stored event says "morning" (3,692 of 3,692). |
| Per-event day stamps | **day 0 on ordinary beats, display-only (settled, D-85)** | beat events are stamped day 0 whatever the clock says; every reader that computes with a day uses the chain's maximum day (`current_world_time`), and `created_day` is meaningful only on module claims and counters (the rules gauntlet fills it). The per-event day in `/events`, commit detail and export, and `created_day` on non-module claims, are 0 and display-only — the earlier "every reader uses the chain's maximum day" overstated it. |
| Time in the prompts | **a gap after a skip is told (D-84); the day itself isn't** | the first beat after the clock moved outside play gets a TIME PASSED line beside the intent (below); no prompt is told the day number, season or segment (settled in D-84: 0 day-part contradictions in 292 narrations). `[calendar]` is parsed and validated but not consumed (not in WorldGenesis, lost on export). |
| Rule files read what they hold | **proven** | a key `rules.yaml`/`agendas.yaml` doesn't read is refused (`test_an_agenda_written_into_rules_yaml_is_refused`); a `knows` ref to nobody is refused (`test_a_knows_ref_to_nobody_is_refused`). Faction/location refs on seeds are still not checked (pre-existing). |

## Post-PoC — the long run (2026-11-02, run by Claude, v0.13.0)

One Ashfall campaign of 146 beats in four acts with 7-, 7- and 14-day time-skips, through the model
registry (Codex `gpt-5.6-luna` for every chat role, `text-embedding-3-large`). Scripted beats
measured; an LLM "player" played the other 110 toward each act's goal, told not to mention the
planted things. 852 calls, no errors, 15.1 s a beat, ~37 minutes. 12 facts were planted (acts 1-2)
and called back 56-122 beats later with thematic or oblique intents (acts 3-4).

| Capability | Status | Evidence |
|---|---|---|
| Recall of callbacks at campaign scale | **live-validated (one campaign)** | the planted fact reached the narrator in 12/12 callbacks. The plant's own beat ranked in the top 4 for 10/12 (mostly 1st or 2nd among 70-125 memories outside the window); the other two came through claims. Every callback got 4 of 4 memories (D-75). |
| The narrator USES what recall brings | **partial: 8/12 (before D-78; see below)** | 8 used the planted detail (some in other words: the map as "the salt-stiffened chart… Grey Shoals"), 1 partly (the sword's notches, not its name), 3 not: it misread the letter errand (sent the player east, though "a sealed letter must be delivered to Orla in Vel" was an active thread in the prompt), sidestepped the nickname, and never tied the fear to Weck or the brother. The gap is narrator adherence, not retrieval. |
| The rumor across a skip, in play | **live-validated** | after the first skip Brannoc told it hedged ("Only talk… no one who saw it has come in to swear by the tale"); beliefs Weck 0.9, Hesk 0.495, Brannoc 0.272 as designed. |
| Bystander knowledge | **narrator-improvised, then recorded** | Ysolde, who holds no belief, told a garbled variant of what Brannoc had just said in the same room; the extractor then recorded it as her testimony and belief. Old Weck, 83 beats after witnessing Mera's tale, retold it consistently, from recall. No harm to play observed. |
| Time passing in play | **measured: narrated every time, recorded never** | the narration passed time on all 8 beats where the player slept, waited, travelled or rested, and on 1 of the other 138 (where the intent said "at first light"); none of it came from speech. The fiction ran ~36 days; the clock, moved only by the skips, recorded 28. This is the labelled data the D-76 clock gate asked for. |
| State over a long campaign | **healthy, one watch item** | 13 actors (no duplicates), 128 claims (30 narrator facts, all lasting clues, no atmosphere), 146 memories. 9 plot threads, all still live, none resolved: player commitments (the promise, the letter) became threads, and scene-level ones accumulate. Every thread reaches every narrator prompt. |

## Post-PoC — the clock follows play (2026-11-02, D-77)

| Capability | Status | Evidence |
|---|---|---|
| The clock follows solo play | **proven (deterministic); live-validated on one campaign (the confirmation run below)** | `test_a_night_in_play_moves_the_clock`: "Then I sleep until dawn" commits day 0 → 1 as its own commit after the beat, caused by the player, the sentence as evidence; speech, a promise, a plan and someone else's night commit nothing; a fork before the advance stays on day 0. `test_days_in_play_fire_the_weekly_agendas`: crossing day 7 in play fires Ashfall's weekly agendas, and a fork and a re-import agree. Re-driving the long run's intents with its skips (stub provider) ends on day 35; the fiction ran ~36 and the clock said 28. |
| The intent cue grammar | **measured on a labelled fixture, English only** | `tests/fixtures/day_cues.json`, gated in CI: a batch scored blind found 16/22 day-passing intents with 0/30 false positives; after one more widening 21/22 (no longer held out); the first targeted set 36/38 and the long run's 7 days are in-sample. 0 fires on 300 generated play intents, 154 of the instance's own and 47 adversarial rows (after its review: single-quoted and whispered or read words, wishes, questions, past tenses, and day counts that date or describe rather than last); days exact on every cue found. A cue it misses passes no time (the narration still may), as before D-77. |
| A clock moved mid-beat | **proven (compare-and-set)** | `advance_day` commits only if the branch is still on the beat's starting day (`test_a_clock_moved_mid_beat_isnt_stacked_on`); a skip's agendas read their window from the skip's own commit, not a read before the lock (`test_a_skip_reads_its_window_from_its_own_commit`). |
| Party, fight, bare, no seat | **pass no day** | `test_a_party_a_fight_and_bare_play_pass_no_day`: a party passes time with a skip; a fight's beat never does; `--bare` never reads a cue; neither does a participant without a seat — an unbound co-player driving the solo PC, or anyone on a campaign with no PC (fixed by its review). A party's day moves by the game's `Engine.pass_time` or an operator skip (D-83); a party played only over `uro serve`, with no embedding game, has no player-side way to pass time (named residual). |
| Rest in play | **not a ruleset's rest** | no downtime runs on a night in play (Uro Pool's strain stays); downtime stays with explicit skips. |
| `world_time.segment`, per-event stamps, time in prompts | **see D-84/D-85** | the segment is still unwritten; stamps are day 0 on ordinary beats (settled, D-85); the day isn't told, but a gap after a skip is (D-84). |

## Post-PoC — narrator adherence and thread accumulation (2026-11-02, D-78, D-79)

Measured live through the registry (Codex `gpt-5.6-luna` narrating and judging, `gpt-4o-mini` for one replay arm), on forks of the long
run's campaign taken just before each beat; every arm narrated the same sample's context.

| Capability | Status | Evidence |
|---|---|---|
| The narrator uses what recall brings | **live-validated on one campaign and one model (D-78), by policy** | the planted detail used in 53/60 design-callback samples (A2) against 35/60 and 34/60 for today's prompt twice; 21/25 against 16/25 on five fresh plant/callback pairs with oblique callbacks; the nickname said 5/5 against 1/5. The letter callback stays 1/5: the link it needs (the carter is Hesk) isn't in the data. One callback gained a `no` once (an invented sign); the baseline's second run had two. |
| Ordinary beats with recall beside the intent | **a small rise, measured at scale** | 12 ordinary beats × 3: unrelated earlier detail dragged in 9/36 against 11 and 3; a small unstated act given to the player (keeping to cover, a plank shifting under the hand) 5/36 against 1 and 2. Over a whole campaign (the confirmation run below, same judge on both long runs' ordinary beats): 14/110 against 11/110. 0 leaks of the header or marker. |
| Old facts re-recorded when a narration brings them back | **cut by D-82: 14 → 2 restatements over 60 callbacks** | over 60 callback narrations the extractor re-recorded a known fact in other words 14 times (A2, in 12 of the narrations), 14 and 13 (today's prompt): the old claim wasn't among its KNOWN CLAIMS, which were built from the intent alone. With KNOWN built from the intent and the narration (D-82), 2 (in 2 narrations); ordinary beats 9 → 8. 0 quoted player lines recorded as narrator fact. |
| A pack overriding the narrator template | **gets the recall header and layout** | built in code (`test_a_pack_overriding_the_narrator_template_keeps_the_recall_header`). |
| The ACTIVE THREADS block carries callbacks | **measured: yes, commitments** | without it, 25/36 callback samples used the planted detail against 28/36, and the letter fell from 3/3 to 0/3 (2 `no`). |
| Thread accumulation | **measured, nothing shipped (D-79)** | 9 live threads, none resolved, after 146 beats; the resolutions were correctly zero (nothing was settled). A creation-side fence in the extractor prompt, replayed twice per model, over-corrected on `gpt-5.6-luna` (8 and 6 threads → 2 and 3, the soldiers and hauling plots lost) and added fragments on `gpt-4o-mini` (3 and 3 → 6 and 6). The narrator-side cap stays shelved with its trigger (D-79). |

## Post-PoC — the confirmation run (2026-11-02, D-77 + D-78 live, run by Claude)

The long run's script again, on a fresh Ashfall campaign with all of this live: the same plants, callbacks, time intents and skips (7, 7,
14), with an LLM player for the other 110 beats (so a different story). Codex `gpt-5.6-luna` for every chat role, `text-embedding-3-large`.
852 calls, no errors, 15.2 s a beat, ~37 minutes.

| Capability | Status | Evidence |
|---|---|---|
| The clock follows play, live | **live-validated (one campaign)** | 6 advances, 7 days, all on the scripted time beats; every one landed on a beat whose narration reached a new day (0/6 refused, by a lexical detector over the narrator's own words that finds 5/7 of the first run's crossings with 0 false positives). No narration crossed a day the intent didn't (0), and no advance came from quoted speech. The clock ended on day 35 (28 skipped, 7 played) against 28 before; play's crossing of day 21 fired the weekly feud agenda (`war_heat` 4 = three skips + one from play). |
| Callbacks used, at campaign scale | **10/12 (one campaign)** | the same judge on both long runs: 10 yes, 1 partial, 1 no against 8 yes, 4 partial. The `no` (the brother): recall had the plant, the narrator evoked a drowning without naming Tomas. |
| Small unstated acts on ordinary beats | **11/110 → 14/110** | the same judge over every ordinary beat of each run. |
| Threads | **4 (2 emergent), not comparable** | the first run had 9: a different story (the LLM player's beats), and nothing about thread extraction changed. The promise became a thread; the letter didn't, and its callback was used anyway. |

## Post-PoC — the clock's ownership (2026-11-03, D-80)

| Capability | Status | Evidence |
|---|---|---|
| A game passes time in play | **proven (deterministic)** | `test_a_game_passes_time_and_the_world_reacts_on_every_path`: `Engine.pass_time` commits its own TimeAdvanced (caused by `system`, no cap), the world's rules and agendas react, a stale `based_on_day` commits nothing; REST `downtime: false` and `uro campaign time-skip --no-downtime` take the same path and rest nobody (`test_rest_management`, `test_cli_time_skip_rests_a_uro_pool_pc`). |
| A world keeps intents off its clock | **proven** | `[clock] from_intent = false` (`test_a_world_can_keep_intents_off_its_clock`, `test_a_pack_declares_its_clock`): no intent moves the day, a dry run shows no advance, `pass_time` still does; the setting travels in WorldGenesis through export/import; a default pack stores an empty `clock` (no setting), and an older genesis without the key reads as the default. |
| Rules triggered on TimeAdvanced | **live on every path** | they fire on a skip, an intent's days and `pass_time`, told apart by `reason` (`time-skip`, `play`, `game`, `operator`); before D-80 no engine path that moved the clock ran them (only an embedder's own `append_and_react` of a TimeAdvanced did). A rule and an agenda sharing an id is refused (`test_a_rule_and_an_agenda_cannot_share_an_id`). |

## Post-PoC — the open questions worked through (2026-11-03, D-83..D-94)

Each remaining open item was measured and then built or settled with a recorded decision (the owner's
"do them all"). Design passes: one measured proposal per item and an adversarial judge per batch.

| Capability | Status | Evidence |
|---|---|---|
| A gap after a skip reaches the narrator | **live-validated (A/B), D-84** | on the first beat after a time-skip, `pass_time` or a time-skip fork, a TIME PASSED line beside the intent says how many days went by (only the count comes from state; ordinary beats are unchanged, `test_with_no_gap_the_narrator_prompt_is_unchanged`). A/B on both long runs' post-skip beats, 6 fork points × recorded and neutral intents × 5: the narration treated the gap as passed 43/60 with the line against 18/60 without, ahead at 5 of 6 fork points, and never overrode the player's action (0/60 vs 3/60). The gain is on neutral intents (30/30 vs 7/30); when the player's own intent continued the old moment, the narrator followed the player (13 vs 11 of 30). |
| Closed-tier provenance | **proven (D-89)** | see the emitter-whitelist row above. |
| A cataclysm's ripple | **authored (D-88)** | see the adaptation-ripple row above. |
| Probes | **D-92** | the refusal verdict is read from its first word ("not refused" no longer warns); each distinct planner/extractor model and a separately bound dialogue model is probed; a judge on the narrator's model is labelled self-judged. |
| The container image | **built, owner-activated (D-94)** | a `Dockerfile` for `uro serve` (the store stays external; nothing secret baked in; non-root) and a dispatch-only `image.yml` that pushes to ghcr only when the owner runs it. Built and smoke-tested locally; never pushed. |
| Settled, nothing built | **D-83, 85, 86, 87, 90, 91, 93, 94** | party time / per-PC pacing (OQ-5); per-event day stamps and rest in play; role-noun recall (7 of 11 matches wrong); bystanders and contact graphs (a mention-based presence signal ~10% right); background extraction (the tail ends before reading does on 143/144 beats, a modelled reading speed); economy (OQ-6); cross-world canon (OQ-10) and the entity set (OQ-1); the narrator thread cap (largest live set 10 threads, ~270 tokens, under its trigger). Each has its reopen trigger in its decision. |


## Post-PoC — the v0.15.0 long run (2026-11-03, run by Claude)

The long run's script a third time, on a fresh Ashfall campaign under v0.15.0 (D-80..D-94 live, D-84's
TIME PASSED line among them): the same plants, callbacks, time intents and skips (7, 7, 14), an LLM player
for the other 110 beats (a different story again). Codex `gpt-5.6-luna` for every chat role (the extractor
at the default effort), `text-embedding-3-large`. 873 calls (117 of them the harness's player), 13 errors (Codex overloads, one HTTP 504):
the harness retried each failed beat after checking nothing had been saved (none was), and 2 beats whose
extractor call failed were committed narration-only, as designed. 17.2 s a beat including the retries.
The same judges as the confirmation run, applied to all three runs.

| Capability | Status | Evidence |
|---|---|---|
| Callbacks used | **9/12, no `no` (one campaign)** | 9 yes, 3 partial, 0 no, against 10/1/1 and 8/4/0. Recall brought the plant back for 10; 9 of those were used in full. The 3 partials: the brother and the mark were retrieval misses on lines that name neither ("why I really came to this coast": the plant's beat ranked 5th of 78 memories with 4 returned, the four above it general harbour scenes at cosine distance 0.58-0.63 against its 0.63; "the sign I left at the roadhouse": 13th of 130). The brother's line still went unnamed although "the night Tomas drowned" was an active thread in the prompt. The fear's plant did come back (2nd) and the narration brought back the fear, not Tomas. Where a callback falls in the ranking moves with each run's wording: the confirmation run placed the same two plants 7th and 6th. |
| A gap after a skip, in play | **3/3 against 1/3 and 1/3 (one campaign each)** | the first beat after each skip treated the days as passed (the same judge over all three runs; more lenient than D-84's A/B judge, it counts opening on a new moment). After the 7- and 14-day skips the narration named the count ("Seven days later", "fourteen days have passed"), which only the TIME PASSED line could tell it. Confounded like D-84: the beat after the first skip is a scripted travel intent and passed in every run, and this run's LLM player happened to write neutral intents after the other two (earlier runs continued the old moment, where D-84 found the narrator follows the player). |
| The clock follows play | **as the confirmation run** | 6 advances, 7 days, on the same 6 scripted time beats; 0 refused by the narration; day 35 at the end (28 skipped, 7 played). The lexical new-day detector flagged 3 beats with no advance; all 3 were false positives ("you rise" from a crouch twice, and a dawn the previous beat's sleep had already counted), so 0 real misses. |
| Small unstated acts on ordinary beats | **13/110** | against 11 and 14. |
| State | **healthy** | 14 actors (no duplicate names), 142 claims (46 narrator facts, 50 dialogue, 42 from the world's rules), 9 places, 146 memories. |
| Pacing (evidence for OQ-2's director half) | **measured, three runs** | Threads: 6 live, 0 resolved; across all three runs (438 beats) no thread resolved. By a judge's labels of which threads each beat brought into play (one call per beat, D-95's data): some live thread had gone 25+ beats untouched on 102, 54 and 102 of the 146 beats (first, confirm, this run); the longest gaps were the Brinehouse night (105 beats, first run) and the authored Saltborn ritual (125, this run, from the start); at the end 2 of 9, 0 of 4 and 0 of 6 had been idle 40+. (This row first counted a thread as touched when the narration shared three of its stakes words, which gave 5/9, 2/4 and 3/6 idle 40+; that proxy misses paraphrase, so it overstated idleness.) The letter errand (a thread from beat 41) was never delivered: its callback at beat 97 set out to do it and the next beat turned to a dark guard post. Beats that committed no new state: 80/146 (90 and 84 before), longest run 9 in all three. Consecutive near-repeats (content-word Jaccard ≥0.35): 0 in all three. Plants the story brought back unprompted (9+ beats after the plant, not on a callback, not named by the intent): 9 of 12, against 10 and 3 (the first run was before D-78; one run each, not an A/B). |
| Reopen triggers | **none fired** | narrator thread cap (D-79/D-94): 6 live threads, ~140 tokens, under >12 or ~500. Background extraction (D-90): extractor p50 3.9 s, p90 7.9 s, one call 45 s, under p90 > 12 s. Segments (D-84): 0 night-to-morning jumps without a rest or a time intent, a lexical proxy, under ≥2 per 150 beats. |

## Post-PoC — the pacing director (2026-11-03, D-95, OQ-2's director half)

In a world that opts in (`[pacing] quiet_after`, off by default since D-96; `worlds/ashfall` opts
in), a quiet plot is named to the narrator beside the intent, at most once every 10 beats, once it
has gone 25 beats without coming up. Which plots came up is recorded for every world. The line is
the world's prompt template `pacing.j2` (D-96). Live on Codex `gpt-5.6-luna` (narrating, extracting and judging);
the judge is the narrator's own model.

| Capability | Status | Evidence |
|---|---|---|
| The narrator takes a named plot up | **live-validated (A/B)** | 8 fork points where a thread had been quiet 25-109 beats × recorded and neutral intents × 5: brought back 44/80 with the line against 0/80 without, ahead at 8 of 8 fork points, the player's action overridden 0/80 on both, judged forced 0/44. Read by hand: the plot comes back as a rumor, a sign or a name inside the answer to the player's question. |
| Which plots went quiet | **measured; best-effort** | the extractor reports the KNOWN threads a narration developed (`develops`); against a judge's labels on 75 forked beats, precision 0.79, recall 0.59 (live, 0.62-0.65 and 0.38-0.56). Word overlap, shared names and embedding similarity scored F1 0.19-0.61 and couldn't aim the nudges. A thread the extractor misses reads as quiet, so a named thread is sometimes already in play (~35% in a simulation over the labels). |
| A pure function of the branch | **proven (deterministic)** | `test_pacing.py`: a dry run names the same thread as the beat; a fork names what the original would; spacing, ties and the off switch; `[pacing]` validated, refused misspelt, carried through export/import; `Engine(pacing=)`; the CLI and server reads; off by default with the signal still recorded, and a pack's `pacing.j2` rewording the line (D-96). |
| Over a stretch of play | **live, one fork, 40 beats per arm** | on: 4 named, 3 taken up, 0 overrides, 1 judged forced; 5 of 6 live threads came up against 3 of 7 off, 1 untouched all 40 beats against 4. Nothing resolved on either arm; the director keeps plots alive, it doesn't close them. |
| The extractor otherwise unchanged | **replayed, 2 runs per arm per model** | the v0.15.0 run's 146 narrations through main's prompt and the new one: threads 4/6 against 7/6 on Codex and 0 on every `gpt-4o-mini` run; claims 96/115 against 99/100 (Codex) and 227/242 against 221/231 (`gpt-4o-mini`); actors and places within a run's own spread. A +1.5-thread lean on Codex is inside the replay-to-replay range (D-79's baselines: 6-8), not separable; the D-79/D-94 thread-cap trigger watches for it. |

## Post-PoC — a genre-neutral engine, beat details, removal (2026-11-04, D-97..D-99)

| Capability | Status | Evidence |
|---|---|---|
| A non-fantasy world | **live-validated (one campaign)** | `worlds/halcyon` (a 2091 deep-sea station), 58 beats on Codex: 0/58 narrations judged out of the setting; callbacks 5/6 used; the clock passed a day on "sleep through the night cycle" and "rest until morning", none on "a twelve-hour shift"; titled people linked as aliases, the station AI was an actor. Found: a capitalized job title minted as a person (now refused, D-97) and plain-noun places split (deferred, D-97). |
| Genre assumptions in code | **removed, tested** | sufficiency needs one place of any kind and a faction or a tier-2+ actor (no role words); history seeding speaks the pack's `[history.vocabulary]`, its sentences included, and refuses without one, also over the API (`test_genre_neutral.py`, `test_seeding_a_pack_with_no_vocabulary_is_a_400`; Ashfall's seeding equals a fixture recorded from the old seeder); a fresh PC's gear is the world's; the role-noun guard refuses sentence-case titles as people only (`test_role_noun_detection`). Still fantasy-flavored: the extractor's and planner's examples (no measured harm). The guard reads letter case (English-like scripts). |
| What a beat did, for a client | **proven (deterministic)** | `beat_committed` carries each check's ruleset-graded outcome (never its trace: no sheet values reach a player), suggestions, mode, day and the entities the beat made, never claims (`test_beat_committed_carries_the_beats_details`); the real server beat runner yields the result last (`test_the_servers_beat_runner_yields_the_result_last`); `uro play` and `uro connect` print them, suggestions only under your own beats. |
| Removal | **proven (deterministic)** | `test_removal.py`: a world goes whole (every kind of row checked by id beforehand) and another world, sharing an embedding, is untouched; a branch takes only the commits it alone reaches (a child fork keeps its history); `main` and shared branches are refused for a campaign; over the API, operator-only with the same refusals; the CLI needs `--yes`; a beat committing during a world removal is removed with it, late memory and call writes for a removed branch are dropped, markers and snapshots on shared commits stay, and a removal closes the campaign's sockets and forgets its tokens (`test_removing_a_campaign_closes_its_sockets_and_forgets_its_tokens`). Kept by design: history a fork shares, the players' codex notes (until the world goes), calls metered with no world. |

**Language (D-101):** play is supported in English. The prompt templates, History's sentences and the clock policy are the game's to change; the engine's three code-built lines, the clock's day-cue grammar and the extractor's text heuristics are English-only until a game needs another language.

## Post-PoC — one-call playthroughs, rules first, and a plot's lifecycle (2026-11-04, D-100..D-102)

| Capability | Status | Evidence |
|---|---|---|
| A new game in one call | **proven (deterministic)** | `own_branch` forks the world (at its head, or at `from`) and starts the campaign there; a failed start leaves no fork on either surface; a player token, a reused name or an unknown `from` is refused (`test_a_playthrough_gets_a_branch_of_its_own`, `test_a_playthrough_from_a_marker_and_its_refusals`, `test_a_failed_own_branch_start_leaves_no_fork`); the playthrough is removable alone. |
| The rules' answer before the story | **proven (deterministic)** | `beat_checks` precedes the narration (`test_beat_checks_arrive_before_the_narration`, `test_the_rules_answer_comes_before_the_story`); only when a beat has a check or a fight. |
| A plot's lifecycle signal | **proven (deterministic)** | `developments` and `beats_live` follow the recorded beats; a fork, a replay and a v2 snapshot agree; a v1 snapshot is skipped, a newer one replaces it; a campaign from before migration 027 counts its past after `uro db rebuild-projections` (`test_a_rebuild_counts_a_campaign_played_before_the_counts`); a branch diff ignores the counts (`test_thread_lifecycle.py`). |
| The game sets a plot's state | **proven (deterministic)** | `set_thread_state` commits and the world's rules react (an authored plot too); REST operator-only, CLI (`test_a_game_moves_a_plot_and_the_world_reacts`). |
| World rules on a plot's counts; a beat's plot changes | **proven (deterministic)** | a `thread_life` rule wakes a finale once a plot is developed twice, and the beat that did it reports the change (`test_a_world_rule_closes_in_on_a_long_plot`); an extractor resolution is reported, a narration-created plot isn't double-listed (`test_a_beat_reports_the_plots_it_moved`). D-103. |
| A "bring it to its end" nudge | **measured, not shipped** | live A/B, 9 fork points: 22/90 against 6/90 (+18 points, gate +30), 0 overrides, 0 forced; it helps when the player's action engages the plot (20/45 vs 6/45), not on a neutral one (2/45). D-102. |

## Post-PoC — backfill for every gap, live (2026-11-08, D-108, run by Claude)

Two packs grown live through the registry (`gpt-5.6-luna`), imported into the scratch test
database. **Glassmere** (a name, three tone tags, a pitch: an original lake town that bottles
light) and **Kessler Ring** (a name and a pitch only: salvage stations round a dead moon) both went
`insufficient` → `runnable`: three nested places, a faction, three people placed by name (one of
standing), a conflict, tone tags where none were given, and four facts about the past that name
the people and places (2-3 linked refs each). Every place, person, faction, thread and claim
committed with `ai_backfill`. Found and fixed: roles came back as whole sentences ("She trims the
wick-cages at dusk…"); the ask now wants a one-to-three-word label, and the re-run gave
"first lamplighter", "bottle-sealer", "lake scavenger". One model, two packs: **live-validated
once**, not a measure of quality across models.


## Post-PoC — Anthropic structured outputs, room to think, inputs in the OpenAPI (2026-11-08, D-111)

| Capability | Status | Evidence |
|---|---|---|
| The planner's and extractor's schemas reach Anthropic as `output_config.format` | **stub-only** | `test_anthropic.py` (mock transport): sent only to the listed models, in strict form, never with a prefill, beside an effort; both engine schemas convert. Not yet sent to the real API, so whether it compiles them and whether replies improve is unmeasured. |
| A refused schema falls back, once | **proven (mock)** | a 400 then a success → the answer, and no schema afterwards; a 400 twice → an error and the schema kept; a 529 → no retry. |
| Thinking can't truncate a stage's answer | **stub-only** | 5.x models get 12,000 more `max_tokens` (asserted on the request body). Whether 12,000 is enough at high effort is unmeasured. |
| `/openapi.json` declares every input | **proven (structural)** | `test_openapi_describes_every_input` parses every handler (and the helpers that read for them) and fails on an undeclared query parameter or body field, a JSON body without a model, or a declaration for a route that doesn't exist. The declarations' types are read from the handlers by hand, not checked by the test. |
| Every `branch` input takes a name or an id | **proven** | one `_branch_ref` on every route; `test_world_log_takes_a_branch_id_as_well_as_a_name` (and a foreign world's id is a 404). |
| Play-made places, people and factions say `emergent` | **proven** | `test_relational_actor_cascades_faction_and_place`: on the events and on the committed rows. |
