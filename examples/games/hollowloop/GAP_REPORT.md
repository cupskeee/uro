# HOLLOWLOOP — Uro Gap Report

## 1. Summary

Uro's branching substrate carried this game, and the headline result is a **pass, not a wall**:
across **500 loops** — 502 real branches, 9,502 commits, 16,526 events, all forked from one
`origin` marker — `fork_branch` stayed **flat at ~6 ms** (mean 6.9 ms, p50 6.0 ms, +12% drift
from the first 10% of loops to the last). The meteor test survives being made into an entire
game: every loop is a genuine branch, the Vale is really destroyed on the doomed ones, and on
the winning loop the aversion is committed and the Vale is still standing. **The single biggest
wall was not performance but *knowledge*: Uro has no concept of anything that survives a fork.**
A time-loop game is *defined* by the asymmetry "the world forgets, the player remembers", and
the engine can only express the first half — so the Loopwalker's Codex had to be invented
outside the engine (G-3), and the clues it tracks cannot even be *named*, because the extractor
mints its own claim ids and refuses the game a stable key (G-5). Two structural findings came
out of the scale run that no smaller consumer would see: **the ~50-commit snapshot cadence never
fires at all** in a fork-per-loop workload (depth is measured from genesis, and every loop
restarts at the origin's depth — a 502-branch world contained exactly **one** snapshot, the one
`create_marker` forced), so markers are silently doing 100% of the materialization work; and as
a direct consequence, **forking from the ancient origin is *faster* (5.5 ms) than forking from a
recent mid-loop commit (7.0 ms)** — the opposite of the intuition, and it means a what-if fork
gets linearly more expensive the later in the loop you take it (G-13). Expected problems that
turned out **fine** and so are not table rows: fork latency at scale (the whole point of the
exercise — it held); marker/branch primitives (`create_marker`/`resolve_ref`/`list_branches`
were index-backed and ergonomic enough that the game keeps **no** branch registry of its own —
the loop tree is reconstructed purely from `list_branches` + `forked_from`); PC binding through
a fork (copy-on-write carries `proj_pcs`, so the Loopwalker is already a PC on a bare fork,
before any campaign work); and fork isolation (a loop that ends in a crater leaves the origin
and every sibling pristine — asserted every run).

## 2. Gap table

