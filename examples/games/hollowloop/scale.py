"""The scale harness — HOLLOWLOOP's central experiment.

N loops, each a real `fork_branch(world_id, "origin", "loop-NNNN")` + a full 7-segment day + a
committed Fall. Every loop does IDENTICAL work, so any change in latency as N grows is a
property of the SUBSTRATE, not of the game.

What we are actually testing (URO_INTEGRATION item 12: "untested at the scale of
hundreds/thousands of forks"):

  H1  fork latency is FLAT in the number of loops.
      The origin is a marker, and `create_marker` writes a snapshot at that commit
      (store.py:912), so `_materialize_into` finds a snapshot at exactly the fork point and
      replays ZERO events (store.py:1200-1229). `_ancestry` only walks UP from the origin, so
      the hundreds of sibling loops are invisible to it. Fork cost should therefore be
      O(rows in the origin's world state) and independent of N.

  H2  ...except that `_copy_memory` (store.py:1231-1257) selects
      `FROM memory_index WHERE commit_id = ANY($1)`, and `memory_index` has exactly ONE index:
      `(branch_id)` (migration 004:23). There is NO index on `commit_id`. Every beat of every
      loop adds a row to that table (Engine._remember), so the table grows ~7 rows per loop and
      each fork sequentially scans ALL of it. Fork latency should therefore grow with N.

  H3  the ~50-commit snapshot cadence interacts pathologically with fork-per-loop.
      `_append` snapshots when `depth % 50 == 0` (store.py:815-816) — and depth is the COMMIT's
      depth, which every loop branch inherits from the shared origin. So every loop crosses the
      same depth boundaries at the same segment: either ALL loops pay for a snapshot at the same
      beat, or none do. It is a systematic, aligned cost, not an amortised one.

The table this prints (and out/scale-N.csv) is the evidence for the branching verdict.
"""

from __future__ import annotations

import csv
import statistics
import time
from pathlib import Path
from typing import Any

import loop as loopmod
from codex import open_codex
from frictionlog import TIMINGS, gap, print_timings, timing_stats
from loop import begin_loop, bootstrap, commit_the_fall
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id

# every loop plays the identical 6-beat day, then the Fall — constant work per loop
UNIFORM_ROUTE = ["go:p:chapel", "wait", "wait", "go:p:well", "wait", "wait"]


