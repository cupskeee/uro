# THE SEVENTH VAULT — Build Task (TASK.md)

Build a runnable, deterministic, multiplayer heist game on top of Uro (Posture B WS + Posture A
library host), and produce a standardized GAP REPORT. Read `system_prompt.md` first, then
`examples/games/URO_INTEGRATION.md` and `examples/hello_uro/hello_uro.py`.

**Folder:** put everything under `examples/games/seventh-vault/`.

---

## 1. STAGED BUILD PLAN (execute + self-verify each stage before moving on)

Each stage ends **green** (its self-check passes) and appends any friction to `GAP_REPORT.md`.

### Stage 0 — Skeleton & determinism spine
- Create the folder, a `pyproject`/deps entry (reuse the workspace; depend on `uro_core`), a
  `README.md` stub, and an **empty `GAP_REPORT.md` with the required headings** (Section 4 below).
- Write a `run.sh`/`Makefile` target that brings up Postgres, migrates, and runs the default
  scripted arc.
- **Self-check:** `GET /healthz` returns ok against a locally-started `uro serve`.

### Stage 1 — The host / world builder (Posture A library)
- Implement `host.py`: create the world with the seven-layer geography, the House Guard faction, the
  Warden (tier 3), guards (tier 0/1), the `t:alarm` and `t:score` threads, and `i:prize`; carry the
  `HEIST_RULE_PACK` (Stage 5). Seat 4 crew PCs (`participant_id == token`) via `start_campaign` on
  one shared campaign. Bind `uro-basic`.
- Emit a **run manifest** (`run_manifest.json`: `campaign_id`, `branch_id`, crew `participant_id`s +
  `actor_id`s, place/thread/item ids) so clients and tests can find the campaign — **because there
  is no endpoint to discover it** (log this).
- **Self-check:** re-read via `active_pcs`, `list_threads`, `list_places`, `get_item` and assert the
  world matches the manifest.

### Stage 2 — One scripted WS client
- Implement `client.py`: connect to `ws://.../campaigns/{c}/play?token=…`, obey turn discipline
  (send intent → await `beat_committed` → wait for token), print/collect all frames.
- A `ScriptedPlayer` drives it from a canned list of intents; it holds and skips on
  `not_your_turn`.
- **Self-check:** a **single** scripted crew member plays 3 beats solo; assert 3 `beat_committed`
  frames and that `recent_beats(branch)` shows them.

### Stage 3 — The full crew, round-robin, shared scene
- Launch the server with 4 tokens (PartyArbiter). Run 4 `ScriptedPlayer`s concurrently, each with
  its own intent script. Verify the shared scene: all clients receive the **same** committed
  narration for each beat; the token rotates in order.
- **Self-check (deterministic):** with `stub` + fixed seeds, the concatenated committed-beat log is
  **byte-identical across two runs**; final `list_threads`/`items_owned_by` match a golden snapshot.

### Stage 4 — Infiltration mechanics (uro-basic checks + heat)
- Wire the layer progression and action→check mapping (Section 3). Actions escalate the alarm via
  committed events + the Reaction Layer. Loot and injuries land as items/sheets.
- **Self-check:** a scripted arc drives `t:alarm` `calm → suspicious → alerted → lockdown` and the
  crew reaches `p:seventh-vault`; assert each thread transition committed.

### Stage 5 — Reaction Layer (declarative heat) + its ceiling
- Author `HEIST_RULE_PACK`: post-beat rules that escalate `t:alarm` on the events beats commit, and
  a downtime agenda that spreads the crew's legend as a rumor after the job.
- **You WILL hit the no-counters wall** encoding "alarm level". Model it as the small **state enum**
  and record precisely what you couldn't express (a numeric heat meter, "3 failed checks → alert").
- **Self-check:** the pack drives at least one `t:alarm` transition purely from committed events (no
  client logic), and one `record_rumor`.

### Stage 6 — The guard response via Chronicler
- At `lockdown`, resolve a guard skirmish with **your own dice**, build an `OutcomeBundle`, POST it
  to `/outcome`. Then read back: guard casualties (tier 0/1) died; the **Warden downgraded to
  testimony**; witness rumors propagated; zero-survivor case → silence.
- **Self-check:** assert a tier-0 guard is dead in projections, the tier-3 Warden is NOT (a
  `truth=unknown` claim exists instead), and a surviving witness carries a rumor claim.

