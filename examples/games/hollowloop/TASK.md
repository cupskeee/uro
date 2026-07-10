# HOLLOWLOOP — TASK

Build the game described in `system_prompt.md`, in **`examples/games/hollowloop/`**, embedding
`uro_core` (**Posture A**). Deterministic with the scripted provider (no key). Then produce the
**standardized `GAP_REPORT.md`**. Read `../URO_INTEGRATION.md` and `../hello_uro/hello_uro.py`
first.

---

## 1. STAGED BUILD PLAN (each stage is runnable and self-verifiable)

> After each stage, run it and eyeball the assertion listed. Commit per stage. Keep the friction
> log (`GAP_REPORT.md` scratch) updated *as you go*.

### Stage 0 — Skeleton + world genesis
- `examples/games/hollowloop/` with `game.py` (entry CLI), `world.py` (the seed events + rule pack),
  `script.py` (the scripted provider + the intent→(narration, extraction) table), `codex.py` (the
  meta-knowledge store), `README.md`, `GAP_REPORT.md`.
- Boot store, `create_world(...)` with all places/actors/threads/items, `start_campaign(...)`, bind
  the PC.
- **Verify:** `list_actors(BRANCH)` returns the full cast; `is_pc(BRANCH, "a:pc")` is `True`;
  `current_world_time(BRANCH)` reads segment 0.

### Stage 1 — The ORIGIN ref + one manual loop
- Establish `ORIGIN_REF` with a named marker: `store.create_marker(world_id, "origin", branch_id)`
  (these primitives exist — see system_prompt); fork every loop from the marker name.
- `fork_branch(world_id, ORIGIN_REF, "loop-0001")`; rebind the campaign onto the forked branch; play
  2-3 hard-coded beats; `time_skip(loop_branch, 1)` between them.
- **Verify:** the loop branch's `current_world_time` advances; `list_claims(loop_branch)` shows the
  beats' extracted facts; `list_claims(ORIGIN_REF branch)` is **unchanged** (fork isolation holds).

### Stage 2 — The schedule, the clue graph, the playable menu
- Implement the segment clock (7 segments), the NPC schedule, and the clue-gating menu (Section 2).
- The scripted provider returns, per recognized intent, the canned narration and an extraction JSON
  that commits the clue claim when (and only when) the intent's place/segment/prereqs are satisfied.
- **Verify:** a full loop is playable end to end via the CLI; talking to Elder Aldis at the chapel
  in the morning commits clue **K1** as a claim on that loop's branch; the same intent at the wrong
  place/segment does not.

### Stage 3 — The Codex (cross-fork meta-knowledge) + knowledge-gated intents
- `codex.py`: a game-side ledger (a JSON file **or** a dedicated never-forked Uro branch — pick one
  and justify) of every clue id the Loopwalker has ever discovered, across all loops.
