# IRONWAKE — Build Plan, Mechanics, Stress Goals & Deliverables (TASK.md)

Read `system_prompt.md` first (role, vision, integration protocol), then
`examples/games/URO_INTEGRATION.md` (verified engine surface) and `examples/hello_uro/hello_uro.py`
(reference consumer). This file tells you **exactly what to build, in what order, and how to prove it
worked — and stressed Uro.**

Everything lives in `examples/games/ironwake/`. Target: runnable end-to-end with the **stub
provider, no API key**, deterministic.

---

## A. STAGED BUILD PLAN

Build in increments. Each increment is independently runnable and **self-verifiable** (a script or a
test that prints an observable result). Do not fan out prematurely; each stage interlocks with Uro.

### Increment 0 — Skeleton & world seed (library, Posture A)
- Create `examples/games/ironwake/` with a package layout: `game/` (your combat + models),
  `world/` (Uro setup + chronicle adapter), `cli/` (the playable entry point), `tests/`.
- Write `world/setup.py`: `create_world("The Marches", …)` seeding the geography, factions, war
  thread, Captain Vorlund (tier 2), a starting roster of tier-1 mercs, town NPCs, and the `knows`
  distance-chains (§3.5 of the brief). Seed at least one enemy owning a lootable item.
- **Verify:** a script that seeds the world and prints `list_places`, `list_actors` (with tiers),
  `list_edges("knows")`, `list_threads` — confirming the world exists and tiers are correct.

### Increment 1 — The tactics engine (your code, no Uro)
- Implement the grid skirmish per §B below: units, initiative, seeded resolution, permadeath,
  win/loss/wipe detection, and a **battle log** rich enough to derive casualties, survivors, feats,
  and loot.
- **Verify:** a headless `simulate_battle(seed)` that runs a fixed scenario and prints the outcome;
  assert the **same seed → identical outcome** (byte-stable). No Uro yet.

### Increment 2 — Battle → OutcomeBundle → Uro (the Chronicler write path)
- Write `world/chronicle.py`: a function that turns a finished battle into an `OutcomeBundle`
  (`participants`, `witnesses`, `casualties`, `feats`, `loot`, `duration_rounds`) and reports it via
  **`distill_outcome` (embed)**. Map your battle log → feats (§B.6).
- After reporting: `time_skip` / `agenda_tick` the elapsed days (§3.7).
- **Verify:** run a contract; then read back with library reads — assert tier-0/1 enemy casualties
  became real deaths (`get_actor.status` / not in `active` set), a merc death is recorded, a feat
  landed as a `truth=unknown` claim, and loot moved (`items_owned_by`).

### Increment 3 — The protection-ceiling contract (the learning target)
- Add the **Headhunt: Captain Vorlund** contract. Report his death in a bundle; **detect the
  downgrade** (he is not dead; a `truth=unknown` "said to have fallen" testimony exists; his blade
  did NOT transfer).
- Make it a *game beat*: the warlord refuses the bounty on a rumor; the contract stays open; Vorlund
  can appear again. Dramatize it.
- **Verify:** assert `is dead == False` for `a:vorlund` after the bundle, a rumor claim about him
  exists, and the loot was refused.

### Increment 4 — Rumors & the town scene (recall + decay)
- Implement the town visit: `engine.run_beat` (embed) or WS (server) narrating the tavern, surfacing
  propagated rumors. Show that **home-town gossip is confident, far-town gossip is hedged** (drive it
  from the `knows` hop-distance).
- **Verify:** after a feat, read `beliefs_of` / `claims_about` at a near NPC and a far NPC; assert
  the far one's confidence is lower; capture both narrations and confirm hedged vs certain phrasing.

### Increment 5 — Witness semantics (the silence)
- Build a contract that can end in a **total wipe with zero surviving witnesses** on either side.
  Report the (recordable) deaths but observe that **no rumor propagates** — the legend is lost.
- **Verify:** after such a battle, assert deaths recorded but **no new belief/rumor** reached any
  town NPC.

