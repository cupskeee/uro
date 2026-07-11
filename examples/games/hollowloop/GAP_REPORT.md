# HOLLOWLOOP — Uro Gap Report

## 1. Summary

Uro's branching substrate carried this game. Across **500 loops** — 502 loop branches, 9,502
commits, 16,526 events, every one of them a real `fork_branch` from a single `origin` marker —
fork latency stayed **flat in the number of loops** (`fork_branch` mean ~7-8 ms, +6% to +36%
drift within a run; materialization is O(origin world state), not O(branches), because
`_ancestry` only walks *up* from the fork point and never sees the siblings). The meteor test
survives being made into an entire game: the Vale is really destroyed on the doomed branches, and
on the winning loop the aversion is committed and the Vale is still standing. **But the single
biggest wall is not performance — it is *knowledge*.** A time-loop game is *defined* by the
asymmetry "the world forgets, the player remembers," and Uro can only express the first half:
there is no cross-branch or player-scoped memory at all (G-1), so the Loopwalker's Codex had to
be invented outside the engine — and the engine then obstructs even that, because the extractor
mints its own claim ids and derives `truth` from `provenance`, so a game cannot key its own facts
and cross-loop clue identity degrades to prose string-matching (G-2).

Two substrate findings that only a fork-per-loop workload surfaces, both now measured by shipped
code rather than asserted: **the ~50-commit snapshot cadence never fires at all** — snapshot depth
is distance from *genesis*, and every loop restarts at the origin's depth, so a 502-branch world
contains exactly **one** snapshot (G-4); and, following from that, **forking from the ancient
origin marker (5.5 ms) is *faster* than forking from a recent mid-loop commit (9.0 ms, depth 18)**,
because materialization replays every commit after the nearest ancestor snapshot — so a sideways
what-if fork gets steadily more expensive the later in the loop you take it. Neither costs *this*
game much (the origin sits at depth 1, so even with no snapshot a fork would replay only the
genesis commits — the cadence is a latent design mismatch, not a live wound). **What does cost
real time is G-10:** `EXPLAIN ANALYZE` on the engine's own fork query shows a **`Seq Scan` over
`memory_index`, discarding ~17,000 rows** to find a handful, because that table is indexed on
`(branch_id)` and the fork filters on `commit_id`. It is 30-60% of every fork, it is **global**
(one row per beat of *every* world in the database), and it is fixed by one line of SQL.

Expected problems that turned out **fine**, and so are not table rows: fork latency in the number
of loops (the hypothesis this game was built to break — it held); marker and branch primitives
(`create_marker`/`resolve_ref`/`list_branches` were index-backed and ergonomic enough that the
game keeps **no** branch registry of its own — the loop tree is rebuilt purely from
`list_branches` + `forked_from`); PC binding through a fork (copy-on-write carries `proj_pcs`, so
the Loopwalker is already an active PC on a bare fork, before any campaign work); and fork
isolation (a loop that ends in a crater leaves the origin and every sibling pristine).

**Disclosures.** (a) The Loopwalker's *position* (`Loop.place`) is game-side navigation state; Uro
could own it as a `located_in` edge and the game declined to commit one per step. Everything else
canonical — the segment, the key, the clues, the Fall, the aversion — is derived from Uro on every
read (`Loop.hydrate`), and the Codex is the one deliberate, documented exception (G-1). (b) The
scale harness plays a *uniform* route (no clue discovery, no win) so that per-loop work is
constant and any latency change is a property of the substrate, not of the game; the discovery →
death → return → win arc is the `--demo`/test path. (c) Evidence under `out/` is **regenerated**
by the commands in the README (`--scale N` writes `out/scale-N.csv` + `out/scale-N-summary.json`,
which contain every number quoted here); it is not committed.

## 2. Gap table

