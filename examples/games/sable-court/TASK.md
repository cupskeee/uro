# THE SABLE COURT — TASK

Build the game described in `system_prompt.md`, embedding Uro (Posture A). Everything runs on the
deterministic stub/scripted provider — no API key. Deliver the game, a `GAP_REPORT.md`, and a
`README.md` in `examples/games/sable-court/`.

Read `examples/games/URO_INTEGRATION.md` and `examples/hello_uro/hello_uro.py` before writing code.

---

## 1. STAGED BUILD PLAN (each stage is runnable and self-verifying)

Build in slices; after each, run it and print the assertions listed. Do not fan out — the layers
interlock.

**Stage 0 — Skeleton & determinism.** Bring up the store, a `ScriptedProvider`, an `Engine`. Create
a one-House world, start a campaign with the Spymaster PC, run one court beat, print the
`BeatResult`. *Verify:* runs with no key; `commit_id` is non-empty; re-running from a clean DB gives
byte-identical output.

**Stage 1 — The realm as seeded canon.** Author the full cast/geography/plots as `extra_events`
(scale in §2.1). Load the `rule_pack` skeleton. *Verify:* `list_actors`/`list_factions via
list_edges`/`list_threads`/`list_places` return the seeded counts; `find_actor_by_name(branch, "the
Marshal")` resolves to `a:aldric-vaelric` via alias.

**Stage 2 — Court beats + working reaction rules.** Implement the intrigue verbs (§2.2) as beat
intents. Ship the reaction rules that the grammar *can* express (§2.4 R1–R6). Play a scripted
sequence that fires at least: a thread flip on a committed event, and a `record_rumor`/`spread_belief`
that lands as a `truth=unknown` claim. *Verify:* assert the thread state changed and the rumor claim
exists via `list_threads`/`claims_about`.

**Stage 3 — The numeric realm sim (the shadow ledger).** Implement the House ledger + downtime tick
math in **game code** (§2.3), seeded and deterministic. On `agenda_tick`, run the sim, then **reflect
qualitative results into Uro** (§2.5): war edges via `append_beat(edge_added(...))`, holding transfers
as place changes, battles via `distill_outcome(OutcomeBundle)`. *Verify:* after a war, `list_edges(...,
"at_war_with")` shows the edge; a battle's casualties/feats appear as claims; a great-lord "death"
attempt via a bundle is **downgraded to a rumor** (assert it did NOT commit an actor death).

**Stage 4 — Scale stress.** Grow to dozens of live threads and many similarly-named nobles; run
several downtime ticks. *Verify:* `list_threads` count grows monotonically (log whether any close
path exists); `assemble_recall` still returns; entity resolution holds or fragments (record which).

**Stage 5 — The fork (signature).** After N beats + M ticks, `fork_branch` the realm; replay downtime
differently on the fork; diff the two lines. *Verify:* main and fork disagree on at least one of {a
war edge, a thread state, a rumor set}, and both replay cleanly.

**Stage 6 — The refusal pass.** With the game working, deliberately enumerate every realm rule you
*wanted* and the grammar refused (§3 target 1). Write each as the exact `rule_pack` entry you wished
you could write. This is the headline output. *Verify:* the refusal log has ≥ 8 entries, each with a
concrete wished-for rule and the missing primitive.

**Stage 7 — Gap report + README.** Assemble `GAP_REPORT.md` (§4 format) from your running friction
log and reach the §3 verdicts. Write `README.md`.

---

## 2. GAME MECHANICS (implement exactly — no ambiguity)

