# 06 — Rulesets

Decision on record: mechanics live behind a **pluggable ruleset interface**; the engine ships one minimal built-in ruleset so it's playable out of the box. This keeps the engine game-agnostic without shipping nothing.

## The port

A ruleset is a Python plugin (entry-point discoverable) implementing:

```python
class Ruleset(Protocol):
    id: str; version: str

    # Character model
    def sheet_schema(self) -> JsonSchema            # what a character sheet looks like
    def new_character(self, spec: CharSpec, rng: Rng) -> Sheet
    def progression(self, sheet: Sheet, award: Award) -> Sheet

    # Action resolution (free-roam checks)
    def affordances(self) -> list[Affordance]       # vocabulary + mandatory trigger categories (D-21)
    def dc_for(self, difficulty: Difficulty) -> int # tier (easy/medium/hard) → numeric DC
    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult

    # Encounter mode (combat)
    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState
    def legal_actions(self, state: EncounterState, actor_id: str) -> list[ActionSpec]
    def npc_action(self, state: EncounterState, actor_id: str, rng: Rng) -> Action   # monster/NPC chooser
    def resolve_action(self, state: EncounterState, action: Action, rng: Rng) -> (EncounterState, list[Effect])
    def is_over(self, state: EncounterState) -> EncounterOutcome | None
```

Contract notes:

- **Deterministic:** rulesets get a seeded `Rng`, never wall-clock or global random — required for replay and dry-run.
- **No LLM calls inside rulesets.** Rules are code. The pipeline narrates *around* mechanical results; the ruleset produces `CheckResult`/`Effect` data with human-readable trace fields ("rolled 16 + 3 vs DC 15") that the narrator role weaves into prose.
- **Affordances are the coupling point** with generation: the planner is prompted with the ruleset's declared affordances (`persuade → CHA check`, `attack → encounter`, `sneak → DEX check`…) so it can *invoke* mechanics without *knowing* the math. Turn-based structure only ever enters through `start_encounter` — free-roam stays turnless (owner requirement).
- **Triggers make affordances mandatory, not just available (D-21):** each affordance declares trigger categories — intent/risk classes that *must* invoke it (e.g. `persuade` triggers on any attempt to change an NPC's disposition or intent). Plan validation enforces triggers deterministically (`13-contracts.md`), and consequence gating is *intended* to backstop it at commit: protected-state changes without mechanics backing get downgraded, so "I persuade the king to hand me the crown" can't succeed by phrasing alone. **Phase-3 status: only the plan-side trigger check ships; the commit-time consequence-gating backstop is NOT built yet (`13`, `extraction.py`) — a misclassified intent can still slip a low-stakes change through, so this is not yet a hard boundary.**
- **Who chooses each turn's action (D-26):** the pipeline drives the initiative loop; on a PC's turn the action comes from the player (`encounter_action`, `08`); on an NPC's turn it comes from `npc_action`. Either way it runs through `resolve_action` and is recorded as `EncounterTurnTaken` (emitter R, `12`). *(Phase-3 PoC status, D-29: encounters AUTO-RESOLVE — both sides use `npc_action` and the whole fight commits as one free-roam beat; interactive per-turn play via `encounter_action` is deferred.)* NPC selection lives in the **ruleset as deterministic code**, not an LLM planner — because Phase 3 requires seeded-RNG, recorded-response replay, and dry-run (`10`, `05`), which a live LLM chooser would break. (The "no LLM in rulesets" rule alone wouldn't forbid a *planner*-side chooser; the determinism requirement is what settles it.)
- Effects map to domain events (`ActorDamaged`, `ItemTransferred`, `EncounterEnded`) so mechanical outcomes are timeline citizens like everything else.
- The world pack declares `ruleset = "id@version"` + config; a campaign snapshots the ruleset version at creation so worlds don't break under it.

## Built-in: "Uro Basic"

A deliberately minimal, original d20-flavored system — enough to exercise every port method, small enough to fit in a few hundred lines:

- Six abilities (STR/DEX/CON/INT/WIS/CHA), modifiers = (score−10)//2.
- Checks: d20 + modifier vs. difficulty (Easy 10 / Medium 15 / Hard 20), advantage/disadvantage.
- HP, simple AC, initiative = d20+DEX; encounter turns: one action (`attack`/`defend`/`flee` — no positioning/`move` in the PoC built-in); attack = check vs. AC, damage dice by weapon tier.
- Level 1–5 progression, flat proficiency bonus.
- Original text/terminology (d20-*mechanics* are uncopyrightable; we still avoid replicating 5e's expression — if fuller D&D compatibility is ever wanted, the 5.1 SRD is CC-BY-4.0 and can become a *separate* `srd51` plugin with attribution).

Caveat on generality: one built-in validates *playability*, not *game-agnosticism*. A port shaped against only Uro Basic will quietly inherit d20 assumptions (ability-score sheets, `CheckResult`/`EncounterState` shapes) until a structurally different ruleset is attempted — the real generality test, tracked as OQ-13. A narrate-only null ruleset does **not** count (it exercises no encounter path, exactly where d20 leaks hide).

Uro Basic doubles as the reference implementation and the test fixture for the port.

## Encounters as the federation seam

An encounter is "hand authority to a resolver, receive effects back as events." Usually the resolver is the in-process ruleset, turn by turn — but the port deliberately allows **out-of-band resolution**: an encounter may be parked with an external resolver (a real game engine running its own battle), and resolution arrives later as an **outcome bundle** — validated like any other untrusted input (external trust tier, `13-contracts.md`). This is Chronicler mode's mechanical door (D-25): Final Fantasy Tactics keeps its battle feel; Uro gets the war story. The full ingestion contract (domain declarations, bundle schema, distillation rules) is OQ-12 and stays unbuilt beyond the Phase 5 toy proof until a real external game demands it.

## What this enables later (not now)

Pathfinder-ish plugins, rules-light narrative systems (PbtA-style 2d6), or a consumer's fully custom system — all without touching engine code. A "no-mechanics" world remains possible by binding a trivial ruleset whose checks always narrate-only; the engine itself never special-cases mechanics-free play.