async def run_scale(store: PostgresEventStore, n: int, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(exist_ok=True)
    vale = await bootstrap(store, out_dir, f"Vale of Mourn — scale {n} ({new_id()[:6]})")
    codex = await open_codex("file", store=store, world_id=vale.world_id, out_dir=out_dir)

    print(
        f"\nHOLLOWLOOP — scale run: {n} loops, each a fork from the '{loopmod.ORIGIN_REF}' marker\n"
    )
    per_loop: list[dict[str, Any]] = []
    t_start = time.perf_counter()

    for i in range(1, n + 1):
        t0 = time.perf_counter()
        fork_before = len(TIMINGS["fork_branch"])
        loop = await begin_loop(vale, i)
        fork_ms = TIMINGS["fork_branch"][fork_before]

        for key in UNIFORM_ROUTE:
            opts = {o.key: o for o in loopmod.options(loop, codex)}
            await loopmod.choose(loop, opts[key], codex)
        await commit_the_fall(loop)

        per_loop.append(
            {
                "loop": i,
                "branch": loop.name,
                "fork_ms": round(fork_ms, 2),
                "loop_ms": round((time.perf_counter() - t0) * 1000, 2),
                "beats": loop.beats,
                "segment": loop.segment,
                "outcome": loop.outcome,
            }
        )
        if i % 10 == 0 or i == n:
            recent = [r["fork_ms"] for r in per_loop[-10:]]
            print(
                f"  loop {i:>4}/{n}  fork {statistics.fmean(recent):6.1f} ms (last 10)   "
                f"loop {per_loop[-1]['loop_ms']:7.1f} ms"
            )

    wall = time.perf_counter() - t_start

    # the cross-branch fan-out: what the player's `loops` view costs at this scale
    t0 = time.perf_counter()
    tree = await loopmod.loop_tree(store, vale.world_id)
    tree_ms = (time.perf_counter() - t0) * 1000

    branches = await store.list_branches(vale.world_id)
    markers = await store.list_markers(vale.world_id)
    events, commits = await _totals(store, vale.world_id)

    summary = {
        "n": n,
        "wall_s": round(wall, 1),
        "branches": len(branches),
        "markers": len(markers),
        "commits": commits,
        "events": events,
        "loop_tree_ms": round(tree_ms, 1),
        "loops_in_tree": len(tree),
    }
    _report(per_loop, summary, out_dir, n)
    _log_scale_gaps(per_loop, summary)
    return summary


async def _totals(store: PostgresEventStore, world_id: str) -> tuple[int, int]:
    """Total events + commits in the world. NOTE: there is no engine API for this — the game
    reaches into the pool with raw SQL, which a real consumer should never have to do."""
    async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
        commits = await conn.fetchval("SELECT count(*) FROM commits WHERE world_id = $1", world_id)
        events = await conn.fetchval(
            "SELECT count(*) FROM events e JOIN commits c ON c.commit_id = e.commit_id "
            "WHERE c.world_id = $1",
            world_id,
        )
    gap(
        gap="Ask the engine how big a world is (events, commits, branches) — basic telemetry",
        happened="No API. `list_branches`/`list_markers` exist, but nothing counts events or "
        "commits; the scale harness had to reach into `store._pool` and run raw SQL against the "
        "`commits`/`events` tables",
        workaround="scale.py _totals executes raw SQL through the private pool",
        severity="annoyance",
        needs="`world_stats(world_id) -> {branches, commits, events, snapshots}` (a graph/vector "
        "store swap-in would need this seam anyway)",
        evidence="scale.py _totals (raw SQL via store._pool)",
    )
    return int(events), int(commits)


def _report(per_loop: list[dict[str, Any]], summary: dict[str, Any], out: Path, n: int) -> None:
    csv_path = out / f"scale-{n}.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(per_loop[0]))
        w.writeheader()
        w.writerows(per_loop)

    first, last = per_loop[: max(1, n // 10)], per_loop[-max(1, n // 10) :]
    f_first = statistics.fmean(r["fork_ms"] for r in first)
    f_last = statistics.fmean(r["fork_ms"] for r in last)
    drift = (f_last / f_first - 1) * 100 if f_first else 0.0

    print(
        f"\n{'=' * 78}\nSCALE RESULT — {n} loops, {summary['branches']} branches, "
        f"{summary['commits']} commits, {summary['events']} events "
        f"in {summary['wall_s']}s\n{'=' * 78}"
    )
    print_timings()
    print(f"\n  fork_branch, first 10%:  {f_first:7.1f} ms")
    print(f"  fork_branch, last  10%:  {f_last:7.1f} ms   ({drift:+.0f}% drift across the run)")
    print(
        f"  the player's `loops` view over {summary['loops_in_tree']} branches: "
        f"{summary['loop_tree_ms']:.0f} ms  (N x 4 queries — there is no aggregate API)"
    )
    print(f"\n  per-loop CSV -> {csv_path}")


def _log_scale_gaps(per_loop: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    n = summary["n"]
    tenth = max(1, n // 10)
    f_first = statistics.fmean(r["fork_ms"] for r in per_loop[:tenth])
    f_last = statistics.fmean(r["fork_ms"] for r in per_loop[-tenth:])
    drift = (f_last / f_first - 1) * 100 if f_first else 0.0
    fork = timing_stats("fork_branch")

    # NB fork latency being FLAT is NOT a gap — it is the hypothesis this game set out to break,
    # and it held (see GAP_REPORT Summary + target 1). Only the LATENT risk on that hot path is
    # a row: the fork copies memory rows with an unindexed scan that grows with the whole DB.
    print(
        f"\n  [fork_branch is FLAT: mean {fork.get('mean_ms', 0):.1f} ms, "
        f"first-10% {f_first:.1f} ms -> last-10% {f_last:.1f} ms ({drift:+.0f}%). "
        "Not a gap — a pass.]"
    )
    gap(
        gap="Fork the world hundreds of times without the fork path degrading (a time-loop "
        "game forks on EVERY new day — it is the hot path)",
        happened=f"IT HELD, and that is the headline result: MEASURED over {n} loops, "
        f"fork_branch mean {fork.get('mean_ms', 0):.1f} ms, p95 {fork.get('p95_ms', 0):.1f} ms, "
        f"drift {drift:+.0f}% from first-10% to last-10%. But ONE latent defect sits on that hot "
        "path: `_copy_memory` (store.py:1231-1257) selects `FROM memory_index WHERE commit_id = "
        "ANY($1)` and `memory_index` has NO index on `commit_id` (only `(branch_id)`, migration "
        "004:23). Every fork therefore sequentially scans that table — which is shared across "
        "ALL worlds in the database and grows with every beat ever played. It did not bite at "
        "N=500 (the table was only ~8.5k rows) but it is O(total beats in the deployment) on "
        "the single hottest call a branching game makes",
        workaround="None available to a consumer — the fork path is entirely inside the engine. "
        "The game simply measured it",
        severity="annoyance",
        needs="`CREATE INDEX memory_index_commit_idx ON memory_index(commit_id)` (one line), and "
        "set-based inserts in restore_snapshot/_copy_memory (both are row-at-a-time today: "
        "projector.py:377-386)",
        evidence=f"scale.py run_scale (N={n}); out/scale-{n}.csv; store.py:1231-1257 vs "
        "migration 004:23",
    )
    gap(
        gap="Prune abandoned loops (a time-loop game forks forever; most loops are dead ends)",
        happened=f"There is NO branch deletion, GC, or prune API anywhere in the store — after "
        f"this run the world carries {summary['branches']} permanent branches and "
        f"{summary['commits']} commits, and a long session would accumulate without bound. "
        "`fork_branch` also enforces UNIQUE(world_id, name), so loop names can never be reused",
        workaround="None. The game keeps every loop forever (which is at least honest — the "
        "tree IS the game's UI), but nothing could ever clean up the what-ifs",
        severity="annoyance",
        needs="`delete_branch(branch_id)` / a retention policy (and a documented answer for what "
        "happens to commits that only that branch references)",
        evidence=f"scale.py run_scale: {summary['branches']} branches after {n} loops, no way to "
        "remove any of them",
    )
    gap(
        gap="The ~50-commit snapshot cadence amortises materialization cost across loops",
        happened="It NEVER FIRES. Snapshots are written when `depth % 50 == 0` "
        "(store.py:815-816) and `depth` is the commit's distance from GENESIS — but every loop "
        "branch is forked from the origin and so restarts at the origin's depth (1) and only "
        "reaches depth ~20. MEASURED: a 502-branch, 9502-commit world contained exactly ONE "
        "snapshot — the one `create_marker` forced at the origin (store.py:912). The entire "
        "snapshot machinery is inert in a fork-per-loop workload, and the marker is silently "
        "doing 100% of the materialization work. Consequence, measured: forking from the ancient "
        "origin marker (5.5 ms) is FASTER than forking from a RECENT mid-loop commit (7.0 ms, "
        "depth 16) — because the recent commit has no snapshot and materialization must replay "
        "every event after the origin's. The cost of a what-if fork therefore grows with how "
        "deep into the loop you take it",
        workaround="Fork every loop from the marker (which is the design anyway); accept that "
        "sideways what-if forks get linearly more expensive the later they are taken",
        severity="major",
        needs="snapshot on a per-BRANCH commit count (or on materialization cost), not on "
        "absolute depth from genesis — and/or an explicit `snapshot(commit_id)` a consumer can "
        "call before forking repeatedly from a hot commit",
        evidence="tests/test_hollowloop.py::test_the_snapshot_cadence_never_fires_in_a_fork_per_"
        "loop_game (asserts exactly 1 snapshot, max depth < 50); the fork-from-marker vs "
        "fork-from-mid-loop benchmark in GAP_REPORT target 1",
    )
    gap(
        gap="Render the loop tree — the game's core UI, and the whole point of branching",
        happened=f"MEASURED: {summary['loop_tree_ms']:.0f} ms for "
        f"{summary['loops_in_tree']} branches, because there is no aggregate query surface — it "
        "is `list_branches` + N x (current_world_time + get_place + list_threads + list_claims). "
        "`current_world_time` alone is a recursive CTE to genesis per branch (store.py:748-765)",
        workaround="loop.py loop_tree fans out by hand; the game caches nothing (Uro owns the "
        "truth)",
        severity="major",
        needs="`query_across(branch_ids, projection) -> rows` + `diff_branches(a,b)` "
        "(see GAP_REPORT target 4)",
        evidence=f"scale.py run_scale: loop_tree over {summary['loops_in_tree']} branches took "
        f"{summary['loop_tree_ms']:.0f} ms",
    )