| Gap (what you wanted) | What happened (actual API/behavior/error/downgrade) | Workaround (or BLOCKED) | Severity for THIS game (blocker\|major\|annoyance\|cosmetic) | What Uro would need (a concrete engine change) | Evidence (the call/file that hit it) |
|---|---|---|---|---|---|
| **G-1** Player knowledge that survives a fork — *the entire premise of a time loop* | No cross-branch or player-scoped memory exists. Claims, beliefs and the memory index are all strictly branch-scoped (`store.search` filters `WHERE m.branch_id = $1`, store.py:1419-1431). A fork correctly resets the world; nothing can remember on the player's behalf | The game invents the **Loopwalker's Codex**, implemented *twice* (a JSON file, and a dedicated never-forked Uro branch of host-authored `k:` claims) so the boundary could be compared with evidence rather than asserted | **blocker** (worked around, but the game cannot exist without this layer) | A participant-scoped knowledge lane the engine owns and forks do not reset — or an explicit doc statement that cross-fork memory is deliberately the consumer's job | `codex.py open_codex`; store.py:1419-1431; `tests/test_hollowloop.py::test_the_knowledge_boundary_is_the_whole_game` |
| **G-2** Ask a branch whether it holds clue **K1** (a stable, game-chosen claim key) | The extractor **mints** the id (`claim_id = f"c:{new_id()}"`, extraction.py:185) and `ProposedClaim` has **no id field** (extraction.py:60-68). The minted ulid differs every run, so it isn't even stable across replays of the same script. `truth` is likewise *derived* from `provenance`, never chosen (extraction.py:177) | Clue identity is the exact **statement prose**: the game keeps `CLUE_BY_STATEMENT` and string-matches every claim on the branch. (Ironically a game *can* choose ids for claims it authors via `append_beat` — so the Codex has stable `k:K1` ids while the extracted loop claims do not) | **major** | An optional caller-supplied key on `ProposedClaim` (or an `extra` dict the gauntlet passes through to the `ClaimRecorded` payload) | `loop.py _harvest_clues`; extraction.py:60-68,177,185; `tests/…::test_the_extractor_will_not_let_a_game_key_its_clues` |
| **G-3** Re-point the existing campaign at the loop's forked branch (*the brief's own instruction*) | **There is no rebind.** `campaigns.branch_id` is written once at INSERT and never updated (no `UPDATE campaigns` exists anywhere). Every campaign-keyed read resolves the branch from that **row**, not from the `Campaign` object passed in — `pc_for_participant`/`campaign_pcs` join `proj_pcs` to `campaigns` **ON c.branch_id** (store.py:641-670). So `campaign.model_copy(update={"branch_id": fork})` *does* run beats on the fork, but the engine resolves the acting PC against the campaign's **original** branch and is right only *by coincidence* (the fork is a copy, so the actor id matches). It breaks silently the moment the PC differs — or if `end_campaign` ever releases it, after which **every later loop runs PC-less with no error** | `start_campaign(fork_branch, adopt_actor_id="a:pc")` per loop — the sanctioned pattern (tests/test_meteor.py:112-116). Costs one commit + one `campaigns` row per loop and leaves a stale copied `proj_pcs` row on every fork | **major** | `store.rebind_campaign(campaign_id, branch_id)` — or resolve the acting PC from the branch the beat is actually running on (the `Campaign` object's `branch_id`) rather than the campaigns row | `loop.py begin_loop`; store.py:641-670; `tests/…::test_the_model_copy_rebind_is_correct_by_coincidence` (proves the coincidence) |
| **G-4** The ~50-commit snapshot cadence amortises materialization across loops | **It never fires.** Snapshots are written when `depth % 50 == 0` (store.py:815-816) and `depth` is distance from **genesis** — but every loop is forked from the origin and so restarts at the origin's depth (1), never reaching ~20. **Measured: a 502-branch, 9,502-commit world contained exactly ONE snapshot** — the one `create_marker` forced (store.py:912). The whole snapshot machinery is inert here, and the marker silently does 100% of the work. Direct consequence, **measured**: forking from the ancient origin marker (**5.5 ms**) is *faster* than forking from a **recent** mid-loop commit (**7.0 ms**, depth 16), because the recent commit has no snapshot and materialization must replay every event after the origin's | Fork every loop from the marker (the design anyway); accept that a sideways `whatif` fork gets linearly more expensive the later in the loop it is taken | **major** | Snapshot on a per-**branch** commit count (or on measured materialization cost), not on absolute depth from genesis; and/or an explicit `snapshot(commit_id)` a consumer can call before forking repeatedly from a hot commit | `tests/…::test_the_snapshot_cadence_never_fires_in_a_fork_per_loop_game` (asserts exactly 1 snapshot, max depth < 50); the fork-cost benchmark in target 1 |
| **G-5** Compare loops — "which loop found which clue, when did each Vale fall, what endings happened" — in one query | **No cross-branch or aggregate query surface exists.** `list_branches(world_id)` returns branch rows only; every projection read is `WHERE branch_id = $1`. The player's `loops` view is therefore a client-side fan-out of N × 4 round-trips, and `current_world_time` is itself a recursive CTE to genesis *per branch* (store.py:748-765). **Measured: 102 ms @ 62 branches → 320 ms @ 202 → 811 ms @ 502** — linear, and the slowest thing in the game | `loop.py loop_tree` fans out by hand and times itself | **major** | `query_across(branch_ids, projection) -> rows` and `diff_branches(a, b) -> {added, removed, changed}` (exact shape in target 4) | `loop.py loop_tree`; the `read_loop(4 queries)` timing row × N in every scale table |
| **G-6** Author the doom ladder in the fiction's own words (`looming → gathering → imminent → warded`) | **Rejected** by the grammar: `ThreadState` is the closed literal `["dormant","offered","active","resolved","dead"]` (events.py:797, pinned at worldpack/rules.py:45,114). Worse: an invalid pack does **not** raise — `Engine.react`/`agenda_tick` swallow the `ValidationError` into a `logger.warning` (engine.py:388-389, 420-421) and the **entire rule pack silently goes dark**. A one-word typo disables every reaction in the world with no error anywhere | The ladder is punned onto the five words the grammar speaks (`world.py DOOM_STATES`) and translated back for the UI; the pack is validated eagerly with `RulePack(**RULE_PACK)` at startup so a typo fails loud | **major** | Pack-declared thread-state vocabularies (validate against the pack, not a global `Literal`) — and a **loud** failure when a rule pack does not validate | `world.py DOOM_STATES`; `game.py _validate_pack`; events.py:797; engine.py:388-389 |
| **G-7** The destruction of the Vale reaches the narrator's prose | `PlaceDestroyed` flips `proj_places.status='destroyed'` and **nothing else**. Place state is not assembled into the narrator prompt at all — `RecallBundle` has no places field (recall.py:26-39) — so on the next beat the GM has no idea the village it is describing is a crater | Commit a `claim_recorded` alongside the destruction (the same trick `tests/test_meteor.py` uses) so the fact enters the narrator's ESTABLISHED FACTS | **major** | Place state (status/description) in the recall bundle and the narrator prompt | `loop.py commit_the_fall`; recall.py:26-39; projector.py:124-129 |
| **G-8** A beat that picks something up commits the ownership change | A free-roam beat **cannot** commit an `ItemTransferred`: the extractor's whole vocabulary is actors+claims (extraction.py:71-73), and the Reaction Layer's action union structurally cannot move an item either. The narration says the key is in your coat while `items_owned_by` still says Wren has it | Host-authored `item_transferred` via `append_beat` immediately after the beat (+ a manual `engine.react`) | **major** | A sanctioned effect channel for a free-roam beat to move an item (the ruleset's opaque effect path already does this for encounter loot) | `loop.py take_the_key`; extraction.py:71-73 |
| **G-9** A clock finer than a day (the loop is **one day** in seven segments) | `world_day` is day-granular: `current_world_time -> int`, `time_skip(branch, days)`. `WorldTime` **has** a `segment` field (events.py:23-27) that `time_advanced` never sets and nothing reads. There is no game↔world time mapping either — a beat costs no time at all | The seven segments of the doomed day **are** seven `world_day`s. It works only because every loop forks from a day-0 origin, so absolute `world_day` == segment — the only reason the pack's **absolute** `world_day` conditions can express "as the day wears on" | **major** | A sub-day clock (populate and read `WorldTime.segment`), or a campaign-declared clock policy mapping beats to fiction-time | `loop.py advance`; `world.py SEGMENTS`; events.py:23-27 |
| **G-10** Fork the world hundreds of times without the fork path degrading | **It held** (see Summary) — but one latent defect sits on that hot path: `_copy_memory` (store.py:1231-1257) selects `FROM memory_index WHERE commit_id = ANY($1)`, and `memory_index` has **no index on `commit_id`** (only `(branch_id)`, migration 004:23). Every fork sequentially scans that table — which is shared across **all worlds in the database** and grows with every beat ever played. It did not bite at N=500 (~8.5k rows), but it is O(total beats in the deployment) on the single hottest call a branching game makes | None available to a consumer — the fork path is entirely inside the engine. The game measured it | **annoyance** (latent; would become a blocker in a long-lived deployment) | `CREATE INDEX memory_index_commit_idx ON memory_index(commit_id)` (one line); set-based inserts in `restore_snapshot`/`_copy_memory` (both row-at-a-time today, projector.py:377-386) | `scale.py run_scale`; `out/scale-500.csv`; store.py:1231-1257 vs migration 004:23 |
| **G-11** Prune abandoned loops (a time-loop game forks forever; most loops are dead ends) | **No branch deletion, GC, or prune API exists anywhere in the store.** After the N=500 run the world carries 502 permanent branches; a long session accumulates without bound. `fork_branch` also enforces `UNIQUE(world_id, name)`, so loop names can never be reused | None. The game keeps every loop forever (at least honest — the tree *is* the UI), but nothing could ever clean up the what-ifs | **annoyance** | `delete_branch(branch_id)` + a retention policy (and a documented answer for commits only that branch references) | `scale.py _log_scale_gaps`; confirmed absent by grep over the whole store |
| **G-12** Host-authored events run the Reaction Layer the way played beats do | `store.append_beat` commits but fires **no** pack rules; only `Engine._finish` (run_beat) and the server's Chronicler path call `engine.react`. An embedder who forgets the manual call gets threads that silently never advance | `loop.py _author` wraps every `append_beat` with `engine.react(campaign, commit_id, events)` | **annoyance** | A store-level post-commit hook, or a documented `engine.append_and_react` | `loop.py _author`; engine.py:339 |
| **G-13** A scripted provider keyed by the player's **intent** (the brief's own design) | Not implementable: **the extractor never sees the intent.** `build_extractor_messages` sends only KNOWN ACTORS / KNOWN CLAIMS / NARRATION (extraction.py:92-112) — deliberate player-text isolation, i.e. an anti-prompt-injection fence (extraction.py:10-12). Only the narrator's `stream()` sees the intent | The game **arms** the provider with the chosen (narration, extraction) pair before each beat. Deterministic and replayable, but the provider is now stateful and no two beats may run concurrently | **annoyance** (the isolation is *correct* — this is a documentation gap, not a design flaw) | A note that scripted providers must be armed/queued; or a beat-scoped correlation id on `CompletionRequest` so a provider can key its stages together | `script.py ScriptedProvider.arm`; extraction.py:92-112 |
| **G-14** Ask the engine how big a world is (events, commits, branches) — basic telemetry | No API. `list_branches`/`list_markers` exist, but nothing counts events or commits; the scale harness had to reach into `store._pool` and run raw SQL | `scale.py _totals` executes raw SQL through the private pool | **annoyance** | `world_stats(world_id) -> {branches, commits, events, snapshots}` — a seam a graph/vector-store swap-in would need anyway | `scale.py _totals` |
| **G-15** Record *why* the doom fell or was warded | `thread_state_changed` has no `cause`/`reason` field (events.py:913-921) — the single most important state change in the game commits with no reason attached | The "why" lives in a companion `claim_recorded` | **cosmetic** | A `cause: str = ""` on `ThreadStateChangedPayload` (every other lifecycle event has one) | `loop.py commit_the_fall` / `ring_the_bell` |