### Stage 7 — The double-cross (consensual PvP) & the endgame
- Implement the betrayal: a crew member, on their turn, acts to take `i:prize` from a fellow PC.
  Discover how far round-robin + Uro's PvP handling lets you go (per Phase-7, auto-resolved PvP
  against another PC falls back to free-roam). Record exactly what a real "consensual double-cross"
  needed.
- Resolve the getaway; the final **prize owner** is the score.
- **Self-check:** two endings replay deterministically — a clean crew escape (prize with the crew)
  and a betrayal ending (prize with the traitor) — each producing distinct, asserted final state.

### Stage 8 — The stress battery (drive the leftover-work on purpose)
- Add the explicit stress scenarios in Section 2 as runnable scripts under `stress/` (party-race,
  reconnect, out-of-turn lookout, vote, simultaneous action, one-ruleset). Each **captures evidence**
  and appends a GAP-REPORT row.
- **Self-check:** every Section-2 target has been *attempted* and has a verdict + evidence in the
  report.

### Stage 9 — Finalize the GAP REPORT & acceptance
- Fill `GAP_REPORT.md` to the exact spec (Section 4), including the per-target verdicts (Section 2).
- Confirm the Definition of Done (Section 6).

---

## 2. URO STRESS GOALS (enumerated — you MUST drive each; each needs a verdict + evidence)

For every item: build gameplay that *needs* it, hit the limit, and record the verdict in the report.

### S1 — Arbiter beyond round-robin (OQ-7) — the headline target
Round-robin `PartyArbiter` is the ONLY arbiter. The heist *wants* five shapes it can't do; drive
each and log **exactly which arbiter shape the game actually needs**:
- **Simultaneous action** — the crew wants "I pick the lock *while* you watch the hall" resolved as
  one beat. Round-robin forces sequential turns. → Attempt; log the desired *simultaneous/parallel*
  arbiter.
- **Proposal window** — the crew debates the plan before committing (stealth vs loud). No
  propose-then-act phase exists. → Log the desired *proposal-window* arbiter.
- **Crew vote** — "go loud?" decided by majority. No consensus arbiter. → Log *consensus/vote*.
- **Lookout out of turn** — a guard approaches on player C's turn; player A (lookout) must *interrupt*
  to warn. Round-robin gives no out-of-turn/interrupt. → Log *reactive/interrupt* arbiter.
- **Consensual PvP double-cross** — one crew member betrays another (Stage 7). Auto-resolved PvP
  against a PC falls back to free-roam. → Log the desired *consensual-PvP* arbiter shape.
- **Evidence:** the `not_your_turn` frames, the fall-back behavior, and the specific script that
  wanted each shape.

### S2 — Party co-combat + PC-anchored recall
Split the crew across layers (Ghost on the roof, Cracksman in the Gallery). Each player's PC should
"see the scene from their vantage." Drive whether Uro's recall/narration is anchored to the **acting
participant's PC** (Phase-7 threaded `participant_id`) and where it is NOT (does a beat narrate what
*this* PC could plausibly know, or the whole world?). During the guard response, test party
co-combat. **Verdict:** is per-PC vantage recall real, partial, or absent? Evidence: two PCs, two
beats, compare the recall each sees (`assemble_recall` / narration).

