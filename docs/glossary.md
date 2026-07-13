# Glossary

One line per term; the linked doc is the authority. Terms are used *exactly* this way across all docs and code — if a doc drifts from these meanings, the doc is wrong.

| Term | Meaning | Doc |
|---|---|---|
| **Actor** | Any character, NPC or PC; one type, tiered T0–T3 | `02` |
| **Adaptation pass** | History service re-evaluating threads/claims after major events, forks, or time-skips | `03` |
| **Affordance** | A mechanics capability a ruleset declares (e.g. `persuade`), the only rules vocabulary the planner may invoke | `06` |
| **Backfill** | Opt-in AI generation filling sufficiency-check gaps in a world pack, provenance-tagged | `09` |
| **Beat** | One player intent, resolved end-to-end through the pipeline; the unit of play in free-roam | `05` |
| **Beat commit** | The single commit batching all events one beat produced | `03` |
| **Belief** | A per-actor edge to a claim with confidence and provenance; what someone *thinks* is true | `02` |
| **Branch** | A named head pointer on a world's commit chain; campaigns play on branches | `03` |
| **Campaign** | A play-through: party + branch + ruleset config; writes to its branch, world outlives it | `02` |
| **Capability probe** | Per-world test suite verifying bound models can deliver the world's declared requirements (incl. content rating) | `04` |
| **Chronicle** | The human-readable projection of a **branch's** history across the campaigns in its lineage — genesis to branch head (the report's "Lore Wall"); divergent branches never see each other's post-fork events | `02` |
| **Chronicler mode** | Integration posture where an external game owns its domain and Uro is the world-memory/consequence layer around it (D-25) | `00`, `06` |
| **Claim** | A statement with engine-level truth (`true/false/unknown`) plus per-actor beliefs; unifies the report's facts and rumors | `02` |
| **Commit** | A parent-linked, hash-chained batch of events; the timeline's unit of history | `03` |
| **Dry-run** | Full pipeline execution with no commit, returning the would-be event diff; the creator sandbox | `05` |
| **Edge** | A typed, weighted, temporally-valid relation between entities; the graph, event-sourced | `02`, `07` |
| **Emitter whitelist** | Per-event-type list of who may emit it; the extractor's hard ceiling | `12` |
| **Encounter mode** | The only turn-based mode; owned by the ruleset's state machine (combat/initiative) | `05`, `06` |
| **Entity resolution** | Matching newly named entities against existing ones (name/alias; embedding leg planned) before any `*Created` is accepted | `13` |
| **EntityMerged / merge map** | Folding a duplicate entity into a survivor; projections resolve through the map, history stays untouched | `12` |
| **Extractor** | The pipeline role that parses generated prose into ProposedEvents for canonicalization | `05`, `13` |
| **Fork** | Creating a branch from any commit; carries world state, not campaign-scoped state | `03` |
| **GM mode** | Integration posture where Uro owns the game loop: play is the beat pipeline (D-25) | `00` |
| **Marker** | An immutable named ref to a commit (a tag): campaign endings, notable moments; a ref, not an event | `03` |
| **Materialization** | Building full world state at an arbitrary commit (nearest snapshot + replay) | `03` |
| **Mode** | Play cadence: `freeroam` (beat-driven, turnless) · `encounter` (turns) · `downtime` (time-skip) | `05` |
| **Outcome bundle** | An external resolver's report on a parked encounter: participants, casualties, witnesses, notable feats; mechanical facts commit, interpretations get distilled | `06`, `12`, `13` |
| **Participant** | A connected player identity within a session, mapped to a PC actor; the multiplayer seam | `08` |
| **Projection** | A rebuildable read-model derived from events; never written directly, never truth | `03`, `07` |
| **Promotion** | Raising an actor's tier (T0 extra → T3 agent) because play made them matter; behavior-driven, one-way | `02` |
| **Prompt pack** | A world pack's template overrides (`prompts/`); world-level, distributable, contract-stable | `09`, `13` |
| **Proposal / ProposedEvent** | An extractor-suggested event that must survive the validation gauntlet before commit | `13` |
| **Role (LLM)** | A generation duty (narrator, dialogue, planner, extractor, summarizer, embedder) bound to a model per deployment/world | `04` |
| **Ruleset** | A deterministic mechanics plugin behind the ruleset port; "Uro Basic" is the built-in | `06` |
| **Session** | A live connection context on a campaign (participants + arbitration); not the campaign itself | `08` |
| **Simulate-on-observation** | Resolving off-screen actor/faction activity lazily, when next observed | `01`, OQ-4 |
| **Snapshot** | Serialized full state at a commit, for fast materialization | `03` |
| **Sufficiency check** | Import-time rubric grading a world pack `runnable / thin / insufficient` | `09` |
| **Suggestions** | Optional planner-emitted next-action hints on a beat result; free text stays canonical (D-23) | `13`, `08` |
| **Thread** | A narrative arc (player quest or off-screen plot): `dormant → offered → active → resolved/dead` | `02` |
| **Tier (T0–T3)** | Actor detail levels: Extra → Sketch → Profile → Agent | `02` |
| **Trigger category** | An intent/risk class an affordance declares mandatory — plan validation re-asks plans that skip it (D-21) | `06`, `13` |
| **Trust model** | The evidence hierarchy for canon: state > narrator prose > dialogue (testimony) > player text (never evidence) | `13` |
| **World** | The container universe: definition + timeline; content settings live here | `02` |
| **World pack** | The portable directory/archive defining a world (manifest, lore, seeds, prompts) | `09` |
| **World time** | In-fiction time: absolute day counter + segment; the world-pack calendar derives years & seasons (D-22); named eras are event-driven, not arithmetic; never wall-clock | `12` |