| Gap (what you wanted) | What happened (actual API/behavior/error/downgrade) | Workaround (or BLOCKED) | Severity for THIS game (blocker\|major\|annoyance\|cosmetic) | What Uro would need (a concrete engine change) | Evidence (the call/file that hit it) |
|---|---|---|---|---|---|
| **G-1** Player knowledge that survives a fork — *the entire premise of a time loop* | No cross-branch or player-scoped memory exists. Claims, beliefs and the memory index are all strictly branch-scoped (`store.search` filters `WHERE m.branch_id = $1`, store.py:1419-1431). A fork correctly resets the world; nothing can remember on the player's behalf | The game invents the **Loopwalker's Codex**, implemented *twice* (a JSON file, and a dedicated never-forked Uro branch of host-authored `k:` claims) so the boundary could be compared with evidence rather than asserted | **blocker** (worked around, but the game cannot exist without this layer) | A participant-scoped knowledge lane the engine owns and forks do not reset — or an explicit doc statement that cross-fork memory is deliberately the consumer's job | `codex.py open_codex`; store.py:1419-1431; `tests/…::test_the_knowledge_boundary_is_the_whole_game` |
| **G-2** Ask a branch whether it holds clue **K1** (a stable, game-chosen claim key) | The extractor **mints** the id (`claim_id = f"c:{new_id()}"`, extraction.py:185) and `ProposedClaim` has **no id field** (extraction.py:60-68). The minted ulid differs every run. `truth` is likewise *derived* from `provenance`, never chosen (extraction.py:177) | Clue identity is the exact **statement prose**: the game keeps `CLUE_BY_STATEMENT` and string-matches every claim on the branch. (A game *can* choose ids for claims it authors via `append_beat` — so the Codex has stable `k:K1` ids while the extracted loop claims do not) | **major** | An optional caller-supplied key on `ProposedClaim` (or an `extra` dict the gauntlet passes through to the `ClaimRecorded` payload) | `loop.py _harvest_clues`; extraction.py:60-68,177,185; `tests/…::test_the_extractor_will_not_let_a_game_key_its_clues` |
| **G-3** Re-point the existing campaign at the loop's forked branch (*the brief's own instruction*) | **There is no rebind.** `campaigns.branch_id` is written once at INSERT and never updated. Every campaign-keyed read resolves the branch from that **row**, not from the `Campaign` object passed in — `pc_for_participant`/`campaign_pcs` join `proj_pcs` to `campaigns` **ON c.branch_id** (store.py:641-670). So `campaign.model_copy(update={"branch_id": fork})` *does* run beats on the fork, but the engine resolves the acting PC against the campaign's **original** branch and is right only *by coincidence* (the fork is a copy, so the actor id matches). If `end_campaign` ever releases that PC, **every later loop runs PC-less with no error** | `start_campaign(fork_branch, adopt_actor_id="a:pc")` per loop — the sanctioned pattern (tests/test_meteor.py:112-116). Costs one commit + one `campaigns` row per loop and leaves a stale copied `proj_pcs` row on every fork | **major** | `store.rebind_campaign(campaign_id, branch_id)` — or resolve the acting PC from the branch the beat is actually running on | `loop.py begin_loop`; store.py:641-670; `tests/…::test_the_model_copy_rebind_is_correct_by_coincidence` |
| **G-4** The ~50-commit snapshot cadence amortises materialization across loops | **It never fires.** Snapshots are written when `depth % 50 == 0` (store.py:815-816) and `depth` is distance from **genesis** — but every loop is forked from the origin and so restarts at its depth, reaching only ~20. **Measured by the harness: 1 snapshot in a 502-branch, 9,502-commit world** (the one `create_marker` forced). Consequence, **measured by a shipped benchmark**: forking from the origin marker (**5.5 ms**) is *faster* than forking from a recent, deeper, un-snapshotted mid-loop commit (**9.0 ms**, depth 18) — a what-if fork costs more the later it is taken | Fork every loop from the marker (the design anyway) | **annoyance** — structurally interesting, but it costs *this* game ~0.5 ms: the origin is at depth 1, so even with no snapshot a fork would replay only the genesis commits. It would bite a game whose fork point is **deep** (a long prologue; loops forked from the end of the previous loop) | Snapshot on a per-**branch** commit count (or on measured materialization cost), not on absolute depth from genesis; and expose `snapshot(commit_id)` so a consumer can pin a hot fork point deliberately rather than relying on `create_marker`'s side-effect | `scale.py _fork_cost_benchmark` + `_totals` (shipped; both numbers in `out/scale-N-summary.json`); `tests/…::test_the_snapshot_cadence_never_fires_in_a_fork_per_loop_game` |
| **G-5** Compare loops — "which loop found which clue, when did each Vale fall, what endings happened" — in one query | **No cross-branch or aggregate query surface exists.** `list_branches(world_id)` returns branch rows only; every projection read is `WHERE branch_id = $1`. The player's `loops` view is a client-side fan-out of N × 4 round-trips, and `current_world_time` is itself a recursive CTE to genesis *per branch* (store.py:748-765). **Measured: 65 ms @ 42 branches → 306 ms @ 202 → 937 ms @ 502** — linear, and the slowest thing in the game | `loop.py loop_tree` fans out by hand and times itself | **major** | `query_across(branch_ids, projection) -> rows` and `diff_branches(a, b) -> {added, removed, changed}` (exact shape in target 4) | `loop.py loop_tree`; the `read_loop(4 queries)` timing row × N in every scale table |
| **G-6** Author the doom ladder in the fiction's own words (`looming → gathering → imminent → warded`) | **Rejected** by the grammar: `ThreadState` is the closed literal `["dormant","offered","active","resolved","dead"]` (events.py:797, pinned at worldpack/rules.py:45,114). Worse: an invalid pack does **not** raise — `Engine.react`/`agenda_tick` swallow the `ValidationError` into a `logger.warning` (engine.py:388-389, 420-421) and the **entire rule pack silently goes dark**. A one-word typo disables every reaction in the world with no error anywhere | The ladder is punned onto the five words the grammar speaks (`world.py DOOM_STATES`) and translated back for the UI; the pack is validated eagerly with `RulePack(**RULE_PACK)` at startup so a typo fails loud | **major** | Pack-declared thread-state vocabularies (validate against the pack, not a global `Literal`) — and a **loud** failure when a rule pack does not validate | `world.py DOOM_STATES`; `game.py _validate_pack`; events.py:797; engine.py:388-389; `tests/…::test_the_reaction_layer_actually_fires` (pins the ladder against exactly this silent-death mode) |
| **G-7** The destruction of the Vale reaches the narrator's prose | `PlaceDestroyed` flips `proj_places.status='destroyed'` and **nothing else**. Place state is not assembled into the narrator prompt at all — `RecallBundle` has no places field (recall.py:26-39) — so on the next beat the GM has no idea the village it is describing is a crater | Commit a `claim_recorded` alongside the destruction (the trick `tests/test_meteor.py` uses) so the fact enters the narrator's ESTABLISHED FACTS | **major** | Place state (status/description) in the recall bundle and the narrator prompt | `loop.py commit_the_fall`; recall.py:26-39; projector.py:124-129 |
| **G-8** A beat that picks something up commits the ownership change | A free-roam beat **cannot** commit an `ItemTransferred`: the extractor's whole vocabulary is actors+claims (extraction.py:71-73), and the Reaction Layer's action union structurally cannot move an item either. The narration says the key is in your coat while `items_owned_by` still says Wren has it | Host-authored `item_transferred` via `append_beat` (+ a manual `engine.react`), then **read ownership back** (`Loop.hydrate`) so the win gate trusts Uro and not a local flag | **major** | A sanctioned effect channel for a free-roam beat to move an item (the ruleset's opaque effect path already does this for encounter loot) | `loop.py take_the_key`; extraction.py:71-73 |
| **G-9** A clock finer than a day (the loop is **one day** in seven segments) | `world_day` is day-granular: `current_world_time -> int`, `time_skip(branch, days)`. `WorldTime` **has** a `segment` field (events.py:23-27) that `time_advanced` never sets and nothing reads. There is no game↔world time mapping either — a beat costs no time at all | The seven segments of the doomed day **are** seven `world_day`s. It works only because every loop forks from a day-0 origin, so absolute `world_day` == segment — the only reason the pack's **absolute** `world_day` conditions can express "as the day wears on" | **major** | A sub-day clock (populate and read `WorldTime.segment`), or a campaign-declared clock policy mapping beats to fiction-time | `loop.py advance`; `world.py SEGMENTS`; events.py:23-27 |
| **G-10** Fork hundreds of times without the fork path degrading — *the hot path of a branching game* | Fork is flat **in the number of loops**, but the **dominant component of its cost is a sequential scan that grows with the whole deployment**. `_copy_memory` (store.py:1231-1257) filters `memory_index` on `commit_id`; that table is indexed on `(branch_id)` **only** (migration 004:23). **`EXPLAIN ANALYZE` of the engine's own query: `Seq Scan`, ~17,000 rows discarded to find a handful, 2.5-4.6 ms — roughly 30-60% of an entire fork.** And `memory_index` is **global**: one row per beat of *every* world in the database. Fork latency is therefore O(total beats ever played in the deployment) — invisible on a small database, unbounded on a real one | None available to a consumer; the fork path is entirely inside the engine. The game measured it and proved the mechanism | **major** | `CREATE INDEX memory_index_commit_idx ON memory_index(commit_id);` — one line, and the scan disappears. (Also: set-based inserts in `restore_snapshot`/`_copy_memory`, row-at-a-time today, projector.py:377-386) | `scale.py _explain_fork_memory_scan` (shipped EXPLAIN ANALYZE; numbers in `out/scale-N-summary.json`); store.py:1231-1257 vs migration 004:23 |
| **G-11** Prune abandoned loops (a time-loop game forks forever; most loops are dead ends) | **No branch deletion, GC, or prune API exists anywhere in the store.** After the N=500 run the world carries 502+ permanent branches; a long session accumulates without bound. `fork_branch` also enforces `UNIQUE(world_id, name)`, so loop names can never be reused | None. The game keeps every loop forever (at least honest — the tree *is* the UI), but nothing could ever clean up the what-ifs | **annoyance** | `delete_branch(branch_id)` + a retention policy (and a documented answer for commits only that branch references) | `scale.py _log_scale_gaps`; confirmed absent by grep over the whole store |
| **G-12** Host-authored events run the Reaction Layer the way played beats do | `store.append_beat` commits but fires **no** pack rules; only `Engine._finish` (run_beat) and the server's Chronicler path call `engine.react`. An embedder who forgets the manual call gets threads that silently never advance | `loop.py _author` wraps every `append_beat` with `engine.react(campaign, commit_id, events)` | **annoyance** | A store-level post-commit hook, or a documented `engine.append_and_react` | `loop.py _author`; engine.py:339 |
| **G-13** A scripted provider keyed by the player's **intent** (the brief's own design) | Not implementable: **the extractor never sees the intent.** `build_extractor_messages` sends only KNOWN ACTORS / KNOWN CLAIMS / NARRATION (extraction.py:92-112) — deliberate player-text isolation, an anti-prompt-injection fence (extraction.py:10-12). Only the narrator's `stream()` sees the intent | The game **arms** the provider with the chosen (narration, extraction) pair before each beat. Deterministic and replayable, but the provider is now stateful and no two beats may run concurrently | **annoyance** (the isolation is *correct* — this is a documentation gap, not a design flaw) | A note that scripted providers must be armed/queued; or a beat-scoped correlation id on `CompletionRequest` so a provider can key its stages together | `script.py ScriptedProvider.arm`; extraction.py:92-112 |
| **G-14** Ask the engine how big a world is (events, commits, snapshots, depth) | No API. `list_branches`/`list_markers` exist, but nothing counts events, commits, or snapshots — the harness had to reach into `store._pool` and run raw SQL to answer the most basic question a branching consumer has ("how big is this world, and is anything being snapshotted?") | `scale.py _totals` / `_branch_events` execute raw SQL through the private pool | **annoyance** | `world_stats(world_id) -> {branches, commits, events, snapshots, max_depth}` — a seam a graph/vector-store swap-in would need anyway | `scale.py _totals` |
| **G-15** Exercise **marker** management at scale (the target asks about hundreds of markers) | **Not exercised — and the game says so rather than claiming a pass.** A scale world holds exactly **1** marker: the origin is created once and resolved *by name* on every one of the 500 forks (index-backed — `markers` has `UNIQUE(world_id, name)`), which is the ergonomics this game actually needed. "Hundreds of markers" was never driven, because a time-loop game has no reason to mint one per loop | n/a — reported as untested rather than asserted | **cosmetic** | Nothing here. The finding is that the *branch* half of that target scaled (502) and the *marker* half is simply not what this genre stresses | `scale.py run_scale`: `summary["markers"] == 1` |

