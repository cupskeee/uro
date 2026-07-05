# 02 — Domain Model

> **Living document.** Per owner feedback, the entity set and fields are *not fixed* — this is the current best guess, expected to evolve through brainstorming and play-testing the engine. Additions/renames are cheap before Phase 2 of the roadmap; treat this as a vocabulary, not a contract. Open items are tracked in `11-open-questions.md` (OQ-1).

## Identity and versioning basics

- Every entity has a stable `entity_id` (ULID) that survives across branches — a fork of a world refers to *the same* Duke by the same ID, with diverged state.
- Entities accumulate `aliases[]` (via `EntityAliasAdded`) — generated prose renames people constantly ("Old Weck" / "Weck" / "the fisherman"), and **entity resolution** matches on name, alias, and embedding before any `*Created` proposal is accepted (`13-contracts.md`). Without this, the extractor breeds duplicate NPCs.
- Duplicates will slip through anyway: `EntityMerged` folds one id into a survivor through a **merge map** that projections and queries resolve through. Entity ids are never deleted; historical events are never rewritten.
- Entity *state* is always "state at a point on a timeline branch" — there is no such thing as an entity's state without saying *when and on which branch*. See `03-timeline-and-branching.md`.
- All models are Pydantic v2; the domain layer is persistence-ignorant.

## Core entities

### World
The container. Definition-level data from the world pack (name, cosmology, magic rules, tone, **content declaration**, prompt pack ref, ruleset ref) plus the root of its timeline. Content settings live **here**, not on any user profile.

### Place (Region / Settlement / Site)
Hierarchical geography: `Region ⊃ Settlement ⊃ Site` (a tavern is a Site in a Settlement in a Region). Terrain, climate, resources, population, government ref (→ Faction/Actor). **Physical state is mutable via events** — `PlaceDestroyed`, `TerrainChanged` (the meteor crater) are ordinary timeline events on the slow-changing layer.

### Actor (NPC or PC)
One type for all characters; PCs are actors bound to a campaign. "Is this actor a PC?" is **not** a global flag on the actor — it's answered per-branch by the campaign's `PCBound`/`PCReleased` history (`12`), because the same `actor_id` can be a PC on one fork and an ordinary NPC on another (the meteor test's retired wizard, `03`). Like all actor state, it's "state at a point on a branch," never a standalone property. Key design point — **promotion tiers**, because "every NPC should have a profile, but not too seriously at first" and any extra can become important:

| Tier | Name | Contents | Created by |
|---|---|---|---|
| T0 | Extra | Role label only ("a dockworker"), maybe unnamed; may exist only in narration | Narration, freely |
| T1 | Sketch | Name, role, one-line disposition, location | Pipeline promotion rule (named in output → sketch) |
| T2 | Profile | + personality, goals, relationships, faction, basic stats, belief set | Repeated player interaction, or planner flags significance |
| T3 | Agent | + agenda (active goals the actor pursues off-screen), private memory journal, full sheet | Story significance, or manual pin (player/creator "makes them important") |

Promotion is one-way and event-recorded (`ActorPromoted`). Demotion doesn't exist; irrelevant actors just go dormant. The pipeline's canonicalizer owns automatic promotion; the API exposes manual pinning.

### Faction
Named collective (kingdom, guild, cult, tribe): members (actor refs), goals, resources, territory (place refs), inter-faction relations (edges: ally/enemy/vassal/at-war, weighted). Religions are factions with `kind=religion` plus doctrine fields — collapsed per report feedback simplification; split later if doctrine mechanics grow.

### Item
Name, kind, properties (ruleset-interpreted), owner/location ref, provenance. MVP keeps items simple; economy simulation is OQ-6.

### Claim (Fact / Rumor — the epistemic layer)
The report's fact/rumor split, generalized. A **Claim** is a statement about the world with:

- `statement` (text + optional structured subject refs)
- `truth`: `true | false | unknown` — ground truth as the *engine* knows it
- `origin`: which event/actor produced it
- per-actor **Belief** edges: `(actor, claim) → confidence ∈ [0,1]`, `learned_from`, `learned_at`

So "the Duke plans war" can be objectively false, believed at 0.9 by the innkeeper who heard it from a spy, and unknown to everyone else. Rumors are simply claims that circulate (belief edges spreading) without `truth=true`. This is what lets misinformation, secrets, and investigation emerge mechanically instead of narratively hand-waved.

### Thread (Quest / Plot)
A narrative thread: originator (actor/faction ref), stakes, state (`dormant | offered | active | resolved | dead`), steps/objectives (loose — the planner improvises within them), consequences-on-resolution (event templates). "Quest" is the player-facing word; internally threads also cover off-screen plots (the bandit chief's ambitions are a thread whether or not a player touches it).

### Campaign
A play-through: branch ref (its timeline), party (PC actor refs), current scene/mode, ruleset config snapshot, campaign status. A campaign *writes* to its branch; the world outlives every campaign.

### Scene
Ephemeral-ish play context: place ref, present actors, mode (`freeroam | encounter | downtime`), open hooks. Scenes are reconstructable from events; stored as a projection for speed.

### Chronicle (the "Lore Wall")
Not a stored entity — a **projection** over the event log, materialized **per branch** (`08`: `?branch=B`): the human-readable history along that branch's lineage — every campaign from world genesis up to the branch head ("every action, NPC, faction reputation, and quest outcome"). Because branches don't merge (`03`), divergent branches keep separate chronicles and never see each other's post-fork events: a what-if branch forked before the meteor never chronicles the meteor; sibling forks are mutually invisible. "Across all its campaigns" means *along the lineage*, not world-globally. Exposed via API for platforms to render; exported in packs. Cross-session continuity falls out of the architecture rather than being a feature.

## Relationships (the graph)

All inter-entity relations are typed, directed, weighted edges in one table (see `07-persistence-and-events.md`): `member_of`, `rules`, `located_in`, `owns`, `knows`(actor→actor), `believes`(actor→claim), `at_war_with`, `party_to`(thread), etc. Edge changes are events like everything else. This gives graph semantics without a graph database until queries demand one.

## What the report had that we dropped or moved

- **PlayerState as separate entity** → folded into Actor(PC) + per-campaign data. Player *identity* (accounts) is a platform concern.
- **Fact and Rumor as distinct types** → unified as Claim + Beliefs (strictly more expressive).
- **Static Region layer** → Places on the slow-changing layer, mutable via events.
- **Content settings per user** → per World.
