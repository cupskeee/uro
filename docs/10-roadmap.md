# 10 — Roadmap

Solo-dev PoC roadmap. No calendar estimates (the report's quarter/budget tables assumed a staffed company and are disregarded — D-14). Phases are strictly ordered by dependency; each has a demoable **acceptance test** and explicit non-goals. A phase is done when its test passes, not when its code feels finished.

## Phase 0 — Walking skeleton

*The thinnest playable end-to-end slice: prove the shape.*

- Monorepo scaffolding (`uro-core` / `uro-server` / `uro-cli`), Docker Compose Postgres, CI with tests — everything per `14-development-guide.md` (uv workspace, migrations, import-linter, recorded-provider test harness).
- Domain + timeline minimal core: events, commits, one branch, no snapshots/forking yet.
- Provider port + `openai_compat` adapter (covers Ollama for free) + role router with a single binding.
- Degenerate pipeline: context (recency only) → narrate → commit raw beat log. No planner, no extraction, no mechanics.
- `uro play` against a hardcoded fixture world.

**Acceptance:** play 20 coherent beats in a tavern over two separate CLI sessions; the second session resumes exactly where the first ended (events reloaded, not chat history).

**Non-goals:** branching, mechanics, world packs, server, structured state.

## Phase 1 — State that matters

*From chat log to world model.*

- Full entity projections (actors, places, claims, edges, beliefs), actor tiers T0–T2. *(inc 1: actors/claims/beliefs done; places/edges when needed.)*
- Extractor stage + validation gauntlet (whitelist/tier/contradiction), promotion rules. *(inc 2 done.)* **The LLM planner is deferred to Phase 3** (D-28) — Phase 1 does deterministic entity-linking for recall, since there are no mechanics affordances to route yet.
- Retrieval: structured recall *(inc 2 done)* + pgvector semantic recall *(inc 3 done)* + summarizer compression *(deferred — token-efficiency optimization, not needed for the acceptance test; lands with belief-strength/journal work)*.
- `anthropic` adapter + multi-role model bindings + thesis harness (ablation `--bare`, `uro consistency`) *(inc 4 done)*. **The live-model acceptance run + human-judged ablation is deferred pending an API key** — all engine code ships; only the paid run remains (commands above).

**Acceptance:** an NPC lies to the player; ten beats later, a *different* NPC contradicts the lie from `truth=true` state — and a claim first mentioned ~50 beats ago resurfaces correctly via recall.

**Non-goals:** combat, forking, off-screen simulation.

## Phase 2 — Timeline ★ the signature phase

*The reason this engine exists.*

- Snapshots, markers, branch-from-any-commit, materialization at arbitrary commits. *(inc 2.1 done)* Places projection (deferred from Phase 1) lands here too — the meteor needs `PlaceDestroyed` to be true on one branch and absent on a sibling.
- Fork semantics: carry/drop rules *(carry done, inc 2.1)*, adopt-existing-actor-as-PC, time-skip on fork (History adaptation pass — deterministic header in the PoC, not an LLM ripple). *(adopt-as-PC + time-skip: inc 2.2 done)*
- `uro branch fork`, `uro log` *(inc 2.1 done)*; campaign-over-branch plumbing (`uro campaign new/end`) *(inc 2.2 done)*.

**Acceptance: the meteor test** (`03-timeline-and-branching.md`) — one played campaign ending in a city-destroying event, then (a) continue as the same character, (b) new campaign as a farmer in the aftermath who hears NPCs retell campaign A's deeds as history, (c) a what-if branch from before the event. All three from the same event log, no special-casing. **Automated (deterministic, no key) in `packages/uro-core/tests/test_meteor.py::test_the_meteor_test` — PASSING.**

**Non-goals:** export packs, server.

## Thesis validation (runs alongside Phases 1–2)

The phases prove the *machine* works; nothing in them proves the *bet* — that state-tracked narration beats a long-context chat log. Two checks, cheap and mandatory (reinstated from the research report's experiments section, which these docs originally deleted):

- **T1 — Ablation.** The same scenario, world, and seed played two ways: (a) the full engine; (b) the same narrator model with a raw rolling transcript — no state, no recall, no extraction. Blind-compare transcripts (yourself + 2–3 volunteers) for continuity errors and preference at 30+ beats. **Kill criterion:** if (b) is indistinguishable, stop building and rethink — that's cheaper to learn at Phase 1 than at Phase 5. *Harness built (inc 4): `uro play <full-campaign>` vs `uro play <bare-campaign> --bare` is the A/B — the `--bare` flag is the exact ablation (no structured/semantic recall, no extraction, no memory).*
- **T2 — Fact-consistency metric.** Percent of narration-asserted, state-checkable claims per beat that agree with projections. Target ≥90% (placeholder, `11`); a downward trend after any pipeline or model change is treated as a regression gate. *Built (inc 4) as a **proxy**: `uro consistency <campaign>` = narrator-origin claims that survived as `truth=true` / all narrator-origin claims. Caveat (review inc 4): this only catches contradictions the **extractor self-flagged** against *recalled* state — it is not a full narration-vs-ground-truth verification, so read it as a regression trend, not an absolute. A real cross-check pass (verify each narration claim against all state) is future work.*

**Running the live experiment (needs an API key — not runnable in CI).** Bind a real model and play both arms; requires `ANTHROPIC_API_KEY` (or OpenAI/Ollama):

```sh
uro world new "Ablation A"   # → campaign A (full)
uro world new "Ablation B"   # → campaign B (bare)
uro play <A> --provider anthropic
uro play <B> --provider anthropic --bare
uro consistency <A>          # T2 on the full arm
```

Multi-model per-role bindings go in `uro.toml` (`[llm.roles] narrator = "anthropic:claude-sonnet-5"`, `extractor = "openai:gpt-4o-mini"`, `embedder = "openai:text-embedding-3-small"`); the `--provider` flag is the default for unpinned roles. The engine code for all of this ships in inc 4; only the human-judged comparison and the token spend are deferred to whoever has a key.

## Phase 3 — Mechanics

- Ruleset port + Uro Basic; mechanics gate stage; affordance-prompted planner.
- Encounter mode: initiative, turn loop, effects-as-events; mode transitions.
- Encounter completion is **async-capable from day one**: an encounter can be parked and later resolved by an out-of-band outcome bundle (the Chronicler-mode door, D-25) — even though this phase only uses in-process resolution.
- Seeded RNG discipline end-to-end; recorded-response replay for beat debugging.

**Acceptance:** a free-roam insult escalates into combat (mode transition decided by pipeline), a three-round fight resolves under Uro Basic with narration weaving real roll results, and a lost fight leaves persistent consequences (injury claim, item looted) visible in later free-roam.

## Phase 4 — Worlds

- World pack format: parsing, validation, **sufficiency check**, AI backfill (opt-in, provenance-tagged, committed via `create --backfill`). *(LLM lore-extraction deferred — authored YAML is the primary source.)*
- Prompt template packs with override-and-fallthrough; History seeding from manifest (`simulate_years`).
- **Capability probes** (report printed with transcripts; persisted/timestamped storage deferred); dry-run (`uro dry-run`, `preview_beat`).
- Two real example packs in `worlds/` (one rich, one deliberately thin — the thin one is the sufficiency-check fixture).

**Acceptance:** author a fresh pack from scratch, `validate` flags a missing conflict seed, backfill fills it (committed, tagged `ai_backfill`), `probe` passes, `seed --seed 42` then `seed --seed 43` produce visibly different histories on identical geography, and a campaign plays in the authored tone. *(Status: deterministic legs — validate/seed/tone/import — pass in CI; the LLM legs — backfill/probe — are stub-tested, live pass pending like Phases 1/3.)*

## Phase 5 — Server & sessions

- `uro-server`: full REST surface, WebSocket play channel, SSE beats, token auth mode.
- Session/participant model with `SoloArbiter`; CLI HTTP-client mode.
- Export/import packs (world, branch-at-commit) with hash-chain verification.
- **Off-screen belief/rumor propagation** (the war-story ripple depends on it; it is scheduled here, not assumed): Actor-service simulate-on-observation emits `BeliefChanged` fan-out along contact edges with per-hop **confidence** decay, **tier-agnostic** so an ordinary downstream NPC can acquire and retell a rumor (OQ-4). The confidence surfaces to the narrator as certainty phrasing (an eyewitness vs "has heard a rumor"), so a low-confidence belief is retold as a hedged rumor. This is also the roadmap's first build of off-screen agenda resolution. *(Statement-level garbling — a mangled retelling per hop — is a later refinement; the PoC distorts via confidence.)*
- **Proof of Chronicler mode:** the outcome-bundle endpoint, bundle schema v0 (participants, witnesses, casualties, notable feats, loot, duration), rule-based distillation into the standard gauntlet — and a ~50-line toy auto-battler script as the external "game."

**Acceptance:** two CLI clients (two tokens) attached to one campaign both receive the same streamed beats; a world exported from one machine imports and continues on another. Plus **the war-story test**: a toy external battle in which the PC's spectacular feat has surviving enemy witnesses — beats later, a tavern NPC retells it as a hedged, low-confidence rumor (the feat is recorded as `truth=unknown` testimony, not canon), with the belief chain traceable back to those witnesses; re-run the same battle with zero survivors and nobody ever mentions it.

## Phase 6 — The alien ruleset ★ the game-agnosticism proof (post-PoC, first up)

*The engine's core claim (D-1: game-agnostic) was backed by exactly ONE d20 ruleset. This phase settles OQ-13 → D-30 by adding a deliberately non-d20 second built-in and forcing every leaked d20 assumption out of the "generic" port.*

- **inc 6.1 (port generalization):** `rulesets/base.py` made game-agnostic — opaque `dict` sheets, a ruleset-declared graded `CheckResult.outcome` (replacing binary `success`), no DC in the port, opaque `EncounterState`, open action/effect kinds, harm via the ruleset's opaque final `SheetUpdated` (projector `{hp}` hardcodes deleted). `uro_basic` refactored to own its d20 shape (meteor + encounter replay stay byte-identical); `uro_pbta` added (2d6 vs 7/10, harm clock, moves, advance-by-failing). A 7-surface leak audit found 64 assumptions; building uro_pbta in lockstep forced each out (leak report in D-30).
- **inc 6.3 (registry & binding):** a ruleset registry resolves a pack's `ruleset = "id@version"` → a bound `Ruleset`; `WorldGenesis` records it, `CampaignStarted` pins id+version (migration 013), `play`/`dry-run` rebind from the campaign. `worlds/emberfell` is the PbtA example pack. (No 6.2 — the port generalization was one cohesive slice, not two.)

**Acceptance** (deterministic, no key): a PbtA campaign, bound via the registry, plays a conflict beat whose **7-9 partial success leaves a persistent, canonical consequence** (an `Exposed` condition / a filled harm clock) a binary d20 result cannot express — carried across a fork; d20 and PbtA rulesets coexist in one build with irreconcilable harm shapes (hp vs a clock) through the identical runner. **Automated in `packages/uro-core/tests/test_alien_acceptance.py` — PASSING.**

## Post-PoC horizon (unordered, deliberately unscheduled)

Multiplayer `PartyArbiter` (OQ-7, DONE — D-31) · full Chronicler-mode ingestion contract beyond the toy proof (OQ-12, hardened — D-32) · ~~module/scripting system for packs~~ (DONE as the declarative **Reaction Layer**, D-33/docs/17 — a pack ships `rules.yaml`/`agendas.yaml` data, no sandbox) · ~~computation/scripting for packs~~ (the engine-owned **computation layer** — event-sourced integer counters that fork by construction — shipped when that use-case arrived: **DONE, Phase 10 / D-34 / docs/19**; only a WASM/unbounded-computation tier stays reserved behind `ports/module.py` at a sharper gate) · graph/vector store swap-ins if scale demands · more rulesets (`srd51` — a d20 sibling; the alien-ruleset generality probe itself is DONE, Phase 6/D-30) · NATS-backed distribution · subscription-OAuth auth strategies if ever reconsidered (removed at D-16) · anything platform-shaped (which is someone else's repo).

## Standing engineering practices (all phases)

- Tests ride along, not after: domain logic unit-tested; pipeline stages tested with recorded LLM responses; each phase's acceptance test automated as far as LLM nondeterminism allows (assert on *events*, not prose).
- Every LLM call metered and stage-tagged from Phase 0 — retrofitting observability is misery.
- `docs/` updated in the same commit as the behavior it describes; decisions appended to `decisions.md` when made, including reversals.