### S3 — The party-race / turn-token edge
Two clients deliberately send `intent` **at the same time** for the same turn (and one sends while
it's not their turn). Capture how the arbiter serializes it: who wins, does the loser get
`not_your_turn` or an error, is anything double-committed? **Verdict:** is the turn token race-safe?
Evidence: concurrent-send script + the frames both clients received.

### S4 — The missing REST management surface
You must build a lobby (create campaign, list players, join, show crew roster/state). Only WS play +
outcome + healthz exist. Everything else you did through the **library/CLI**. **Deliverable:** an
explicit, enumerated **"management endpoints I needed and Uro does not have"** list — for each: the
call you wanted (e.g. `GET /campaigns/{c}/roster`), what you did instead (which `store.*` call), and
whether a network-only (non-Python) client could have done it at all.

### S5 — Session lifecycle (join / leave / reconnect; turn state not event-sourced)
Have a scripted player **disconnect mid-heist and reconnect** (new WS on the same token). Test: does
their PC binding survive (it's event-sourced) while the **turn token** does (it's session state,
not)? Does a mid-turn drop wedge the party or rotate past them? Does a late `campaign join` seat a
5th player? **Verdict:** what breaks across a reconnect, and what turn/session state is lost because
it isn't event-sourced. Evidence: the reconnect script + before/after `active_pcs` and turn frames.

### S6 — One-ruleset-per-server-process
The stealth phase and the loud guard-response phase arguably want different mechanics, but the server
binds **one ruleset per process**. Attempt to want a second ruleset mid-heist. **Verdict:** did
one-ruleset actually constrain this game, or was `uro-basic` enough? Evidence: where a second ruleset
would have been used.

### S7 (secondary, drive if reached) — declarative Reaction Layer has no counters
The alarm is naturally a **counter** ("3 alarms = lockdown"). The grammar has no counters/arithmetic/
accumulating state. Record exactly the rule you wanted and why the enum-state workaround is lossy.
Also record the scope-splitting wrinkle if a rule wants to touch a thread AND an actor. Evidence: the
rule you couldn't write.

### S8 (secondary) — no game↔world time mapping
Your heist runs in rounds/turns, not world-days, but the Chronicler + downtime want world time. Log
what mapping you had to invent (and that you called `time_skip`/`agenda_tick` by hand).

---

## 3. GAME MECHANICS / RULES (implement exactly — no ambiguity)

**Ruleset:** `uro-basic` (d20; PCs have sheets with stats/hp). All checks resolve through Uro's beat
pipeline (the ruleset does the roll from the seeded RNG); your client never rolls for skill checks.

**The vault (places, in order):** `p:outer-gate → p:gallery → p:security-hub → p:antechamber →
p:seventh-vault`. The crew advances only by clearing each layer's obstacle.

**The crew (4 PCs, each a participant/token):**
| Role | actor / token | Strong at | pc_sheet emphasis |
|---|---|---|---|
| Cracksman | `crew-cracksman` / a:vesna | locks, mechanisms | DEX/INT |
| Face | `crew-face` / a:doran | social, distraction | CHA |
| Ghost | `crew-ghost` / a:sable | stealth, climbing | DEX |
| Muscle | `crew-muscle` / a:brakk | force, combat | STR/CON |
(2-player mode: Cracksman + Muscle. Solo scripted tests: any one.)

**Action → resolution:** a player's intent maps to a d20 check via `uro-basic` through `run_beat`.
Success advances/opens/loots; failure **escalates the alarm** (a committed event the Reaction Layer
reads). Suggested obstacle per layer: Outer Gate = stealth/lock; Gallery = a patrolling guard
(social or stealth); Security Hub = disable the alarm line (INT), *this is the pivot*; Antechamber =
the Warden's puzzle-lock; Seventh Vault = take `i:prize`.

**The alarm (`t:alarm`, shared thread state):** `calm → suspicious → alerted → lockdown`. It
escalates **only via committed events + Reaction-Layer rules** (never client-set). At `lockdown` the
guard response fires (Stage 6 Chronicler). Because Uro has no counters, alarm is an **enum**, not a
meter — record the loss (S7).

**The score (`t:score`):** `pending → prize-taken → escaped | caught`. `i:prize` starts owned by
`p:seventh-vault`; taking it transfers ownership to the taker's actor; the **final owner after the
getaway is the winner**.

**The double-cross:** on a betrayer's turn they attempt to take `i:prize` from another **PC**. This
is consensual PvP; discover and record how round-robin/Uro handle it (S1). The betrayal outcome (who
holds the prize at getaway) must be a real committed item-ownership state.

**The guard response (external, Chronicler):** at `lockdown`, your game rolls a simple skirmish
(deterministic dice from a fixed seed): each downed guard is a tier-0/1 casualty; feats and loot go
in the bundle; witnesses are surviving guards. Report via POST `/outcome`. Honor D-32 (Warden can't
die → testimony; zero survivors → no rumor).

**Determinism:** fixed seeds everywhere (world seed, PC seeds, your skirmish dice), `stub` provider,
canned intent scripts. Two default runs → byte-identical committed-beat log + identical final
projections.

---

## 4. DELIVERABLES

1. **The game** under `examples/games/seventh-vault/`, runnable with **`stub`, no key**:
   - `host.py` (Posture-A world builder + run manifest + roster/state reads),
   - `client.py` (WS client + turn discipline), `players.py` (scripted crew + intent scripts),
   - `heist.py` (layer progression, action mapping, alarm/score, double-cross, Chronicler skirmish),
   - `rule_pack.py` (the declarative `HEIST_RULE_PACK`),
   - `stress/` (one runnable script per Section-2 target, each emitting evidence),
   - a top-level `run.sh`/`Makefile` and an `arc.py` that runs the full default multiplayer arc,
   - a deterministic test that asserts the golden final state (mirror `test_example_hello_uro.py`).
   - **Real-model / real-human modes behind flags only** (`--provider openai`, a human WS client).
2. **`GAP_REPORT.md`** — the scientific output (exact format below).
3. **`README.md`** — what it is, how to run the deterministic arc, which Uro postures/features it
   exercises, and how to run the stress battery.

---

## 5. GAP_REPORT.md — EXACT REQUIRED FORMAT (do not deviate; results must be comparable across games)

```markdown
# GAP REPORT — The Seventh Vault

## 1. Summary
<One paragraph: did Uro's current surface support this game? What was the single biggest wall?>

## 2. Gap table
| Gap (what you wanted) | What happened (actual API/behavior/error/downgrade) | Workaround (or BLOCKED) | Severity for THIS game (blocker\|major\|annoyance\|cosmetic) | What Uro would need (concrete engine change) | Evidence (call/file:line) |
|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | ... |

## 3. TOP 3 THINGS URO MUST ADD for this game to be good
1. <ranked #1> — tied to gap-row: <which>
2. <ranked #2> — tied to gap-row: <which>
3. <ranked #3> — tied to gap-row: <which>

## 4. Verdicts on the targeted leftover-work
For each, state: did you HIT it? was the deferral the RIGHT call, or is it now BLOCKING a real consumer?
- **S1 Arbiter beyond round-robin (OQ-7):** hit? / which arbiter shape(s) the game actually needs / verdict.
- **S2 Party co-combat + PC-anchored recall:** hit? / real, partial, or absent / verdict.
- **S3 Party-race / turn-token edge:** hit? / race-safe? / verdict.
- **S4 Missing REST management surface:** the enumerated list of endpoints needed + verdict.
- **S5 Session lifecycle (join/leave/reconnect, non-event-sourced turn state):** what broke / verdict.
- **S6 One-ruleset-per-process:** did it constrain this game? / verdict.
- **S7 Reaction Layer has no counters (alarm):** the rule you couldn't write / verdict.
- **S8 No game↔world time mapping:** what mapping you invented / verdict.
```
Rules for the table: **one row per distinct gap**, real severity for *this heist* (not in the
abstract), and **every row must cite evidence** (the exact `store.*`/WS call and `file:line`).
Non-issues you expected but that turned out fine → note them in the Summary, not the table.

---

## 6. DEFINITION OF DONE / ACCEPTANCE

The game is DONE when **all** hold:

**It works (deterministic, no key):**
- [ ] `run.sh` (or the documented command) boots Postgres+migrate, launches `uro serve --provider
      stub`, runs 4 scripted crew, and completes a full heist with **zero API key**.
- [ ] The default arc is **byte-deterministic**: two runs produce an identical committed-beat log and
      identical final projections (`list_threads`, `items_owned_by`, `active_pcs`).
- [ ] The shared scene is real: all clients receive the same committed narration per beat; the
      round-robin token rotates correctly; `not_your_turn` is honored.
- [ ] The heist reaches a real ending in **committed Uro state**: `i:prize` has a definite final
      owner, `t:alarm` and `t:score` end in defined states, and both a clean-escape and a
      double-cross ending replay deterministically.
- [ ] The Chronicler leg lands: a tier-0/1 guard dies in projections, the tier-3 Warden does NOT
      (a `truth=unknown` claim exists instead), a surviving witness carries a propagated rumor, and
      the zero-survivor case propagates nothing.
- [ ] A deterministic test asserts the golden final state (like `test_example_hello_uro.py`).
- [ ] Uro is **unmodified** (no diffs to `uro_core`/`uro-server`/`uro-cli`/migrations/rulesets); all
      game code is inside `examples/games/seventh-vault/`.

**It genuinely stressed Uro (the real bar):**
- [ ] Every S1–S8 target was **attempted with running code** and has a **verdict + evidence** in the
      GAP REPORT.
- [ ] S1 names the **specific arbiter shape(s)** the game needs (not just "round-robin was limiting").
- [ ] S4 enumerates the **management endpoints** needed and what a non-Python client could NOT do.
- [ ] `GAP_REPORT.md` matches the exact Section-5 format, with a filled gap table (every row cited),
      a ranked Top-3, and all eight verdicts.
- [ ] `README.md` explains the run and which Uro features/limits it exercises.

**Acceptance headline test:** *One shared campaign, four scripted thieves, the stub GM — the crew
cracks the Seventh Vault; the alarm they left, the prize's final owner, who got caught, and the
rumor that spread are all read back from Uro's committed state, byte-identically on every run — and
the friction of getting there is written down as a rigorous, evidenced GAP REPORT.*
