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

**OQ-4 · Off-screen simulation depth** *(Actor engine)*
Simulate-on-observation is the MVP answer, but: how deep (agenda-step vs. full beat-sim)? Do faction-level agendas tick on world-time boundaries even unobserved (wars shouldn't freeze because nobody watched)? What stops interim-outcome generation from contradicting a *different* place's interim outcomes?
*For now:* T3 actors resolve on observation; faction agendas advance only at downtime/time-skip boundaries.

**OQ-5 · Mixed-pace time** *(narrowed — the calendar model is settled as D-22: absolute day counter + world-pack calendar config deriving years/seasons/eras)*
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
*For now:* adaptation pass only at fork/time-skip, scoped to entities within 2 edge-hops of the triggering event's refs.

**OQ-9 · Probe scoring rigor** *(narrowed — D-24 adds a `judge` role with per-probe rubrics, heuristic fallback, and transcripts attached to every report)*
Remaining open: rubric quality, and the circularity when the judge binding *is* the judged binding.
*For now:* prefer binding a different model as judge; transcripts always attached for human override.

**OQ-10 · Cross-world canon (the "multiverse" question)**
Should anything ever flow *between* worlds (shared pantheons, guest NPCs)? Gut answer: no — worlds are isolated universes; export/import is the only door. Revisit only if a consumer platform demands it.

**OQ-12 · Chronicler-mode ingestion contract** *(from: federation brainstorm, D-25)*
What does an external game declare and report, exactly? Domain-authority scope (what it may commit directly), outcome-bundle schema beyond v0, time mapping between the game's clock and `world_time`, distillation rules (game-declared feat thresholds vs. summarizer-role judgment), witness reporting obligations, and anti-abuse limits (a buggy or malicious game spamming legendary feats).
*For now:* the Phase 5 toy proof uses fixed bundle schema v0 + rule-based distillation; everything interpretive passes the standard gauntlet with witness-scoped beliefs. The full contract waits for a real external game.

---

**Placeholder constants** *(unvalidated defaults wearing lab coats — tune with play data, then promote to `decisions.md`)*: snapshot cadence N≈50 (`03`, `07`) · adaptation radius 2 edge-hops (OQ-8) · sufficiency thresholds (`09`) · role temperatures and the 0.5 confidence cutoff (`13`) · latency budget 2s / 10s / 300ms (`13`) · fact-consistency target ≥90% (`10`). Anywhere else a bare number appears without a decision reference, assume it belongs on this list.

---

*Process: when an OQ gets settled, move the decision to `decisions.md` with its rationale and delete it here. When a new genuinely-open design question appears, it gets an OQ number — not a silent inline choice.*
