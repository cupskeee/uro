# 06 — Rulesets

Decision on record: mechanics live behind a **pluggable ruleset interface**; the engine ships one minimal built-in ruleset so it's playable out of the box. This keeps the engine game-agnostic without shipping nothing.

## The port

A ruleset is a Python plugin (entry-point discoverable) implementing:

```python
class Ruleset(Protocol):
    id: str; version: str

    # Character model — a Sheet is an OPAQUE dict[str, Any]; the ruleset owns its shape.
    def sheet_schema(self) -> JsonSchema            # what THIS ruleset's sheet looks like
    def new_character(self, spec: CharSpec, rng: Rng) -> Sheet   # CharSpec.data is opaque
    def progression(self, sheet: Sheet, award: Award) -> Sheet  # Award.data is opaque

    # Action resolution (free-roam checks)
    def affordances(self) -> list[Affordance]       # vocabulary + mandatory trigger categories (D-21)
    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult   # owns ALL resolution

    # Encounter mode — EncounterState is OPAQUE (a ruleset subclass); the runner never reads it.
    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState
    def current_actor(self, state) -> str | None    # who acts next, or None when decided
    def legal_actions(self, state, actor_id: str) -> list[ActionSpec]
    def npc_action(self, state, actor_id: str, rng: Rng) -> Action   # monster/NPC chooser
    def resolve_action(self, state, action: Action, rng: Rng) -> (EncounterState, list[Effect])
    def is_over(self, state) -> EncounterOutcome | None
    def sheets(self, state) -> dict[str, Sheet]     # final opaque sheets → SheetUpdated at end
```

**GAME-AGNOSTIC BY CONSTRUCTION (OQ-13 → D-30).** The port names NO game vocabulary — no
abilities, no hp/ac, no DC, no attack/damage. A `Sheet` is an opaque `dict` each ruleset
validates internally; a `CheckResult` carries a ruleset-declared graded `outcome` string (d20:
`{failure, success}`; PbtA 2d6: `{miss, partial, full}` — the 7-9 partial a `bool` could not
hold); `CheckRequest` carries a `stat` key + opaque `difficulty` hint (no numeric DC — the
ruleset owns thresholds, so `dc_for` is gone from the port); `Action`/`Effect` kinds are open
strings; `EncounterState` is an opaque ruleset subclass. **Harm** never assumes an hp scalar:
the runner persists each combatant's opaque final sheet as `SheetUpdated` (an hp system zeroes
hp; a harm-clock system fills the clock), and `ActorDied` is only a ruleset-agnostic lifecycle
trace. Two structurally different built-ins keep this honest (below).

Contract notes:

- **Deterministic:** rulesets get a seeded `Rng`, never wall-clock or global random — required for replay and dry-run.
- **No LLM calls inside rulesets.** Rules are code. The pipeline narrates *around* mechanical results; the ruleset produces `CheckResult`/`Effect` data with human-readable trace fields ("rolled 16 + 3 vs DC 15", "2d6 (8) +1 → partial") that the narrator role weaves into prose.
- **Affordances are the coupling point** with generation: the planner is prompted with the ruleset's declared affordances (`persuade → CHA check`, `attack → encounter`, `sneak → DEX check`…) so it can *invoke* mechanics without *knowing* the math. Turn-based structure only ever enters through `start_encounter` — free-roam stays turnless (owner requirement).
- **Triggers make affordances mandatory, not just available (D-21):** each affordance declares trigger categories — intent/risk classes that *must* invoke it (e.g. `persuade` triggers on any attempt to change an NPC's disposition or intent). Plan validation enforces triggers deterministically (`13-contracts.md`), and consequence gating is *intended* to backstop it at commit: protected-state changes without mechanics backing get downgraded, so "I persuade the king to hand me the crown" can't succeed by phrasing alone. **Phase-3 status: only the plan-side trigger check ships; the commit-time consequence-gating backstop is NOT built yet (`13`, `extraction.py`) — a misclassified intent can still slip a low-stakes change through, so this is not yet a hard boundary.**
- **Who chooses each turn's action (D-26):** the pipeline drives the initiative loop; on a PC's turn the action comes from the player (`encounter_action`, `08`); on an NPC's turn it comes from `npc_action`. Either way it runs through `resolve_action` and is recorded as `EncounterTurnTaken` (emitter R, `12`). *(Phase-3 PoC status, D-29: encounters AUTO-RESOLVE — both sides use `npc_action` and the whole fight commits as one free-roam beat; interactive per-turn play via `encounter_action` is deferred.)* NPC selection lives in the **ruleset as deterministic code**, not an LLM planner — because Phase 3 requires seeded-RNG, recorded-response replay, and dry-run (`10`, `05`), which a live LLM chooser would break. (The "no LLM in rulesets" rule alone wouldn't forbid a *planner*-side chooser; the determinism requirement is what settles it.)
- Effects map to domain events so mechanical outcomes are timeline citizens like everything else: harm reaches the timeline as the ruleset's opaque final `SheetUpdated` (D-30 — never an hp-scalar event), loot as `ItemTransferred`, and the fight bracketed by `EncounterStarted`/`EncounterEnded`. (`ActorDamaged` is legacy — pre-D-30 — retained only for replaying old logs.)
- The world pack declares `ruleset = "id@version"` + config; a campaign snapshots the ruleset id+version at creation so worlds don't break under it. **Binding (inc 6.3, D-30):** a **registry** (`rulesets/registry.py`) resolves the declared id → a bound `Ruleset`; `create_world` records the pack's ruleset on `WorldGenesis`, a campaign started on the world **pins** it (`CampaignStarted.ruleset_id`/`ruleset_version` + the `campaigns` projection), and `play`/`dry-run` **rebind** from the campaign's pin — so a PbtA pack (`worlds/emberfell`) plays under `uro_pbta`, not the old hard-coded default. An unknown id fails loudly. The PoC ships an explicit registry of the two built-ins; entry-point discovery (an external pip package registering its own ruleset) is the documented extension seam (`register()`). *(Server caveat: `uro serve` binds one ruleset per process; per-campaign rebind there is deferred.)*

