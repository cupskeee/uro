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
| Structured (entity-triggered) recall | proven | word-boundary matching, dead-actor exclusion; `test_recall.py`. **Scope: actors/claims/beliefs only** — NOT place-state/active-threads (docs/04 overstated; reconciled). |
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
| Export/import + hash-chain verify | proven | `test_export.py`. |
| Bundle integrity | proxy | **keyless tamper-evidence**, NOT cryptographic authenticity (a re-derived internal chain). |
| Belief/rumor propagation (confidence decay, traceable) | **stub-only** | deterministic BFS tested; **never run with a live narrator**. Distortion = confidence decay only (statement garbling deferred). |
| Chronicler ingestion trust-scoping (D-32) | proven | protection ceiling / participant scope / ownership / testimony downgrade / caps / idempotent replay; `test_chronicler_hardening.py`. |
| Chronicler parked-encounter registry + per-campaign endpoint authority | deferred | "the full contract waits for a real external game"; the protection ceiling contains the damage without it. |

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
part of the forward event-catalog contract, marked reserved in docs/12: `ClaimTruthChanged`,
`ActorPromoted`, `TerrainChanged`, `PlaceStateChanged`, `EdgeUpdated`/`EdgeRemoved`. `ActorDamaged`
is **legacy** (replay-compat handler retained; the current harm path is opaque `SheetUpdated`, D-30).
Other reserved: `AdmitDecision.QUEUED` (proposal-window arbiter), `TEMPLATE_API_VERSION` (pack pin),
the transactional outbox + async event bus (docs/07 — the shipped model is inline-projector-in-txn),
`uro world delete` (privacy wipe), persisted probe reports, the full REST management surface, the
per-campaign expected-head concurrency guard, consequence-gating (D-21), the `entity_index` (OQ-3).

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