**The refusal log** (rules the declarative grammar could not express; printed live by every run
with `--print-log`): **RL-1** escalate the dread after the player's *third* fruitless visit —
*missing: counters/accumulating state*; conditions are compare-only, and across loops a counter
would reset with the fork anyway. **RL-2** the Fall itself as a reaction to the hour —
*missing: any world-changing action*; the action union structurally cannot destroy a place,
which is correct for untrusted authors and means the single most important event in this game
can never be declarative.

## 3. Top 3 things Uro MUST add for this game to be good

1. **Player-scoped knowledge that survives a fork — tied to G-1 (and G-2).** This is not a
   nice-to-have; it is the genre. Every time-loop, roguelike, or New-Game-Plus consumer needs
   the exact asymmetry Uro half-implements: the world resets, the player does not. Today the
   engine gives you the reset for free and leaves the *remembering* entirely to the game — and
   then makes even that hard, because an extracted fact has no key a game can hold onto (G-2).
   The fix is small and composable: a participant-scoped claim lane that `fork_branch` does not
   reset, plus an optional caller key on `ProposedClaim`.
2. **A cross-branch query surface — tied to G-5.** Branching is Uro's signature, and yet the
   moment a game has more than a handful of branches, *the thing that makes branching legible to
   a player* is the slowest operation in the system (811 ms to draw 502 loops, N × 4 round-trips,
   because every read is `WHERE branch_id = $1`). `query_across(branch_ids, projection)` and
   `diff_branches(a, b)` would turn the fork tree from a fan-out into a query.