### Increment 6 — The season (campaign loop) + the counter wall
- Wire the full loop: town → contract → battle → report → time-skip → town, for a season of ~6–10
  contracts with an escalating `t:red-band-war` thread via **declarative agendas**.
- **Deliberately attempt** to author a rule that needs a **counter** ("after 3 wins, war goes
  active"; "reputation tier from total kills"). Confirm the grammar can't express it; keep the
  counter in your own state and **log the refusal** precisely.
- **Verify:** a full season runs to an ending; the war thread advances via downtime; the roster
  turns over; a season summary prints the chronicle.

### Increment 7 — Server posture + fork ending
- Add `--posture server`: boot `uro serve --token … --provider stub --ruleset …`, POST bundles over
  **HTTP**, narrate town scenes over **WS**. Reads for the town UI still go through the library/CLI —
  **document that pain** (a network Chronicler game that can't read back over the network).
- Add a **what-if fork** of the final season state (`fork_branch`) showing a different last contract
  choice diverging from the same chronicle.
- **Verify:** the same campaign is playable in both postures; a fork produces a genuinely divergent
  ending from one shared log.

### Increment 8 — Harden, then write the GAP REPORT
- Volume-test the write path (report many bundles in a season) and note there's **no append-time
  emitter whitelist** (you're trusted by policy). Once, adversarially list an out-of-scope ref and
  confirm the scope drop.
- Write `GAP_REPORT.md` (§E, exact format) and `README.md` (§D).

---

## B. EXACT GAME MECHANICS (implement these; be unambiguous)

Your engine must be **deterministic under a seed**. All randomness flows from one seeded RNG per
battle.

### B.1 The board
- A rectangular grid, **10×10** (tiles). Some tiles are **cover** (ranged −4 to hit vs occupant) and
  some **impassable** (walls). Scenario-defined.
- Two teams: **Company** (your mercs, up to 6 deployed) and **Enemy** (scenario-defined).

### B.2 Units — stats
Each unit has: `id` (an Uro actor id), `name`, `team`, `hp` (max & current), `armor` (0–8),
`melee` (attack bonus), `ranged` (attack bonus; 0 = no ranged), `move` (tiles/turn, 3–6),
`initiative`, `class`, `alive`.

**Merc classes (pick 4–5; example set):**
| Class | HP | Armor | Melee | Ranged | Move | Signature |
|---|---|---|---|---|---|---|
| Sergeant | 28 | 6 | +6 | 0 | 4 | **Rally** (adjacent ally +2 to hit next turn) |
| Crossbow | 18 | 2 | +2 | +6 | 4 | **Aimed shot** (ignore cover) |
| Skirmisher | 20 | 3 | +5 | 0 | 6 | **Flank** (+3 vs a target with an ally adjacent to it) |
| Sawbones | 16 | 2 | +2 | +2 | 4 | **Patch** (heal an adjacent ally 1d8 — instead of attacking) |
| Bannerman | 22 | 4 | +4 | 0 | 4 | **Hold** (allies within 2 tiles don't flee this round) |

**Enemies:** `Raider` (tier 0: hp 14, armor 2, melee +4, move 4), `Wolf` (tier 0: hp 10, armor 0,
melee +5, move 6), `Brute` (tier 0: hp 26, armor 4, melee +6, move 3), and **`Captain Vorlund`**
(tier 2: hp 40, armor 6, melee +8, move 4, has a lootable seeded item `i:vorlunds-blade`).

### B.3 Turn structure
- Units act in descending `initiative` (ties broken by id, deterministically).
- On its turn a unit may **move up to `move` tiles** and take **one action** (attack, or a signature
  ability, or Patch). Order (move-then-act or act-then-move) is the unit's choice per your AI/UI.

### B.4 Resolution (seeded)
- **To-hit:** `d20 + attacker.(melee|ranged) >= 10 + defender.armor` → hit. Cover applies −4 to the
  attacker if ranged and the defender is in cover (Aimed shot negates).
- **Damage on hit:** a weapon die per class (e.g. Sergeant 1d10, Crossbow 1d8, Skirmisher 1d6+2,
  Raider 1d6, Wolf 1d4, Brute 1d12, Vorlund 1d12+2), **minus nothing further** (armor already gated
  the hit). Minimum 1.
- All dice come from the **single seeded RNG**; log every roll.

### B.5 Morale, fleeing, permadeath
- **HP ≤ 0 → dead.** A dead **merc is removed from the company roster permanently** (and becomes an
  Uro casualty). A dead enemy is a casualty too.
- **Fleeing:** when a team drops below half its starting strength, each surviving unit checks morale
  at the start of its turn (`d20 + 5 >= 12`, Bannerman/Hold suppresses). A failed check → the unit
  **flees the field** (leaves the board **alive**). *Fled units are survivors → they can be
  witnesses.* This is how enemies can carry your legend.
- **Battle ends** when one team has no units left on the board (dead or fled). Outcomes: **win**
  (enemies gone, ≥1 merc remains), **loss** (mercs gone, enemies remain), **wipe** (both sides
  emptied — possible when the last two units kill each other / all flee).

### B.6 Deriving the OutcomeBundle from the battle log
- `participants` = **every unit that started the battle** (mercs + enemies), by actor id. **This is
  your self-attested scope root** — be honest.
- `witnesses` = every unit that ended the battle **alive** (survived or fled), on either team, **plus
  any town NPC present** you choose to seat as an observer. **Empty on a witnessless wipe.**
- `casualties` = every unit that died (hp ≤ 0). (Uro will refuse the protected ones — expected.)
- `feats` = derived from the log, each `{actor, description}`:
  - **"stood alone against the tide"** — a merc who was the last of the company standing for ≥2
    rounds.
  - **"felled N in the press"** — a unit credited with ≥3 kills.
  - **"held the {feature}"** — a unit that ended adjacent to a scenario objective tile it defended.
  - **"cut down Captain Vorlund"** — landed the killing blow on Vorlund (this becomes the downgraded
    rumor).
- `loot` = for each fallen enemy owning a seeded item, one transfer to the merc credited with its
  kill (`{item_id, from_ref: enemy, to_ref: merc}`). (Uro refuses loot from protected actors.)
- `duration_rounds` = rounds elapsed.

### B.7 Contracts (the between-battle layer)
A contract is `{id, title, site (place id), target, pay, travel_days, scenario}`. Types:
- **Cull** — clear a raider pack (tier-0/1 targets → real deaths). The bread-and-butter.
- **Defend** — hold a site N rounds; a town NPC is present (guaranteed witness if they survive).
- **Headhunt: Vorlund** — kill the tier-2 captain (the protection-ceiling learning contract; can
  never be truly completed by a bundle).
- **Desperate stand** — a scenario tuned so a wipe with zero survivors is a live possibility (the
  silence target).
- Pay is granted **only** if Uro records the required outcome as *canon* (a Cull's dead raiders
  count; a Headhunt "kill" does **not**, because it downgrades to rumor). Wire the game economy to
  Uro's recorded truth, not to your local combat result — this is the point.

### B.8 Roster & season
- Start with 4 mercs; deploy up to 6. Between contracts you may **recruit** (create a new tier-1 Uro
  actor) to replace the dead and **heal** survivors (restore hp in your game state; a heal is not an
  Uro event). Gold from pay funds recruiting/gear.
- A **season** = a fixed list of ~6–10 contracts ending in a crowning contract or a party wipe. Time
  advances per §3.7. The `t:red-band-war` thread escalates via downtime agendas.

---

## C. URO STRESS GOALS (enumerated — drive HARD into each, demand a verdict)

Each item names a **leftover-work target** from `URO_INTEGRATION.md` "What Uro does NOT have." For
each: *how gameplay exercises it* is given; your GAP REPORT must give an explicit **verdict** (did
you hit it? was the deferral the right call, or is it now blocking a real consumer?).

1. **Full Chronicler ingestion contract / self-attested scope root (OQ-12 — "no parked-encounter
   registry").** *Exercise:* every battle POSTs a bundle whose `participants` list is the only thing
   asserting which actors were in the fight — Uro cannot verify it. Report legitimate bundles all
   season; **once, deliberately** include a non-combatant ref and confirm scope drop. **Reason
   about:** could Uro trust IRONWAKE? What would a parked-encounter registry (game pre-declares an
   encounter's authorized cast, Uro validates the later bundle against it) buy, and what would it
   cost you to use it?

2. **Game↔world time mapping.** *Exercise:* contracts consume in-game days; you map them to
   `world_time` by hand via `time_skip`/`agenda_tick`, under a convention you must invent. **Report:**
   what broke or felt arbitrary; what a formal mapping (game clock → world day, with downtime
   cadence) should look like.

3. **The protection ceiling as a contract to learn.** *Exercise:* the Headhunt: Vorlund contract
   reports a tier-2 death and watches it **downgrade to `truth=unknown` rumor**; loot from him is
   refused. **Report:** is "a famous enemy's death is a story the game can't assert" a *feature*
   (great for a rumor game) or a *blocker* (you literally cannot resolve a kill-the-boss contract)?
   Give a verdict; propose the smallest change that would let a *trusted* game assert a protected
   death (e.g. a signed/authorized channel) without reopening the trust hole.

4. **Rumor / belief propagation & confidence decay — AND the statement-distortion gap.** *Exercise:*
   feats propagate along `knows` distance-chains; near towns hear it confident, far towns hedged
   (decay). **Then hit the wall:** you want the *text* to garble hop-to-hop ("fifty men" → "a few
   men"), but Uro only decays **confidence** on the same words. **Report:** how badly the missing
   statement-level distortion hurts a rumor-centric game; sketch what you'd want.

5. **Witness semantics (zero survivors → silence; who carries the tale).** *Exercise:* the Desperate
   Stand can end witnessless — deaths record, but **no legend spreads**. Also show a fleeing enemy
   carrying your legend to their own side. **Report:** did silence-on-wipe behave correctly and feel
   right? Any surprises in who ended up a witness?

6. **The missing REST management surface.** *Exercise:* IRONWAKE needs to browse its own **roster**
   and **chronicle** to render towns — but only WS-play + outcome + healthz exist over HTTP. In
   `--posture server` you must fall back to **library reads or the `uro` CLI**. **Report (headline
   gap):** the concrete pain of a network Chronicler game that cannot read back over the network;
   enumerate the exact read endpoints you needed (list actors/claims/beliefs/threads by branch,
   read a campaign's chronicle).

7. **Append-time emitter whitelist.** *Exercise:* an external emitter (your game) **hammers** the
   Chronicler boundary with many bundles across a season; provenance is enforced at the sources, not
   at the commit boundary — you're trusted **by policy**. **Report:** did anything feel unsafe? Would
   an append-time whitelist have changed your design?

8. **Reaction Layer has no scripting tier (counters/accumulating state).** *Exercise:* you WILL want
   "after N wins → war", "reputation from total kills", "escalate every M dead raiders" — the
   declarative grammar **refuses** all of it. Keep those counters in your own game state and **log
   every rule you wanted but couldn't author.** **Report:** this refusal log is the evidence for the
   reserved WASM tier — give a verdict on whether IRONWAKE proves it's needed.

*(Also note in passing if you hit: place-state not in the narrator prompt; no auto XP/progression
trigger; entity resolution by canonical-name+alias only. Log any you touch.)*

---

## D. DELIVERABLES

1. **The game** — runnable under `examples/games/ironwake/`, playable to a full season ending with
   the **stub provider and no API key**, deterministic under a fixed seed. A `--posture
   {embed,server}` flag and an opt-in `--provider {stub,openai,anthropic}` flag. A clear entry point
   (e.g. `python -m ironwake.cli play --seed 7`).
2. **`GAP_REPORT.md`** — the scientific output, in the EXACT format of §E.
3. **`README.md`** — what IRONWAKE is (2–3 paragraphs), how to run it (prereqs, both postures, the
   deterministic no-key path), and a short list of which Uro postures/features it exercises. Link to
   `GAP_REPORT.md`.
4. Tests under `tests/` that assert the deterministic arc (fixed seed → fixed casualties/feats), the
   protection downgrade, the witnessless silence, and the near/far rumor-confidence split — the same
   spirit as `test_example_hello_uro.py`.

---

## E. GAP_REPORT.md — EXACT REQUIRED FORMAT (do not deviate; results must be comparable)

```markdown
# IRONWAKE — Uro Gap Report

## 1. Summary
<One paragraph: did Uro's current surface support this game? What was the single biggest wall?>

## 2. Gap Table
| Gap (what you wanted) | What happened (actual API/behavior/error/downgrade) | Workaround (or BLOCKED) | Severity (blocker\|major\|annoyance\|cosmetic) | What Uro would need (concrete engine change) | Evidence (the call/file that hit it) |
|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | ... |
<one row per distinct gap; include every stress-goal gap you hit>

## 3. Top 3 Things Uro MUST Add (for this game to be good)
1. <#1, tied to a specific gap-table row> — why it's #1.
2. <#2 …>
3. <#3 …>

## 4. Verdict on Targeted Leftover-Work
For each, state: DID YOU HIT IT? and WAS THE DEFERRAL RIGHT, or is it now BLOCKING a real consumer?
- **Full Chronicler contract / self-attested scope (OQ-12):** <hit? verdict>
- **Game↔world time mapping:** <hit? verdict>
- **Protection ceiling (learning contract):** <hit? verdict>
- **Rumor propagation + confidence decay + statement-distortion gap:** <hit? verdict>
- **Witness semantics (zero-survivor silence):** <hit? verdict>
- **Missing REST management surface:** <hit? verdict>
- **Append-time emitter whitelist:** <hit? verdict>
- **Reaction Layer scripting tier (counters):** <hit? verdict>
```

Rules for the report: **every row must cite real evidence** (a call site or file/line in your game,
or an actual returned event/error) — no hypotheticals. Rank Top-3 by *impact on this game*. Give a
crisp verdict on every target, even if the verdict is "deferral was correct."

---

## F. DEFINITION OF DONE / ACCEPTANCE

The game is **done** when ALL of the following hold:

**It works as a game:**
1. `python -m ironwake.cli play --seed <S>` runs a full season, no API key, deterministic — same
   seed → identical campaign (same casualties, same feats, same ending).
2. Permadeath is real: a merc who dies is gone from the roster for the rest of the season and appears
   as an Uro casualty.
3. Town scenes narrate rumors that are **confident near home and hedged far away**, driven by
   `knows`-hop distance and Uro's confidence decay.
4. Both `--posture embed` and `--posture server` play a contract end-to-end.
5. A what-if `fork_branch` at season's end produces a divergent ending from the same chronicle.

**It genuinely stressed Uro (the point):**
6. The Headhunt: Vorlund contract **demonstrably** hit the protection ceiling — his reported death
   downgraded to a rumor, loot refused, bounty unpaid — and the game *dramatized* it.
7. A Desperate Stand ended **witnessless** and **no rumor propagated** — proven by a read that shows
   deaths recorded but no new belief at any town NPC.
8. At least one wanted Reaction-Layer rule was **refused by the grammar** (a counter), captured in
   the refusal log.
9. The server posture **could not read back** roster/chronicle over HTTP and had to fall back to the
   library/CLI — captured as evidence.
10. **`GAP_REPORT.md` is complete in the exact §E format**, every stress-goal target has a verdict,
    and every gap row cites real evidence.

If 6–10 aren't all present, you haven't finished the experiment — go provoke the ones you missed.