## Built-in: "Uro Basic"

A deliberately minimal, original d20-flavored system — enough to exercise every port method, small enough to fit in a few hundred lines:

- Six abilities (STR/DEX/CON/INT/WIS/CHA), modifiers = (score−10)//2.
- Checks: d20 + modifier vs. difficulty (Easy 10 / Medium 15 / Hard 20), advantage/disadvantage.
- HP, simple AC, initiative = d20+DEX; encounter turns: one action (`attack`/`defend`/`flee` — no positioning/`move` in the PoC built-in); attack = check vs. AC, damage dice by weapon tier.
- Level 1–5 progression, flat proficiency bonus.
- Original text/terminology (d20-*mechanics* are uncopyrightable; we still avoid replicating 5e's expression — if fuller D&D compatibility is ever wanted, the 5.1 SRD is CC-BY-4.0 and can become a *separate* `srd51` plugin with attribution).

Uro Basic doubles as the reference implementation and the test fixture for the port.

## Second built-in: "Uro PbtA" — the generality probe (OQ-13 → D-30)

One built-in validates *playability*, not *game-agnosticism*: a port shaped against only Uro
Basic would quietly inherit d20 assumptions until a structurally different ruleset is attempted.
So the port is now proven by a deliberately **alien** second built-in — `uro_pbta`, a PbtA-style
2d6 system that shares nothing with d20:

- Four stats (`forceful/clever/quick/steady`, −1..+3); the **stat IS the modifier** (no `(score-10)//2`).
- **2d6 + stat vs fixed 7/10** → three outcomes: `miss` / `partial` (success-at-a-cost) / `full`. No DC, no binary success.
- **No hp, no ac.** Harm is a **0-4 clock** + narrative **conditions**; "out of the fight" at clock 4, not hp 0.
- **Moves** (`seize_by_force`, `persuade`, …), not attack/defend/flee.
- A **move-exchange** conflict — no initiative, no rounds.
- **Advance by failing** — mark XP on a miss.

Building it forced every d20 assumption out of the "generic" port into `uro_basic` (the full
forced-change list is the leak report in D-30). A narrate-only null ruleset would **not** have
counted — it exercises no encounter path, exactly where d20 leaks hid. The two built-ins now run
through the identical port + runner, which is the actual game-agnosticism proof.

## Encounters as the federation seam

An encounter is "hand authority to a resolver, receive effects back as events." Usually the resolver is the in-process ruleset, turn by turn — but the port deliberately allows **out-of-band resolution**: an encounter may be parked with an external resolver (a real game engine running its own battle), and resolution arrives later as an **outcome bundle** — validated like any other untrusted input (external trust tier, `13-contracts.md`). This is Chronicler mode's mechanical door (D-25): Final Fantasy Tactics keeps its battle feel; Uro gets the war story. The full ingestion contract (domain declarations, bundle schema, distillation rules) is OQ-12 and stays unbuilt beyond the Phase 5 toy proof until a real external game demands it.

## What this enables

A rules-light narrative system (PbtA-style 2d6) is now a shipped built-in (`uro_pbta`, above) —
proof the port hosts a non-d20 system without engine changes. Pathfinder-ish plugins or a
consumer's fully custom system follow the same seam, all without touching engine code. A
"no-mechanics" world remains possible by binding a trivial ruleset whose checks always
narrate-only; the engine itself never special-cases mechanics-free play.