3. **Fix the snapshot cadence for fork-heavy workloads — tied to G-4.** Snapshotting on depth
   from genesis means that in the exact workload Uro was built to be proud of — hundreds of forks
   off one root — the cadence *never fires at all*, and a single marker snapshot silently carries
   the whole system. It works today (5.5 ms forks) only because `create_marker` happens to force
   a snapshot. Make it a per-branch commit count, and expose `snapshot(commit_id)` so a consumer
   can pin a hot fork point deliberately instead of by luck.

## 4. Verdicts on targeted leftover-work

- **Branching/materialization at scale — HIT, and the deferral was RIGHT.** Measured, from
  `out/scale-{60,200,500}.csv`:

  | N loops | branches | commits | events | fork mean | fork p95 | drift (first 10% → last 10%) | wall |
  |---|---|---|---|---|---|---|---|
  | 60 | 62 | 1,142 | 2,006 | 5.9 ms | 7.6 ms | 5.5 → 6.5 ms (+18%) | 6.7 s |
  | 200 | 202 | 3,802 | 6,626 | 6.1 ms | 8.3 ms | 5.5 → 6.1 ms (+10%) | 23.1 s |
  | **500** | **502** | **9,502** | **16,526** | **6.9 ms** (p50 6.0) | **8.6 ms** | **5.4 → 6.1 ms (+12%)** | **55.6 s** |

  **Fork latency is flat in the number of loops** — it is O(size of the origin's world state),
  not O(branches), because `_ancestry` only walks *up* from the fork point (siblings are
  invisible) and the origin marker's snapshot means **zero events are replayed**. Largest N
  reached: **500** (the run is linear in N; nothing strained, and I stopped because the evidence
  was conclusive, not because it broke). Postgres-as-graph-store is **adequate at this scale** —
  this is *not* evidence for a specialized store. Two caveats found only by looking: the
  ~50-commit cadence **never fires** (G-4 — one snapshot in a 502-branch world) so markers carry
  everything; and fork-from-recent (7.0 ms, depth 16) is **slower** than fork-from-ancient
  (5.5 ms), so what-if forks get costlier the later they're taken. The one latent scale defect is
  the unindexed `memory_index(commit_id)` scan on the fork hot path (G-10).
- **Fork-from-past + marker management — HIT; the primitives held up, no game-side registry
  needed.** `create_marker`/`resolve_ref`/`list_markers`/`list_branches` were ergonomic and
  index-backed (`markers` has `UNIQUE(world_id, name)`, so resolving `"origin"` 500× is free),
  and every loop forks from a **name**, never a hash. The loop tree is reconstructed **purely
  from Uro** — `list_branches` + `forked_from` + `head_depth` — so the game keeps **no** branch
  registry of its own, which was the question. Fork-from-a-long-past ref is not merely as good as
  fork-from-recent, it is **better** (see above), because the past ref is the one with the
  snapshot. Friction: branch names are `UNIQUE(world_id, name)` and there is no deletion (G-11),
  so a long campaign accumulates branches forever and can never reuse a loop number.
- **Knowledge carry across forks (engine vs game boundary) — HIT; this is the game's biggest
  wall (G-1).** Precisely, a fork **inherits**: actors, claims, beliefs, places, **PC bindings**,
  sheets, items, factions, edges, threads (the 10 `_SNAPSHOT_TABLES`), plus memory-index rows,
  the derived `world_day`, and the beat transcript through the fork point. It does **not**
  inherit: anything from *sibling* branches (proven every run — loop N+1 has never heard of
  loop N's clues), and **campaigns** (the `campaigns` row stays pointed at its original branch —
  G-3). Is a game-side Codex the right boundary? **Half-right.** It is honest — player
  meta-knowledge genuinely is out-of-world — but Uro should still *own the concept*, because
  every consumer of this genre will otherwise reinvent it, and the engine actively obstructs the
  reinvention by denying stable claim keys (G-2). I implemented **both** options the brief
  offered and shipped both: the **file** Codex is the honest boundary (zero Uro calls, trivially
  inspectable, obviously out-of-world); the **never-forked branch** Codex is the durable one (it
  is real Uro state, survives export/import with the world, is queryable through the same
  projections — and, being host-authored via `append_beat`, it can hold the stable `k:K1` ids the
  extractor refuses the loop branches). Verdict: **ship the file, but the engine should offer the
  lane.**
- **Cross-fork/aggregate queries — HIT; BLOCKING (G-5).** There is no aggregate surface at all.
  The exact API this game needed:
  ```python
  # 1. the loops view: one round-trip instead of N x 4
  await store.query_across(
      branch_ids=[...],                      # or: world_id + a branch name glob
      select=["world_time", "places.status", "threads.state", "claims.statement"],
  ) -> dict[branch_id, dict[str, Any]]
  # 2. the what-if view: what actually differs between two lines?
  await store.diff_branches(a, b) -> {"added": [...], "removed": [...], "changed": [...]}
  ```
  Measured pain: **811 ms** to render 502 loops (linear: 102 ms @ 62, 320 ms @ 202), which is the
  single slowest operation in the game — and it is the *core UI* of a game whose whole selling
  point is its branch tree.
- **PC binding across forks — HIT; it works, and it's the one place the engine anticipated the
  genre.** `is_pc` and `active_pcs` survive `fork_branch` from the origin **with no rebinding at
  all** — copy-on-write carries `proj_pcs` (including `active`), so the Loopwalker is already a
  PC on a bare fork before any campaign work (asserted in
  `test_pc_binding_survives_every_fork`). No surprises with `active_pcs` on forked branches. What
  is **awkward** is the *campaign*: it cannot be rebound (G-3), the `model_copy` trick is correct
  only by coincidence, and the sanctioned fix (`start_campaign` per fork) costs a commit and a
  row per loop and leaves a stale `proj_pcs` row on every branch. Deferral verdict: PC-through-
  fork was right; campaign-through-fork is now blocking a real consumer.
- **Time within a loop (`time_skip`/`world_day`) — HIT; the lack of sub-day time is real but
  survivable (G-9).** The seven segments of the doomed day *are* seven `world_day`s — the game
  borrows whole days as intra-day segments. It works only because every loop forks from a day-0
  origin, so absolute `world_day` == segment; a loop forked from a *later* commit would inherit
  its day and every `world_day` condition in the pack would be off by that offset. The
  Reaction-Layer day-cadence did **not** fight the clock: `every_days: 1` fires exactly once per
  `agenda_tick(branch, 1)`, so one agenda pass per segment, which is exactly what a dread ladder
  wants. Two sharp edges: `store.time_skip` alone runs **no** rules (only `engine.agenda_tick`
  does), and `Engine.react` never fires on a `TimeAdvanced` commit — so a `Rule` triggering on
  `TimeAdvanced` is **accepted and permanently inert**. And the missing game↔world time mapping
  (URO_INTEGRATION item 2) is real: a beat costs no time at all, so the game must move the clock
  by hand. **Also brushed against and confirmed:** place-state never reaches the narrator prompt
  (G-7 — item 8 on the "does NOT have" list), the Reaction Layer has no counters (RL-1 — item 6),
  and entity resolution is name+alias only (the extractor's `about` must carry an actor's *name*,
  never its id, or the claim silently binds to a dangling `name:` token — item 10).