### 2.1 The realm (seed as Uro canon, minimum scale)
- **≥ 6 factions:** `f:crown`, `f:vaelric` (martial), `f:corvane` (coastal trade), `f:dellmoor` (old,
  dwindling), `f:argent` (bankers' guild), `f:ashen` (hidden cult).
- **≥ 14 actors** across tiers, including deliberately confusable names to stress entity resolution:
  `Aldric Vaelric` (a:aldric-vaelric, tier 3, aliases `["the Marshal","Lord Vaelric","Aldric"]`),
  `Aldrice Corvane` (a:aldrice-corvane, tier 3, aliases `["Lady Corvane"]`), `Aldous Dellmoor`
  (a:aldous-dellmoor, tier 2), `King Halric` (a:halric, tier 4), plus **at least three near-identical
  minor knights** — `Ser Garret` / `Ser Garrick` / `Ser Gareth` — and `Aldric the Younger`
  (a:aldric-younger, Vaelric's nephew). Give every colloquially-referenced noble authored `aliases`.
- **≥ 6 places (holdings):** `p:capital`, `p:border-march`, `p:saltport`, `p:oldkeep`,
  `p:temple-district`, `p:silvermines`. Each starts owned by a House (encode ownership as an edge or
  place metadata you can read back).
- **≥ 12 threads (plots), designed to grow to dozens:** `t:succession`, `t:vaelric-corvane-feud`,
  `t:ashen-heresy`, `t:argent-debt`, `t:border-war`, `t:royal-betrothal`, `t:poison-plot`,
  `t:saltport-smuggling`, `t:crown-illness`, `t:dellmoor-decline`, `t:cult-infiltration`,
  `t:tax-revolt`. Mix `active`/`dormant` initial states.

### 2.2 Court beats (player intrigue verbs → beat intents)
Implement these as free-text intents the player issues to `engine.run_beat`; the narrator/extractor
turn them into claims. At minimum: **whisper/rumor**, **blackmail**, **bribe**, **broker-marriage**,
**incite-feud**, **investigate**, **sell-secret**, **assassinate** (see the death collision in §3).
Each beat may fire post-beat reaction rules.

### 2.3 The numeric ledger + downtime tick (game code — deterministic, seeded)
Keep a JSON ledger the game owns (this is the shadow state Uro *cannot* own; each field is a
refusal-log line):
```
House = { id, uro_faction, strength:int, gold:int, influence:int,
          holdings:[place_id], ambition:enum{expand,hoard,convert,ascend,survive}, loyalty:int }
tension: dict[(houseA,houseB) -> int]
```
On each `agenda_tick(branch, days)`, in lockstep, run:
1. **Income:** `gold += Σ HOLDING_VALUE[h] for h in holdings  −  UPKEEP * strength`.
2. **Ambition action:** `expand` → recruit `strength += gold // RECRUIT_COST` (spend it); `hoard` →
   bank; `ascend` → buy `influence`; `convert` → cult `spread_belief`; `survive` → consolidate.
3. **Tension:** for each hostile interaction since last tick, `tension[pair] += 1`; a brokered
   marriage/alliance resets that pair to 0; `tension[pair] >= WAR_THRESHOLD (5)` ⇒ **declare war**.
4. **War resolution:** for each `at_war` pair, `roll = strength + seeded_d6()*SCALE`; the loser cedes
   one holding and both lose strength proportionally; a House at `strength <= 0` is **broken**.
All constants deterministic; `seeded_d6` from your own seeded RNG so replay is byte-identical.

### 2.4 Reaction rules that the grammar CAN express — ship these (they must fire)
- **R1 feud-wakes-on-death:** `trigger ActorDied`, `when t:vaelric-corvane-feud dormant` →
  `set_thread_state active`; `scope thread`.
- **R2 war-breeds-rumor:** agenda `every_days:20`, `when edge_exists at_war_with(f:vaelric,f:corvane)`
  → `record_rumor` among `f:corvane`; `scope faction`.
- **R3 heresy-spreads:** agenda `every_days:30`, `when t:ashen-heresy active` → `spread_belief` among
  `f:ashen`; `scope faction`.
- **R4 alliance-spawns-counterplot:** `trigger edge_added allied_with` → `create_thread` (a
  counter-alliance plot); `scope faction`. *(Watch for the scope split — §3 target 2.)*
- **R5 scheduled-border-war:** agenda `when world_day > 90` → `add_edge at_war_with(f:vaelric,
  f:dellmoor)`; `scope faction`. *(Note the honest limit: a fixed date, not accumulated tension.)*
- **R6 succession-opens:** `trigger ActorDied` (King) → `set_thread_state t:succession active`;
  `scope thread`.

### 2.5 Reflecting sim results into Uro canon
- **War/alliance edges:** `append_beat(branch, [edge_added(src, "at_war_with"|"allied_with", dst)])`
  (or `remove_edge` where appropriate). Attempt the authored-event path; if `append_beat` rejects an
  event kind you need, log it.
- **Battles:** `distill_outcome(store, branch, OutcomeBundle{...})` — casualties among **tier 0/1
  retainers only** commit; loot needs a real owned item; feats become `truth=unknown` testimony that
  propagates to `witnesses`. Zero witnesses ⇒ nothing propagates (exercise this too).
- **Holding transfers:** change the place's ownership (edge/metadata) so `get_place` reflects the new
  holder — then, in Stage 4, narrate a beat referencing that place and confirm the narrator is blind
  to the change (§3 target 4).

---

## 3. URO STRESS GOALS (the named leftover-work this game must drive into — verdict required on each)

For **each** target: exercise it through gameplay as described, then in `GAP_REPORT.md` §4 give an
explicit verdict — *did you hit it? was the deferral the right call, or is it now blocking a real
consumer?* — with evidence (the call/file/line).

1. **Reaction-Layer EXPRESSIVENESS CEILING → the REFUSAL LOG (headline).** Push the realm sim
   through the declarative grammar until it refuses, then record every rule you wished you could
   write. **How gameplay drives it:** §2.3 is the whole numeric realm — none of it fits the grammar.
   Produce ≥ 8 refusal entries, each the *exact wished-for `rule_pack` rule*, e.g.:
   - *Tension counter + threshold + reset* — "`when tension(f:vaelric,f:corvane) >= 5` →
     `set at_war_with`; a marriage `resets tension to 0`." Needs a **per-pair counter + accumulation +
     reset**. Grammar has none.
   - *Economy* — "each downtime, `gold += Σ holding_value − upkeep*strength`; if `gold < 0` sell a
     holding." Needs **arithmetic + accumulating state + a conditional loop over holdings**.
   - *Comparative war trigger* — "`when strength(f:vaelric) > strength(f:corvane) * 1.2` → declare
     war." Needs **cross-entity numeric comparison**.
   - *Weighted outcome table* — "on war: 40% defection / 30% siege / 30% truce." Needs **weighted
     RNG / tables** (grammar is deterministic, no tables).
   - *Fall of a House* — "`when holdings(f:dellmoor) == 0` → mark Fallen and release every member as
     landless." Needs **counting to zero + iteration over members**.
   These are the ones that MUST appear (plus your own). This log is the evidence gate for the reserved
   WASM tier (D-33 Stage B) — **treat it as the primary output.**
2. **Single-dimension scope wrinkle.** A rule that must touch a **House (faction) AND a thread**
   forces a split into two scoped rules. **How gameplay drives it:** R4/R6 — e.g. "on the Marshal's
   death, activate `t:succession` AND remove `f:vaelric`'s alliance edge" cannot be one rule. Show the
   single rule you wanted, then the two you had to write; verdict on the ergonomics cost at scale.
3. **OQ-8 off-screen simulation / adaptation blast radius.** A war or a great death should **cascade**
   (allies dragged in, ally-of-ally goes wary, dependent plots activate, holdings change hands, rumors
   ripple to distant courts). The declarative layer does single-hop reactions only; `time_skip`/
   `agenda_tick` apply a header but **no LLM ripple**. **How gameplay drives it:** your §2.3 war
   resolution must compute the multi-hop cascade in game code and reflect each hop; log every cascade
   step the Reaction Layer could not do (transitive alliance traversal is the canonical refusal).
   Verdict: is single-hop-only a blocker for a realm sim?
4. **Place-state recall gap.** Holdings change hands but the narrator prompt does not see place-state
   (only active threads). **How gameplay drives it:** transfer `p:border-march` to a new House
   (§2.5), then run a beat like "describe the border march to me" and check via `assemble_recall`
   whether the narrator was told it changed hands. Verdict with the recall dump as evidence.
5. **Entity resolution at scale.** Many similarly-named nobles. **How gameplay drives it:** your
   `Ser Garret/Garrick/Gareth` + `Aldric` / `Aldric the Younger` / `the Marshal` cast; issue beats
   that refer to nobles colloquially and check `find_actor_by_name` + whether the extractor
   fragmented them into new actors. Record every fragmentation or false-merge; verdict on whether
   canonical-name + alias (no `entity_index`) suffices for a court of dozens.
6. **Thread lifecycle at scale.** Dozens of live plots created/escalated over many ticks. **How
   gameplay drives it:** seed ≥ 12, let R-rules/`create_thread` grow it; run ≥ 4 ticks; watch
   `list_threads` and whether recall floods with active plots. Is there any *retire/close* path
   beyond `set_thread_state`? Verdict on thread lifecycle management at scale.

---

## 4. DELIVERABLES

All under `examples/games/sable-court/`:
1. **The game** — runnable end to end with the **stub/scripted provider, no key**; a real model
   opt-in behind a flag. One documented entry command (e.g. `uv run python
   examples/games/sable-court/sable_court.py`).
2. **`GAP_REPORT.md`** — the scientific output, in the EXACT format below (so results compare across
   games).
3. **`README.md`** — what it is, how to run it (Postgres on 5433, migrate, run), which Uro postures/
   features it exercises (Posture A + Reaction Layer + agenda_tick + Chronicler + fork), and the
   headline finding.

### GAP_REPORT.md — required format (do not deviate)
```markdown
# Sable Court — Uro Gap Report

## 1. Summary
<One paragraph: did Uro's current surface support this game? What was the single biggest wall?>

## 2. Gap table
| Gap (what you wanted) | What happened (actual API/behavior/error/downgrade) | Workaround (or BLOCKED) | Severity (blocker\|major\|annoyance\|cosmetic) | What Uro would need (concrete engine change) | Evidence (call/file/line) |
|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | ... |

## 3. Top 3 things Uro MUST add for this game to be good
1. <ranked, tied to a specific gap row>
2. ...
3. ...

## 4. Verdict on targeted leftover-work
For each of the six §3 targets: **Hit? (yes/no)** · **Deferral right, or now blocking?** · evidence.
- Reaction-Layer expressiveness ceiling: ...
- Single-dimension scope wrinkle: ...
- OQ-8 blast radius: ...
- Place-state recall gap: ...
- Entity resolution at scale: ...
- Thread lifecycle at scale: ...

## 5. THE REFUSAL LOG (headline — the WASM-tier evidence gate)
Every realm rule the declarative grammar could NOT express. For each: the exact `rule_pack` rule you
wished you could write, and the missing primitive (counter / arithmetic / loop / table / traversal /
cross-entity compare).
    ### RL-1 — <name>
    Wished-for rule:
    ```jsonc
    { ... the rule as you'd write it if the grammar allowed ... }
    ```
    Missing primitive: <e.g. per-pair accumulating counter + threshold + reset>
    Where the game needed it: <call/file/line>
(≥ 8 entries.)
```

---

## 5. DEFINITION OF DONE / ACCEPTANCE

The game is done when **all** hold:
- **Runs deterministically with no key** on the stub/scripted provider; a clean-DB re-run is
  byte-stable.
- **Realm seeded at scale:** ≥ 6 factions, ≥ 14 actors (incl. the confusable-name set with aliases),
  ≥ 6 places, ≥ 12 threads.
- **A full session plays:** ≥ 8 court beats + ≥ 3 downtime `agenda_tick`s + **one fork** with a
  divergent replay; the two lines demonstrably disagree on ≥ 1 of {a war edge, a thread state, a
  rumor set}.
- **The Reaction Layer demonstrably fired:** assert (in code, printed) at least one thread-state flip
  from a committed event and one rumor/belief that landed as a `truth=unknown` claim.
- **The trust model was hit head-on:** a great-lord (tier ≥ 2) "death" via a Chronicler bundle is
  shown **downgraded to a rumor** (asserted: no committed actor death), and this collision is a gap
  row.
- **It genuinely stressed Uro:** `GAP_REPORT.md` is complete in the exact format, with a **REFUSAL
  LOG of ≥ 8 concrete wished-for rules**, the Top-3, and a verdict on **all six** named targets, each
  with real evidence (call/file/line). A report with an empty or hand-wavy refusal log means the
  experiment failed — the whole point is to find the computation ceiling and document it precisely.