- New loops read the Codex to widen the intent menu (e.g. K1-known unlocks the "press Sela on the
  ward" intent). **Prove the boundary:** clues in the Codex are *not* present in a fresh
  `fork_branch(ORIGIN_REF)` world state (the engine resets them); the Codex is what carries them.
- **Verify:** discover K1 in loop-0001; start loop-0002; `list_claims(loop-0002 branch)` does **not**
  contain K1 (world reset), but the CLI *does* offer the K1-gated intent (Codex carried it).

### Stage 4 — The Fall (the meteor), committed
- At the doom segment, commit `place_destroyed(place_id="p:vale", cause="the Fall")` (verify the constructor + commit path; log if
  the game can't emit it directly and use the fallback). Escalate `t:doom` via the Reaction Layer as
  the day wears on so the narrator feels the dread.
- **Verify:** on a loop played to nightfall, reading that branch shows the Vale destroyed / `t:doom`
  terminal; the **next** fork from `ORIGIN_REF` is pristine again.

### Stage 5 — what-if forks + the loop tree UI
- `whatif` command: `fork_branch(current_commit, "whatif-...")` mid-loop; you can play the sideways
  branch and then return to your main loop.
- `loops` command: render the whole fork tree (loop number, branch id, segment reached, ending);
  `codex` command: render which loop discovered which clue. **This is the legibility requirement** —
  the commit graph must be visible to the player.
- **Verify:** `loops` shows loop-0001, its what-if child, and loop-0002 as distinct lines with
  distinct outcomes.

### Stage 6 — Break the loop (the win) + the scale run
- When the Codex holds **K1-K4** and the player is at the tower at the doom segment with
  `i:tower-key`, the `ring-the-bell` intent averts the Fall: commit the aversion, `t:doom → warded`,
  mark `m:broke-the-loop`. Winning ending.
- **The scale harness** (`scale.py` or `--scale N`): drive **N automated loops** (default N=60, must
  support N≥200) — each: fork from origin, play the full 7-segment schedule choosing clue-optimal
  intents, hit the Fall (or break on the winning loop), record timings. **Instrument
  `fork_branch` latency, any materialize-at-commit latency, per-loop event count, and total
  branches/events**, and dump a CSV/table.
- **Verify:** the scale run completes at N=60 without error and emits the timing table; run it again
  at the largest N you can and record where (if anywhere) it strains — that data *is* the branching
  gap evidence.

### Stage 7 — Finalize the GAP REPORT + acceptance
- Distill the friction log into `GAP_REPORT.md` (exact format in Section 4). Fill the per-target
  verdicts (Section 3). Confirm Definition of Done (Section 5).

---

## 2. EXACT GAME MECHANICS (implement precisely; numbers are tunable, semantics are not)

**Segments / the loop clock.** One loop = one in-fiction day = **7 segments**, mapped to `world_day`
0-6 via `time_skip(branch, 1)` after each beat:

| seg | world_day | name |
|----|----|----|
| 0 | 0 | dawn |
| 1 | 1 | morning |
| 2 | 2 | noon |
| 3 | 3 | afternoon |
| 4 | 4 | dusk |
| 5 | 5 | last light |
| 6 | 6 | **the Fall** (doom) |

**Places (Uro `place_created`):** `p:vale` (the whole village, the meteor target), `p:square`,
`p:chapel`, `p:forge`, `p:well`, `p:manor`, `p:tower` (the Sky-Bell). The player `go`es between
them (game-side location; being *at* a place is a precondition for its intents).

**Cast (Uro `actor_created`, note tiers — they matter for the trust model if you touch Chronicler):**

| id | name | tier | role | where (by segment) |
|----|----|----|----|----|
| `a:pc` | the Loopwalker | — (PC) | player | player-controlled |
| `a:aldis` | Elder Aldis | 2 | elder | chapel 0-2, manor 3-6 |
| `a:sela` | Chaplain Sela | 1 | chaplain | chapel 0-6 |
| `a:wren` | Wren (the child) | 0 | child | square 0-2, well 3-4, square 5, hidden 6 |
| `a:bryn` | Bryn the Smith | 1 | smith | forge 0-6 |
| `a:harrow` | Harrow the Stranger | 1 | harbinger | square 0, well 1-2, tower 3-6 |

**Items (Uro `item_created`):** `i:tower-key` (owned by `a:wren`), `i:bell-hammer` (owned by
`a:bryn`), `i:star-chart` (owned by `a:aldis`).

**The four keystone clues** (each is a durable Uro **claim** committed on discovery, and a Codex
entry that persists across forks):

| clue | what you learn | discovered by | prerequisite |
|----|----|----|----|
| **K1** `c:nature` | the Fall is a falling star that strikes at last light | talk Aldis @ chapel, seg 0-2 | none |
| **K2** `c:ward` | the Sky-Bell can ward the Fall if rung at the moment it strikes | talk Sela @ chapel | **K1** known |
| **K3** `c:key` | Wren hid the tower key in the well; get `i:tower-key` | talk Wren @ well, seg 3-4 | none |
| **K4** `c:timing` | the star falls at nightfall (seg 6) and the bell must ring *then* | witness a full loop to the Fall **or** talk Harrow @ tower seg 3-6 (needs K1) | K1 (Harrow path) |

**Intents & gating.** The CLI shows a menu built from: (a) always-available *movement/look/wait*
intents; (b) *talk* intents for NPCs present at the player's current place+segment; (c)
*knowledge-gated* intents unlocked by Codex clues. Each recognized intent maps in `script.py` to a
`(narration, extraction_json)` pair. The extraction JSON commits the clue claim **only** when
place+segment+prereqs match (the game decides eligibility before selecting the script entry;
otherwise it selects a "nothing learned" entry). Same intent → identical result in any loop
(deterministic).

**Discovering a clue** = the beat's extractor commits the claim (`truth=true`, provenance narrator)
**and** the game records the clue id in the Codex.

**Ending a loop.** Reaching seg 6 without the ward → the **Fall**: commit `place_destroyed(place_id="p:vale", cause="the Fall")`
(or the logged fallback), record the loop's outcome (`fell @ seg 6`), fork the next loop from
`ORIGIN_REF`. Death mid-loop (optional: a hazardous intent) → same, earlier segment.

**Winning.** Codex ⊇ {K1,K2,K3,K4} **and** player holds `i:tower-key` **and** is at `p:tower` at
seg 6 → the `ring-the-bell` intent averts the Fall: commit the aversion (`t:doom → warded`, an
aversion claim, mark `m:broke-the-loop`). The Vale survives past nightfall → win screen.

**Commands (CLI):** `look`, `go <place>`, `talk <npc>` / numbered intent menu, `wait`, `whatif`
(sideways fork), `loops` (fork-tree view), `codex` (cross-loop clue view), `ring` (win intent when
eligible), `quit`. Plus non-interactive `--scale N` (Stage 6) and `--provider {stub,openai,...}`.

**Reaction Layer (`RULE_PACK`, inline at `create_world`).** Downtime agendas that escalate dread
(declarative only): e.g. `t:doom` `looming → gathering → imminent` on a segment cadence, and a
`record_rumor` among villagers ("the sky feels wrong today") as segments pass, so `assemble_recall`
feeds the narrator rising tension. **Remember the grammar has no counters/arithmetic/state** — when
you want something it can't express (e.g. "escalate after the 3rd visit"), log it, don't fake it.

---

## 3. URO STRESS GOALS (drive into each; every one demands a verdict in `GAP_REPORT.md`)

These are the specific **leftover-work targets** this game exists to test. For each: **how the
gameplay exercises it**, and a required **verdict** (hit? was the deferral right, or is it now
blocking a real consumer?).

1. **Branching / materialization AT SCALE** *(target: graph/vector-store-swap + snapshot-tuning +
   the meteor machinery under load).*
   - *Exercised by:* the Stage-6 scale run — 60-200+ loops, each a `fork_branch(ORIGIN_REF)` plus 7
     beats + 6 `time_skip`s, each ending in a committed Fall. Instrument `fork_branch` latency,
     materialize-at-commit latency, per-loop and total event counts.
   - *Verdict must answer:* does fork latency stay flat as branch count and total event count grow?
     Where does the ~50-commit snapshot cadence help or hurt (origin is an early commit — is
     materialize-at-origin cheap and stable, or does it degrade)? At what N does anything strain? Is
     Postgres-as-graph/vector-store adequate here, or is this concrete evidence for a specialized
     store?

2. **Fork-from-past + marker management** *(target: a stable origin marker; naming/listing many
   branches).*
   - *Exercised by:* every loop forks from the **one fixed** `ORIGIN_REF`; what-if forks fork from
     arbitrary mid-loop commits; `loops` must enumerate all branches.
   - *Verdict must answer:* `create_marker`/`list_markers`/`list_branches`/`resolve_ref` all exist —
     did they hold up at SCALE (hundreds of markers/branches: naming, listing, querying), or did the
     game end up keeping its own branch registry anyway? Does fork-from-a-long-past ref behave the
     same (perf/correctness) as fork-from-recent, once many snapshots sit between them?

3. **Knowledge carry ACROSS forks — the engine/game boundary** *(target: what Uro carries vs what
   the game must track as meta-knowledge).*
   - *Exercised by:* clues are Uro claims on a loop branch (world-side, reset each fork); the Codex
     is game-side meta-knowledge that survives forks from origin.
   - *Verdict must answer:* precisely what does a fork inherit (parent lineage: memory-index rows,
     claims, PC binding) vs. NOT (sibling loops forked from the same origin)? Is a game-side Codex
     the *right* boundary, or does a real time-loop consumer need an engine-level "player knowledge /
     cross-branch persistent memory" concept? Did you consider a dedicated never-forked Uro branch
     for the Codex, and how did that compare?

4. **Cross-fork / aggregate queries** *(target: no REST/aggregate query surface — "compare
   branches").*
   - *Exercised by:* `codex` and `loops` want to compare outcomes across all loops (which loop found
     which clue, when each Vale fell, which endings occurred).
   - *Verdict must answer:* there is no cross-branch/aggregate query API — you had to iterate
     branches and query each head. Document the **exact shape of a "compare branches" API** this game
     needed (inputs, outputs, e.g. `diff_branches(a, b)`, `query_across([...], projection)`), and how
     painful the N-branch fan-out was at scale.

5. **PC binding across forks** *(target: PC-ness through the fork.)*
   - *Exercised by:* the PC is bound once at origin; every loop is a fork; the win/what-if forks must
     keep the same Loopwalker as PC.
   - *Verdict must answer:* does `is_pc` / the PC binding survive `fork_branch` from origin without
     re-binding? Did rebinding the `campaign` onto each forked branch work cleanly, or was it awkward
     (a finding)? Any surprises with `active_pcs` on forked branches?

6. **Time within a loop (`time_skip` / `world_day` as the loop clock)** *(target: intra-loop time.)*
   - *Exercised by:* the 7-segment day mapped onto `world_day` 0-6 via `time_skip(1)` per beat; the
     Fall keyed to `world_day == 6`; the Reaction-Layer dread cadence.
   - *Verdict must answer:* `world_day` is **day-granular** — you borrowed whole "days" as
     intra-day segments. Is the lack of sub-day time a real limitation for a time-loop game? Did the
     Reaction Layer's day-cadence agendas line up with segments, or fight the clock? Note the
     missing **game↔world time mapping** (URO_INTEGRATION item 2).

> Also opportunistically log anything from URO_INTEGRATION's "What Uro does NOT have" list you brush
> against (place-state not in the narrator prompt; declarative-only reactions with no counters;
> no auto-progression; entity resolution being name+alias only; etc.).

---

## 4. DELIVERABLES

1. **The game** — runnable end to end with the `stub`/scripted provider, **no API key**, in
   `examples/games/hollowloop/`. Interactive play **and** `--scale N`. A real model behind
   `--provider` (optional, never required).
2. **`GAP_REPORT.md`** — the scientific output (exact format below).
3. **`README.md`** — what HOLLOWLOOP is, how to run it (interactive + scale), which Uro postures and
   features it exercises, and the one-line "biggest wall" teaser.

### `GAP_REPORT.md` — EXACT REQUIRED FORMAT (do not deviate; results must be comparable across games)

```markdown
# HOLLOWLOOP — Uro Gap Report

## 1. Summary
<One paragraph: did Uro's current surface support this game? What was the single biggest wall?>

## 2. Gap table
| Gap (what you wanted) | What happened (actual API/behavior/error/downgrade) | Workaround (or BLOCKED) | Severity for THIS game (blocker\|major\|annoyance\|cosmetic) | What Uro would need (a concrete engine change) | Evidence (the call/file that hit it) |
|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | ... |

## 3. Top 3 things Uro MUST add for this game to be good
1. <ranked, tied to a specific row in the table above>
2. ...
3. ...

## 4. Verdicts on targeted leftover-work
For each targeted item, an explicit verdict: did you hit it? Was the deferral the right call, or is
it now blocking a real consumer?
- **Branching/materialization at scale:** <verdict + the timing evidence from the scale run>
- **Fork-from-past + marker management:** <verdict>
- **Knowledge carry across forks (engine vs game boundary):** <verdict>
- **Cross-fork/aggregate queries ("compare branches"):** <verdict + the API shape you needed>
- **PC binding across forks:** <verdict>
- **Time within a loop (time_skip/world_day):** <verdict>
```

- **Severity is scored for THIS game** (a time-loop roguelike), not in the abstract: a slow
  `fork_branch` is a *blocker* here even if a one-campaign game wouldn't care.
- Every table row's **Evidence** column must cite a real call and file:line in your code (or a
  captured error / timing number). No evidence → not a row.
- The Section-4 scale verdict must include the **actual measured** fork/materialize timings and the
  largest N you reached.

---

## 5. DEFINITION OF DONE / ACCEPTANCE

The game is done when **all** hold:

**It plays (proves the game works):**
- [ ] `python game.py` (stub provider, no key) runs an interactive loop: you can `look`, `go`,
      `talk`, `wait`, advance segments, and reach the Fall.
- [ ] A clue discovered in one loop unlocks a gated intent in a **later** loop, while
      `list_claims(later_loop_branch)` confirms the world itself reset (Codex carried the knowledge,
      Uro did not) — the knowledge-boundary is demonstrated live.
- [ ] Reaching seg 6 commits the **Fall** (`place_destroyed(place_id="p:vale", cause="the Fall")` or the logged fallback) on that
      loop's branch; the next fork from `ORIGIN_REF` is pristine.
- [ ] Assembling K1-K4 + the key + being at the tower at seg 6 → `ring` **breaks the loop** (aversion
      committed, `m:broke-the-loop` marked, win screen). This is the meteor-test-as-a-game passing.
- [ ] `loops` renders the fork tree and `codex` renders cross-loop clue discovery — the branching is
      **legible to the player**.

**It stressed Uro (proves the science):**
- [ ] `--scale 60` completes and emits the instrumented timing table; you also ran the largest N you
      could and recorded the result.
- [ ] The scale run created **dozens-to-hundreds of real branches**, all forked from the fixed
      `ORIGIN_REF`, with fork/materialize latencies measured and reported.
- [ ] `GAP_REPORT.md` is filled in the exact required format, every row has evidence, and **all six**
      targeted leftover-work items in Section 3 have an explicit verdict.
- [ ] The friction log was kept continuously (not reconstructed at the end) and at least the
      branching-at-scale and cross-fork-query targets have concrete, quantified findings.

**It stayed honest:**
- [ ] `uro_core` / `packages/` untouched (Uro consumed as a dependency).
- [ ] No shadow world-state that Uro should own; the only game-side persistence is the explicitly
      documented Loopwalker Codex (meta-knowledge).
- [ ] No invented Uro APIs — anything wished-for that didn't exist is a `GAP_REPORT.md` row, not a
      guess in code.
```