**The refusal log** (rules the declarative grammar could not express; printed live by every run
with `--print-log`): **RL-1** escalate the dread after the player's *third* fruitless visit —
*missing: counters/accumulating state*; conditions are compare-only, and across loops a counter
would reset with the fork anyway. **RL-2** the Fall itself as a reaction to the hour — *missing:
any world-changing action*; the action union structurally cannot destroy a place, which is
correct for untrusted authors and means the single most important event in this game can never be
declarative.

## 3. Top 3 things Uro MUST add for this game to be good

1. **Player-scoped knowledge that survives a fork — G-1 (with G-2).** Not a nice-to-have; it is
   the genre. Every time-loop, roguelike, or New-Game-Plus consumer needs the exact asymmetry Uro
   half-implements: the world resets, the player does not. Today the engine gives the reset for
   free and leaves the *remembering* entirely to the game — then obstructs it, because an
   extracted fact has no key a game can hold onto. The fix composes: a participant-scoped claim
   lane that `fork_branch` does not reset, plus an optional caller key on `ProposedClaim`.
2. **One index on the fork's hot path — G-10.** `CREATE INDEX memory_index_commit_idx ON
   memory_index(commit_id);`. Every fork currently sequentially scans a table that holds a row per
   beat of *every world in the database* — 30-60% of fork time today, and unbounded as a
   deployment ages. It is the cheapest high-value fix in this report, and a branching engine
   cannot afford a fork cost that grows with unrelated worlds.
3. **A cross-branch query surface — G-5.** Branching is Uro's signature, yet the moment a game has
   more than a handful of branches, *the thing that makes branching legible to a player* becomes
   the slowest operation in the system (937 ms to draw 502 loops; N × 4 round-trips, because every
   read is `WHERE branch_id = $1`). `query_across(branch_ids, projection)` and `diff_branches(a,b)`
   would turn the fork tree from a fan-out into a query.

## 4. Verdicts on targeted leftover-work

- **Branching/materialization at scale — HIT; the deferral was RIGHT, with one real defect found.**
  Measured (regenerate with `--scale N`; every number lands in `out/scale-N-summary.json`):

  | N loops | branches | commits | events | fork mean | fork p95 | drift (first→last decile) | `loops` view | wall |
  |---|---|---|---|---|---|---|---|---|
  | 40 | 42 | — | — | 8.1 ms | — | 7.6 → 8.2 ms (+8%) | 65 ms | — |
  | 200 | 202 | 3,802 | 6,626 | 6.9 ms | 9.0 ms | 6.0 → 6.4 ms (+6%) | 306 ms | 27 s |
  | **500** | **502** | **9,502** | **16,526** | **8.0 ms** (p50 7.8) | **10.9 ms** | 6.3 → 8.6 ms (+36%) | **937 ms** | 78 s |

  **Fork latency is flat in the number of loops** — O(origin world state), not O(branches).
  Largest N reached: **500**; nothing strained, and I stopped because the evidence was conclusive.
  Postgres-as-graph-store is **adequate at this scale** — this is *not* evidence for a specialized
  store. But looking *inside* the fork found the one real defect: an `EXPLAIN ANALYZE` of
  `_copy_memory`'s query shows a **`Seq Scan` over `memory_index` discarding ~17,000 rows**,
  costing 2.5-4.6 ms — **30-60% of every fork** — and that table is *global*, so the cost grows
  with every beat ever played in the deployment (G-10). Snapshot cadence: **never fires** (exactly
  1 snapshot in a 502-branch world, max depth 20 — G-4), and consequently fork-from-recent (9.0 ms,
  depth 18) is *slower* than fork-from-ancient-marker (5.5 ms).
- **Fork-from-past + marker management — HIT (branches); NOT EXERCISED (markers), and reported as
  such.** `create_marker`/`resolve_ref`/`list_markers`/`list_branches` were ergonomic and
  index-backed, and every loop forks from a **name**, never a hash. The loop tree is reconstructed
  **purely from Uro** (`list_branches` + `forked_from` + `head_depth`), so the game keeps **no**
  branch registry of its own — which was the question, and the answer is a clean pass. Fork-from-a-
  long-past ref is not merely as good as fork-from-recent, it is **better** (G-4), because the past
  ref is the one with the snapshot. **Honest limit:** the "hundreds of markers" half of this target
  was never driven — a scale world holds exactly **one** marker (G-15), because a time-loop game
  has no reason to mint one per loop. Friction found: branch names are `UNIQUE(world_id, name)` and
  there is no deletion (G-11), so a long campaign accumulates branches forever.
- **Knowledge carry across forks (engine vs game boundary) — HIT; the game's biggest wall (G-1).**
  Precisely, a fork **inherits**: actors, claims, beliefs, places, **PC bindings**, sheets, items,
  factions, edges, threads (the 10 `_SNAPSHOT_TABLES`), plus memory-index rows, the derived
  `world_day`, and the beat transcript through the fork point. It does **not** inherit anything
  from *sibling* branches (proven every run — loop N+1 has never heard of loop N's clues) or
  **campaigns** (the row stays pointed at its original branch — G-3). Is a game-side Codex the
  right boundary? **Half-right.** It is honest — player meta-knowledge genuinely is out-of-world —
  but Uro should still *own the concept*, because every consumer of this genre will otherwise
  reinvent it, and the engine actively obstructs the reinvention by denying stable claim keys
  (G-2). I implemented **both** options the brief offered and shipped both: the **file** Codex is
  the honest boundary (zero Uro calls, trivially inspectable, obviously out-of-world; it must be
  scoped per-world or a new game starts already knowing everything); the **never-forked branch**
  Codex is the durable one (real Uro state, survives export/import with the world, queryable
  through the same projections — and, being host-authored, it holds the stable `k:K1` ids the
  extractor refuses the loop branches). Verdict: **ship the file, but the engine should offer the
  lane.**
- **Cross-fork/aggregate queries — HIT; BLOCKING (G-5).** No aggregate surface exists. The exact
  API this game needed:
  ```python
  # 1. the loops view: one round-trip instead of N x 4
  await store.query_across(
      branch_ids=[...],                      # or: world_id + a branch-name glob
      select=["world_time", "places.status", "threads.state", "claims.statement"],
  ) -> dict[branch_id, dict[str, Any]]
  # 2. the what-if view: what actually differs between two lines?
  await store.diff_branches(a, b) -> {"added": [...], "removed": [...], "changed": [...]}
  ```
  Measured pain: **937 ms** to render 502 loops (linear: 65 ms @ 42, 306 ms @ 202) — the single
  slowest operation in the game, and it is the *core UI* of a game whose whole selling point is its
  branch tree.
- **PC binding across forks — HIT; it works, and it is the one place the engine anticipated the
  genre.** `is_pc` and `active_pcs` survive `fork_branch` from the origin **with no rebinding at
  all** — copy-on-write carries `proj_pcs` including `active`, so the Loopwalker is already a PC on
  a bare fork before any campaign work (`test_pc_binding_survives_every_fork`). No surprises with
  `active_pcs` on forked branches. What is **awkward** is the *campaign*: it cannot be rebound
  (G-3), the `model_copy` trick is correct only by coincidence (proven in a test), and the
  sanctioned fix (`start_campaign` per fork) costs a commit and a row per loop and leaves a stale
  `proj_pcs` row on every branch. Verdict: PC-through-fork was right; campaign-through-fork is now
  blocking a real consumer.
- **Time within a loop (`time_skip`/`world_day`) — HIT; the lack of sub-day time is real but
  survivable (G-9).** The seven segments *are* seven `world_day`s. It works only because every loop
  forks from a day-0 origin, so absolute `world_day` == segment; a loop forked from a *later* commit
  would inherit its day and every `world_day` condition in the pack would be off by that offset (the
  what-if fork inherits the clock correctly — `test_a_whatif_fork_carries_the_clock_and_the_reactions`).
  The Reaction-Layer day-cadence did **not** fight the clock: `every_days: 1` fires exactly once per
  `agenda_tick(branch, 1)`, so one agenda pass per segment — exactly what a dread ladder wants, and
  the ladder is pinned by a test. Two sharp edges: `store.time_skip` alone runs **no** rules (only
  `engine.agenda_tick` does), and `Engine.react` never fires on a `TimeAdvanced` commit — so a
  `Rule` triggering on `TimeAdvanced` is **accepted and permanently inert**. The missing game↔world
  time mapping is real: a beat costs no time, so the game moves the clock by hand. **Also brushed
  against and confirmed:** place-state never reaches the narrator prompt (G-7 — item 8 on the "does
  NOT have" list); the Reaction Layer has no counters (RL-1 — item 6); and entity resolution is
  name+alias only — the extractor's `about` must carry an actor's *name*, never its id, or the claim
  silently binds to a dangling `name:` token (item 10).
