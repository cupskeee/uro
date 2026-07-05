# 15 — End-to-End Walkthroughs

Three concrete traces that exercise every subsystem, showing how the docs compose. Re-read them after any design change — they are the *manual* drift check; nothing enforces them automatically. (These traces are the skeletons of the automated acceptance tests, `10` + `14`.)

## A. Life of a world: Ashfall

| # | Step | What happens | Governed by |
|---|---|---|---|
| 1 | **Author** | You write `worlds/ashfall/`: `world.toml` (tone: grim/political; content: mature, violence+horror, no sexual content; ruleset uro-basic; `simulate_years=200`), lore markdown, a few seeded entities, a narrator style template. | `09` |
| 2 | **Validate** | `uro world validate ./worlds/ashfall/` → sufficiency report: everything `runnable` except *Conflict seeds: none found*. You accept AI backfill; History proposes two tension threads, event-tagged `provenance=ai_backfill`. Grade: `runnable`. | `09` |
| 3 | **Import** | `uro world create ./worlds/ashfall/` → `WorldGenesis` + `PlaceCreated`/`FactionCreated`/`ActorCreated`/`EdgeAdded` (emitter `S`) for the authored seeds, all in the first commit on branch `main`. | `12`, `07`, `09` |
| 4 | **Probe** | `uro world probe ashfall` → bound models tested: structured-output ✓, mature-content ✓ (world declares it; models comply), consistency ✓. Report stored. | `04` |
| 5 | **Seed** | `uro world seed ashfall --seed 42` → History simulates 200 years: `HistorySeeded` header + hundreds of Created/Edge events. Duchy of Vel rules the coast; the Saltborn cult (faction, kind=religion) festers. Seed 43 would have produced different dynasties on identical geography. | `01`, `03` |
| 6 | **Play (Campaign A)** | `uro campaign new ashfall --branch main --pc wizard.yaml` → `CampaignStarted`, `PCBound`. Beats accumulate (walkthrough B). Over many sessions the party's choices push the Saltborn ritual thread to completion; as its **consequence-on-resolution** the History service emits `TerrainChanged`/`PlaceDestroyed(Vel)` (emitter `H`, `caused_by=player_action`) — a falling star obliterates Vel *mid-campaign* (not the extractor/pipeline, which are barred). An adaptation pass then ripples dependent threads (`caused_by=history, pass=adaptation`). | `03`, `12` |
| 7 | **End & mark** | Campaign A closes: `CampaignEnded`, `PCReleased` (the wizard is now a world NPC), marker `campaign-a-end`. | `03`, `12` |
| 8 | **Fork: continue** | `uro branch fork ashfall --at campaign-a-end --name aftermath` + new campaign adopting the wizard (`PCBound` to the same actor_id). Same player faces what they caused. | `03` |
| 9 | **Fork: new life** | Second fork, new campaign, fresh farmer PC, time-skip 1 year → History adaptation pass (`AdaptationApplied`, refugee threads spawned, Duchy edges to `at_war_with` scavenger factions). The farmer hears NPCs retell Campaign A's deeds — they're `ClaimRecorded` history on this branch. | `03`, OQ-8 |
| 10 | **Share** | `uro export branch aftermath --at campaign-a-end -o vel-aftermath.uwp` → hash-verified pack; anyone imports it (fork-on-import) and plays the ruins. What a platform would build a library around. | `07` |

## B. Life of a beat

Mid-Campaign A. Free-roam, evening, the Brinehouse tavern (Site) in Vel. The player types:

> **"I ask Mera the innkeeper what she knows about the missing dockworker."**

