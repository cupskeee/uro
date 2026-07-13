# 00 — Vision and Scope

## Vision

Build a **game-agnostic, platform-agnostic RPG world engine** as a proof of concept. The engine drives persistent, AI-generated worlds in which:

- An LLM "Game Master" (composed of multiple cooperating modules) generates scenes, NPCs, dialogue, quests, and consequences on the fly.
- A canonical, queryable **world state** keeps the AI honest — facts, rumors, reputations, borders, and outcomes are recorded, not just narrated.
- **Every world has a timeline, and timelines can be branched.** A campaign's consequences persist beyond the campaign. New campaigns can continue a world's story or fork from any point in its history with different characters.

The proof-of-concept question the engine must answer: *can versioned, forkable world state plus a multi-pass LLM pipeline produce campaigns that feel like they happen in a real, continuous world — rather than in a chat log?*

## The engine/platform boundary

This is the single most important scoping rule. Uro is the **engine**; everything user-facing beyond a reference CLI is a **consumer** (a game, a web platform, a VTT integration) built on the engine.

**In scope (engine):**

- World state modeling, event-sourced persistence, branching timelines.
- The generation pipeline: context assembly, planning, generation, mechanics gating, narration, canonicalization.
- LLM provider abstraction: multiple providers, multiple auth styles, per-role model routing, capability probes.
- Pluggable ruleset interface + one minimal built-in ruleset.
- World definition format (files, not screens): manifest, lore, entity seeds, prompt template packs, content declaration.
- Lore import with a **sufficiency check** (warn when lore is too thin to run a world).
- **Dry-run mode**: run generation against a world without committing state — this is the "testing sandbox" from the research report, made concrete.
- Session management: single-player-first, but structurally multiplayer-ready.
- Portable export/import of worlds, campaigns, and branches.
- A CLI reference client for playing and debugging.

**Out of scope (platform concerns — the engine only provides primitives for them):**

- Graphical world-builder UIs.
- Community features: sharing libraries, asset marketplaces, ratings, forums, story-log publishing. (The export pack format and the chronicle projection are the primitives these get built on.)
- A module/mod marketplace or scripting sandbox for third-party code. (Revisit after the engine matures; prompt-template packs cover most creator customization for now.)
- User identity/social systems beyond minimal API auth.
- Content moderation policy. The engine is **content-agnostic** (see below).
- Frontends of any kind beyond the reference CLI.
- CDN/distribution infrastructure. (Noted as a platform must-have; nothing engine-side to build.)

## Two integration postures: GM mode and Chronicler mode

A consumer chooses how much authority Uro holds (D-25):

- **GM mode** — Uro runs the table. Play *is* the beat pipeline: the ruleset resolves uncertainty, narration is the interface. This is the default posture the rest of these docs describe. Suited to beat-driven, narrative-paced games.
- **Chronicler mode** — an external game owns its domain (a tactics battle, a 4X turn, a real-time raid) and Uro wraps it as the **world-memory and consequence layer**. Outcome bundles flow in, pass the same validation gauntlet as any untrusted input, become events on the timeline — and the epistemic machinery does the rest: witnesses gain beliefs, rumors spread and distort, reputations form, future narration bends around what happened. A spectacular feat with no surviving witnesses stays unknown; that is a feature.

Both postures share one seam: an encounter is "hand authority to a resolver, receive effects." A ruleset is a resolver in-process; a federated game is a resolver out-of-band (`06-rulesets.md`). Real-time games — excluded from GM mode by construction — federate fine in Chronicler mode.

The division of authority is the engine's identity statement: a conventional RPG engine's core questions are *"can I hit the goblin? how much damage? do I pass the check?"* Uro's core questions are **"who knows what? what evidence supports it? how does new information change the story?"** External games keep answering the first set better than any LLM pipeline could; Uro answers the second set, which they cannot answer at all.

MVP scope: the Chronicler-mode *doors* are built now because they're retrofit-expensive (an external trust tier in `13`, an external emitter class in `12`, out-of-band encounter completion in `06`), plus a tiny proof in Phase 5 (`10`); the full ingestion contract is deferred until a real external game demands it (OQ-12).

## Content agnosticism

The engine is content-neutral — it does not judge what you commit. There are **no engine-level safety filters or content guardrails**. Content boundaries are declared **per world** (in the world manifest, not on a user profile) and enforcement is delegated to two parties who actually own the concern:

1. **The connected LLM** — cloud providers enforce their own policies regardless of what we allow. The engine therefore ships **capability probes** (see `04-llm-integration.md`): per-world tests of whether a bound model can actually deliver the world's declared content rating, tone, and structural requirements — turning a policy problem into a best-effort, testable compatibility signal (judge-scored with transcripts attached, D-24).
2. **The consuming platform/game** — a kid-friendly game and an 18+ platform can both sit on the same engine and apply their own filtering at their layer.

What the engine *does* own: **data privacy**. Credentials encrypted at rest, no story content in telemetry, logs that redact secrets, full data export/delete. See `04-llm-integration.md` and `07-persistence-and-events.md`.

## Cost posture

The engine does not budget, cap, or bill LLM usage — that is a consumer concern. But it must be **as optimized as possible** (caching, batching, retrieval-scoped context, async pre-generation) and must **expose usage metrics** (tokens, calls, latency per pipeline stage) so consumers can care about cost even though the engine doesn't.

## Target users — of the engine

The research report's personas (Solo Adventurer, Tabletop DM, Worldbuilder, Co-op Party, Moderator) remain valid, but they are **users of platforms built on Uro**. The engine's direct users are:

1. **The engine developer (you, now):** needs the CLI loop, dry-run mode, and inspectable state to develop and debug the engine itself.
2. **Game/platform developers (later):** need a stable API/SDK, embeddable core, portable world packs, and clear extension points (rulesets, providers, prompt packs).
3. **Worldbuilders (indirectly):** author world packs as files; the engine validates and runs them. Their UI, if any, is a platform's job.

## Baseline assumptions

- Fantasy-RPG (D&D-ish) is the *default vocabulary*, not a hard dependency — the ruleset is pluggable and the world pack defines the setting.
- Readily-available LLMs, connected in multiple ways (API key, local Ollama, any OpenAI-compatible endpoint). The engine never fine-tunes models itself; consumers may connect fine-tuned models like any other endpoint.
- Solo development pace; the report's team-size/budget/quarter tables do not apply to this PoC and are explicitly disregarded.
