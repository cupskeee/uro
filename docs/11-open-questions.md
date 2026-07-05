# 11 — Open Questions

The brainstorm backlog. The owner explicitly flagged several report sections as "not a fixed set / needs more brainstorming" — this file is where those live so they don't silently harden into accidental decisions. Each has an owner-of-record answer *for now* (what the code will do until the question is properly settled) so open questions never block development.

---

**OQ-1 · Entity set completeness** *(from: Data Models feedback)*
What's missing from `02-domain-model.md`? Candidates already suspected: languages, currencies/economy primitives, calendars/holidays, creature/bestiary types vs. actors, weather systems, player-visible maps as data.
*For now:* current entity set; add via events + projections as play reveals gaps (additive changes are cheap pre-Phase-2).

**OQ-2 · Pipeline shape** *(from: Generation Pipeline feedback — "not a fixed flow")*
Is plan→mechanics→generate→extract the right decomposition? Should extraction run inline (latency) or async post-commit (risk: player sees prose whose facts fail validation)? Do we need a distinct "director" pass for pacing/foreshadowing across beats?
*For now:* the `05` graph, extraction inline; instrument stage latency from Phase 0 and let data argue.

**OQ-3 · Recall selection** *(from: Retrieval and Memory feedback; the field's known-hard problem)*
Entity-triggered + top-k + recency will miss thematic callbacks ("the promise made in beat 12"). Candidates: planner-directed retrieval (planner asks questions, memory answers), periodic "salience" re-scoring, per-thread memory indices.
*For now:* the `04` heuristic; collect failure cases in a recall-misses log during Phases 1–3.

**OQ-4 · Off-screen simulation depth & belief diffusion** *(Actor engine)*
Simulate-on-observation is the MVP answer, but: how deep (agenda-step vs. full beat-sim)? Do faction-level agendas tick on world-time boundaries even unobserved (wars shouldn't freeze because nobody watched)? What stops interim-outcome generation from contradicting a *different* place's interim outcomes? And — the part the war-story test (`10` Phase 5, `15C`) leans on — how does a belief *spread* NPC-to-NPC so a tavern regular two regions away can retell a rumor, with distortion?
*For now:* T3 actors resolve on observation; faction agendas advance only at downtime/time-skip boundaries; **belief fan-out is tier-agnostic** — on observation, an actor may acquire beliefs circulating among its contact edges (`BeliefChanged`, emitter A), confidence/detail decaying per hop, so an ordinary T0–T1 NPC can carry a rumor. Diffusion depth/rate is a placeholder to tune with play data. (Scheduled as a Phase 5 deliverable, `10`.)

**OQ-5 · Mixed-pace time** *(narrowed — the calendar model is settled as D-22: absolute day counter + world-pack calendar config deriving years & seasons; named eras are event-driven, not arithmetic)*
How does world time behave when a post-MVP party splits pace (one PC in downtime while another plays beats)?
*For now:* a campaign has one clock; per-PC pacing is a multiplayer-era problem, adjacent to OQ-7.

**OQ-6 · Economy & items depth**
Prices, trade, scarcity — simulated, narrated, or ruleset-delegated?
*For now:* narrated flavor + ruleset-priced transactions; no simulation.

**OQ-7 · Multiplayer free-roam arbitration** *(explicitly deferred at D-9)*
Encounter mode self-arbitrates via initiative; free-roam with 4 players does not. Proposal windows? GM-player role? Consensus prompts? This is a design problem *and* a UX problem, and it's the first thing to brainstorm when multiplayer becomes real.
*For now:* `SoloArbiter` only.

**OQ-8 · History adaptation triggers**
After a major event, *which* threads/claims get re-evaluated? Full-world sweeps won't scale; entity-neighborhood graphs (blast radius via edges) seem right but need a distance metric.
*For now:* adaptation pass runs at fork/time-skip **and after a major in-play event** (e.g. a player-triggered cataclysm), scoped to entities within 2 edge-hops of the triggering event's refs. Note the two distinct writes: the *direct* consequence of a player action (the meteor itself) is a thread consequence emitted by History with `caused_by=player_action`; the *ripple* the adaptation pass then propagates (festival thread → dead, refugee thread → spawned) carries `caused_by=history, pass=adaptation`. This aligns OQ-8 with `01`/`03`/`12`, which always described adaptation as firing "after major events."

**OQ-9 · Probe scoring rigor** *(narrowed — D-24 adds a `judge` role with per-probe rubrics, heuristic fallback, and transcripts attached to every report)*
Remaining open: rubric quality, and the circularity when the judge binding *is* the judged binding.
*For now:* prefer binding a different model as judge; transcripts always attached for human override.

**OQ-10 · Cross-world canon (the "multiverse" question)**
Should anything ever flow *between* worlds (shared pantheons, guest NPCs)? Gut answer: no — worlds are isolated universes; export/import is the only door. Revisit only if a consumer platform demands it.

**OQ-12 · Chronicler-mode ingestion contract** *(from: federation brainstorm, D-25)*
What does an external game declare and report, exactly? Domain-authority scope (what it may commit directly), outcome-bundle schema beyond v0, time mapping between the game's clock and `world_time`, distillation rules (game-declared feat thresholds vs. summarizer-role judgment), witness reporting obligations, and anti-abuse limits (a buggy or malicious game spamming legendary feats).
*For now:* the Phase 5 toy proof uses fixed bundle schema v0 + rule-based distillation; everything interpretive passes the standard gauntlet with witness-scoped beliefs. The full contract waits for a real external game.

**OQ-13 · The second ruleset should be alien** *(from: cold-reader review F13 + the "D&D is vocabulary, not identity" brainstorm)*
The ruleset port (`06`) is validated by exactly one built-in — Uro Basic, a d20 system. One implementation proves *playability*, not *game-agnosticism*: a port shaped against a single d20 ruleset will silently leak d20 assumptions (ability-score-shaped sheets, `CheckResult`/`EncounterState` fields) into the "generic" contract, and nobody finds out until a structurally different system is attempted. The real generality test is a *deliberately alien* second ruleset — not Pathfinder (d20 in a hat), but e.g. a PbtA-style 2d6 system (2d6+stat vs 7/10 tiers, success-with-cost, no AC/HP) or a diceless resource-bid system. A narrate-only null ruleset does **not** count — it exercises no encounter/turn path, exactly where d20 leaks hide.
*For now:* one built-in (Uro Basic); the alien second ruleset is the genuine port-generality probe, deferred to when `srd51` or the first non-d20 consumer arrives (`10` post-PoC horizon). Port changes forced by that second ruleset are the signal that a d20 assumption leaked.

---

**Placeholder constants** *(unvalidated defaults wearing lab coats — tune with play data, then promote to `decisions.md`)*: snapshot cadence N≈50 (`03`, `07`) · adaptation radius 2 edge-hops (OQ-8) · sufficiency thresholds (`09`) · role temperatures and the 0.5 confidence cutoff (`13`) · latency budget 2s / 10s / 300ms (`13`) · fact-consistency target ≥90% (`10`). Anywhere else a bare number appears without a decision reference, assume it belongs on this list.

---

*Process: when an OQ gets settled, move the decision to `decisions.md` with its rationale and delete it here. When a new genuinely-open design question appears, it gets an OQ number — not a silent inline choice.*