| Stage | What happens | Contract |
|---|---|---|
| *admission* | `SoloArbiter` admits; one in-flight beat per campaign. `BeatState` created: mode `freeroam`, scene = Brinehouse projection. | `08`, `13` |
| **[1] Context** | Structured recall: Mera (T2 — promoted after repeated visits) profile + her belief set: `believes(claim-0451 "the Saltborn take people from the docks", confidence 0.8, learned_from actor:old-fisher)`. Claim-0451's engine truth: `unknown`. Semantic recall surfaces the beat where the party found a salt-crusted amulet. Recency: last 6 beats. | `04` |
| **[2] Plan** | Planner (temp 0.2, schema-forced) → BeatPlan: `intent_class: dialogue`, `speakers: [actor:mera]`, `mechanics: [{affordance: "persuade", actor: pc, target: mera, context: "get her to share what she fears saying"}]`, `time_cost: 0`. `persuade` is in Uro Basic's declared affordances — validation passes. | `13`, `06` |
| **[3] Mechanics** | Ruleset resolves: d20(seeded)+CHA = 17 vs Medium 15 → success. `CheckResult` with trace "17 vs DC 15". No LLM involved. | `06` |
| **[4] Generate** | Dialogue call for Mera: her profile, beliefs-in-scope, the successful check, and Ashfall's dialogue template render the prompt. She leans in: *"Third one this season. The Saltborn, I'd stake the till on it — old Weck saw robes by the pier."* Streams to the client while [5] starts. | `04`, `13`, `09` |
| **[5] Extract** | Extractor (temp 0.1) — fed *only* the generated prose, plan, and mechanics results; raw player text never reaches it (trust model, `13`) — proposes: ① `BeliefChanged` — PC now believes claim-0451 (belief strength 0.7, learned_from Mera; distinct from the extractor's own state-worthiness confidence that gates the proposal, `13`). ② `ActorCreated` T1 "Old Weck, dock fisher" — entity resolution found no existing match by name/alias/embedding, so create; tier ceiling respected. ③ `EdgeAdded knows(weck→saltborn-activity)`. **Not** proposed: claim-0451 as `truth=true` — Mera *saying* it is testimony, not narrator-asserted fact; provenance keeps it a belief. Contradiction check: clean, downgrade-or-drop only (narration already streamed). | `05`, `12`, `13` |
| **[6] Commit** | One transaction: `BeatResolved` (intent, synopsis, narration) + the three proposals → commit `01J...`, branch head advances, outbox row written, projections update, chronicle gains a line. The beat result returns with three planner-emitted suggestions, dimmed in the CLI — free text stays canonical (D-23). Usage: 4 LLM calls, stage-tagged, in `llm_calls`. | `07`, `12` |

Next beat, the player might go find Old Weck — a T1 sketch the *extractor* invented thirty seconds ago, now as real as anything seeded, and two interactions from a T2 promotion. If the player instead types *"I flip the table and swing at the guard,"* [2] emits `mode_transition: encounter`, [3] runs `start_encounter`, and turns exist until `EncounterEnded` — and if they lose, `ActorDamaged`/`ItemTransferred` effects persist into every future beat and every future fork.

**Dry-run variant:** the same input with `?dry_run=true` runs all six stages, streams the same prose, and returns the three proposals as an event diff instead of committing — the creator sandbox and the debugging harness are this one flag.

## C. War story (Chronicler mode — the Phase 5 proof)

The engine as consequence layer around an external game (D-25). A toy auto-battler plays the battle; Uro never sees a single turn.

| # | Step | What happens | Governed by |
|---|---|---|---|
| 1 | **Hand off** | The campaign reaches a battle the external game will fight. Uro parks the encounter: `EncounterStarted` (emit E), authority transferred. | `06`, `12` |
| 2 | **Battle** | The auto-battler plays out entirely in its own process — its rules, its feel, its pace. Uro is not involved and does not care. | — |
| 3 | **Report** | `POST /campaigns/{c}/encounters/{e}/outcome` with bundle v0: participants, casualties, loot, `witnesses: [3 bandits who fled]`, `notable_feats: ["PC annihilated the warband with a single fire spell"]`. Mechanical facts commit directly (`ActorDied`, `ItemTransferred`, `EncounterEnded` — trust tier 1, within the encounter's domain). | `08`, `13` |
| 4 | **Distill** | The feat is interpretive, so it takes the long road: distillation proposes `ClaimRecorded` ("the mercenary wields fire like a god", `truth=true` — the engine *knows* it happened) + `BeliefChanged` **only for the three surviving witnesses**. The gauntlet validates as if the extractor had proposed it. | `12` rule 7, `13` |
| 5 | **Ripple** | The witnesses scatter; off-screen simulation spreads the belief through their contacts, distorting confidence and details as rumors do. Beats later, a tavern NPC two regions away retells a warped version — belief chain traceable to bandit #2. **Re-run step 3 with `witnesses: []`:** the claim still exists (`truth=true`), but zero beliefs means nobody ever mentions it. Killing every witness is a mechanically real strategy. | `02`, `05` |

## Doc responsibility map

| Question while coding | Doc |
|---|---|
| What is / isn't the engine's job? | `00` |
| Where does this module live; what may it import? | `01`, `14` |
| What fields does this entity have? | `02` |
| How do branches/forks/materialization work? | `03` |
| How do I talk to a model / add a provider / test capability? | `04` |
| What order do stages run; what's canonical vs flavor? | `05` |
| How do mechanics plug in? | `06` |
| What's the schema; how do events persist and publish? | `07`, `12` |
| What's the API/CLI surface; how do sessions work? | `08` |
| What's in a world pack; how is it validated? | `09` |
| What do I build next; when is it done? | `10`, `14` |
| Is this genuinely undecided? | `11` |
| What exact JSON does a stage produce/consume? | `13` |
| Why is it this way? | `decisions.md` |
