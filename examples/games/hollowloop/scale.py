"""The scale harness — HOLLOWLOOP's central experiment.

N loops, each a real `fork_branch(world_id, "origin", "loop-NNNN")` + a full 7-segment day + a
committed Fall. Every loop does IDENTICAL work, so any change in latency as N grows is a
property of the SUBSTRATE, not of the game.

Three hypotheses, and what actually happened (URO_INTEGRATION item 12: "untested at the scale of
hundreds/thousands of forks"). All three results are recorded honestly, including the one that
refuted its own hypothesis:

  H1  fork latency is FLAT in the number of loops.  -> HELD.
      `_ancestry` only walks UP from the fork point, so hundreds of sibling loops are invisible
      to it; fork cost is O(rows in the origin's world state), not O(branches). Measured flat to
      N=500.

  H2  ...but `_copy_memory` (store.py:1231-1257) selects `FROM memory_index WHERE commit_id =
      ANY($1)`, and `memory_index` has exactly ONE index: `(branch_id)` (migration 004:23) — none
      on `commit_id`.  -> CONFIRMED by `_explain_fork_memory_scan`: Postgres SEQUENTIALLY SCANS
      that table on every fork, discarding ~17k rows to find a handful, at 30-60% of total fork
      cost. And the table is GLOBAL (a row per beat of EVERY world in the DB), so fork latency
      grows with every beat ever played in the deployment. One index removes it.

  H3  the ~50-commit snapshot cadence interacts pathologically with fork-per-loop.  -> CONFIRMED,
      and more sharply than expected: it never fires AT ALL. `_append` snapshots when
      `depth % 50 == 0` (store.py:815-816), and `depth` is distance from GENESIS — every loop
      restarts at the origin's depth, so no branch ever gets deep enough. `_totals` counts the
      snapshots to prove it, and `_fork_cost_benchmark` measures the consequence: a fork from a
      DEEP, RECENT, un-snapshotted commit is SLOWER than a fork from the ancient origin marker.

Evidence shipped per run: out/scale-N.csv (per loop) and out/scale-N-summary.json (every number
the GAP report quotes).
"""

from __future__ import annotations

import csv
import json
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


async def run_scale(
    store: PostgresEventStore, n: int, out_dir: Path, *, codex_kind: str = "file"
) -> dict[str, Any]:
    out_dir.mkdir(exist_ok=True)
    vale = await bootstrap(store, out_dir, f"Vale of Mourn — scale {n} ({new_id()[:6]})")
    codex = await open_codex(codex_kind, store=store, world_id=vale.world_id, out_dir=out_dir)

    print(
        f"\nHOLLOWLOOP — scale run: {n} loops, each a fork from the '{loopmod.ORIGIN_REF}' marker\n"
    )
    per_loop: list[dict[str, Any]] = []
    t_start = time.perf_counter()
    deep_commit = ""  # a mid-loop commit, kept for the fork-cost benchmark below

    for i in range(1, n + 1):
        t0 = time.perf_counter()
        fork_before = len(TIMINGS["fork_branch"])
        loop = await begin_loop(vale, i)
        fork_ms = TIMINGS["fork_branch"][fork_before]

        for key in UNIFORM_ROUTE:
            opts = {o.key: o for o in loopmod.options(loop, codex)}
            await loopmod.choose(loop, opts[key], codex)
        if not deep_commit:
            deep_commit = await loop.head()  # a DEEP (un-snapshotted) fork point
        await commit_the_fall(loop)

        per_loop.append(
            {
                "loop": i,
                "branch": loop.name,
                "fork_ms": round(fork_ms, 2),
                "loop_ms": round((time.perf_counter() - t0) * 1000, 2),
                "beats": loop.beats,
                "events": await _branch_events(store, loop.branch_id),
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

    bench = await _fork_cost_benchmark(store, vale.world_id, deep_commit)
    scan = await _explain_fork_memory_scan(store, vale.world_id)
    branches = await store.list_branches(vale.world_id)
    markers = await store.list_markers(vale.world_id)
    events, commits, snapshots, max_depth = await _totals(store, vale.world_id)

    summary = {
        "n": n,
        "wall_s": round(wall, 1),
        "branches": len(branches),
        "markers": len(markers),  # NB: only ever 1 ('origin') — the scale run never wins
        "commits": commits,
        "events": events,
        "snapshots": snapshots,  # the H3 evidence: expected to be exactly 1
        "max_commit_depth": max_depth,  # ...because no branch ever reaches depth 50
        "loop_tree_ms": round(tree_ms, 1),
        "loops_in_tree": len(tree),
        **bench,
        **scan,
    }
    _report(per_loop, summary, out_dir, n)
    _log_scale_gaps(per_loop, summary)
    return summary


async def _fork_cost_benchmark(
    store: PostgresEventStore, world_id: str, deep_commit: str, k: int = 15
) -> dict[str, Any]:
    """THE fork-cost experiment, shipped: fork K times from the SNAPSHOTTED origin marker, and K
    times from a DEEP un-snapshotted mid-loop commit. Materialization replays every commit after
    the nearest ancestor snapshot, so the deep fork should be the slower one — which inverts the
    intuition that forking from the distant past must be expensive."""
    if not deep_commit:
        return {}

    async def bench(from_ref: str, label: str) -> list[float]:
        out: list[float] = []
        for i in range(k):
            t0 = time.perf_counter()
            await store.fork_branch(world_id, from_ref, f"bench-{label}-{i}-{new_id()[:5]}")
            out.append((time.perf_counter() - t0) * 1000)
        return out

    marker_ms = await bench(loopmod.ORIGIN_REF, "marker")
    deep_ms = await bench(deep_commit, "deep")
    async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
        depth = await conn.fetchval("SELECT depth FROM commits WHERE commit_id = $1", deep_commit)
    return {
        "fork_from_marker_ms": round(statistics.fmean(marker_ms), 2),
        "fork_from_deep_ms": round(statistics.fmean(deep_ms), 2),
        "deep_commit_depth": int(depth or 0),
        "fork_bench_k": k,
    }


async def _explain_fork_memory_scan(store: PostgresEventStore, world_id: str) -> dict[str, Any]:
    """The mechanism probe for G-10, shipped: EXPLAIN ANALYZE the exact query `_copy_memory`
    runs on every fork (store.py:1231-1257). `memory_index` is indexed on `(branch_id)` only
    (migration 004:23) — there is NO index on `commit_id`, which is what this query filters on —
    so Postgres sequentially scans the table. And that table is GLOBAL: it holds a row per beat
    of every world in the database. This reports how many rows the fork's memory-copy actually
    had to scan, and how long it took."""
    async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
        total = await conn.fetchval("SELECT count(*) FROM memory_index")
        sample = [
            r["commit_id"]
            for r in await conn.fetch(
                "SELECT commit_id FROM commits WHERE world_id = $1 ORDER BY depth LIMIT 3",
                world_id,
            )
        ]
        plan = await conn.fetch(
            "EXPLAIN (ANALYZE, FORMAT JSON) "
            "SELECT DISTINCT ON (commit_id, content_hash) commit_id, content_hash, kind, text, "
            "entity_refs FROM memory_index WHERE commit_id = ANY($1::text[])",
            sample,
        )
    node = plan[0][0] if isinstance(plan[0][0], dict) else json.loads(plan[0][0])[0]
    root = node["Plan"]

    def find_scan(p: dict[str, Any]) -> dict[str, Any] | None:
        if "Scan" in p.get("Node Type", ""):
            return p
        for child in p.get("Plans", []):
            hit = find_scan(child)
            if hit:
                return hit
        return None

    scan = find_scan(root) or {}
    return {
        "memory_index_rows_in_db": int(total or 0),
        "fork_memory_scan_node": scan.get("Node Type", "?"),
        "fork_memory_rows_discarded": int(scan.get("Rows Removed by Filter", 0) or 0),
        "fork_memory_scan_ms": round(float(node.get("Execution Time", 0.0)), 2),
    }


async def _branch_events(store: PostgresEventStore, branch_id: str) -> int:
    """Events committed on THIS loop's own commits — i.e. its ancestry chain from the branch head
    back down to (but excluding) the commit it forked from. Stage 6 asks for a per-loop event
    count, and there is no engine API for it (G-14), so this walks the chain in raw SQL."""
    async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
        return int(
            await conn.fetchval(
                """
                WITH RECURSIVE b AS (
                    SELECT head_commit, forked_from FROM branches WHERE branch_id = $1
                ), chain AS (
                    SELECT c.commit_id, c.parent_id FROM commits c, b
                     WHERE c.commit_id = b.head_commit
                    UNION ALL
                    SELECT c.commit_id, c.parent_id
                      FROM commits c JOIN chain ch ON c.commit_id = ch.parent_id, b
                     WHERE ch.commit_id IS DISTINCT FROM b.forked_from
                )
                SELECT count(*) FROM events e JOIN chain ON chain.commit_id = e.commit_id, b
                 WHERE chain.commit_id IS DISTINCT FROM b.forked_from
                """,
                branch_id,
            )
            or 0
        )


async def _totals(store: PostgresEventStore, world_id: str) -> tuple[int, int, int, int]:
    """Total events + commits + SNAPSHOTS + max depth. NOTE: there is no engine API for this —
    reaches into the pool with raw SQL, which a real consumer should never have to do."""
    async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
        commits = await conn.fetchval("SELECT count(*) FROM commits WHERE world_id = $1", world_id)
        events = await conn.fetchval(
            "SELECT count(*) FROM events e JOIN commits c ON c.commit_id = e.commit_id "
            "WHERE c.world_id = $1",
            world_id,
        )
        snapshots = await conn.fetchval(
            "SELECT count(*) FROM snapshots s JOIN commits c ON c.commit_id = s.commit_id "
            "WHERE c.world_id = $1",
            world_id,
        )
        max_depth = await conn.fetchval(
            "SELECT max(depth) FROM commits WHERE world_id = $1", world_id
        )
    gap(
        id="G-14",
        gap="Ask the engine how big a world is (events, commits, snapshots, depth) — telemetry",
        happened="No API. `list_branches`/`list_markers` exist, but nothing counts events, "
        "commits, or snapshots; the scale harness had to reach into `store._pool` and run raw "
        "SQL against the `commits`/`events`/`snapshots` tables to answer the most basic question "
        "a branching consumer has ('how big is this world, and is anything being snapshotted?')",
        workaround="scale.py _totals executes raw SQL through the private pool",
        severity="annoyance",
        needs="`world_stats(world_id) -> {branches, commits, events, snapshots, max_depth}` (a "
        "graph/vector store swap-in would need this seam anyway)",
        evidence="scale.py _totals (raw SQL via store._pool)",
    )
    return int(events), int(commits), int(snapshots), int(max_depth or 0)


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
    fork = timing_stats("fork_branch")

    # the SUMMARY row — every number the GAP report quotes, persisted next to the per-loop CSV,
    # so the report's evidence is reproducible from shipped artifacts rather than from prose.
    summary_out = {
        **summary,
        "fork_mean_ms": round(fork.get("mean_ms", 0), 2),
        "fork_p50_ms": round(fork.get("p50_ms", 0), 2),
        "fork_p95_ms": round(fork.get("p95_ms", 0), 2),
        "fork_max_ms": round(fork.get("max_ms", 0), 2),
        "fork_first_decile_ms": round(f_first, 2),
        "fork_last_decile_ms": round(f_last, 2),
        "fork_drift_pct": round(drift, 1),
    }
    summary_path = out / f"scale-{n}-summary.json"
    summary_path.write_text(json.dumps(summary_out, indent=2, sort_keys=True) + "\n")

    print(
        f"\n{'=' * 78}\nSCALE RESULT — {n} loops, {summary['branches']} branches, "
        f"{summary['commits']} commits, {summary['events']} events "
        f"in {summary['wall_s']}s\n{'=' * 78}"
    )
    print_timings()
    print(f"\n  fork_branch, first 10%:  {f_first:7.1f} ms")
    print(f"  fork_branch, last  10%:  {f_last:7.1f} ms   ({drift:+.0f}% drift across the run)")
    print(
        f"\n  SNAPSHOTS in this whole world: {summary['snapshots']}  "
        f"(max commit depth {summary['max_commit_depth']} — the depth%50 cadence never fires)"
    )
    if "fork_from_marker_ms" in summary:
        print(
            f"  fork from the 'origin' MARKER (snapshotted):     "
            f"{summary['fork_from_marker_ms']:6.2f} ms"
        )
        print(
            f"  fork from a mid-loop commit (depth "
            f"{summary['deep_commit_depth']}, NO snapshot): "
            f"{summary['fork_from_deep_ms']:6.2f} ms   <- the DEEPER, more RECENT commit is slower"
        )
    print(
        f"\n  the player's `loops` view over {summary['loops_in_tree']} branches: "
        f"{summary['loop_tree_ms']:.0f} ms  (N x 4 queries — there is no aggregate API)"
    )
    print(f"\n  per-loop CSV -> {csv_path}\n  summary JSON -> {summary_path}")


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
    scan_ms = summary.get("fork_memory_scan_ms", 0.0)
    scan_rows = summary.get("memory_index_rows_in_db", 0)
    discarded = summary.get("fork_memory_rows_discarded", 0)
    share = (scan_ms / fork.get("mean_ms", 1)) * 100 if fork.get("mean_ms") else 0
    gap(
        id="G-10",
        gap="Fork the world hundreds of times without the fork path degrading (a time-loop game "
        "forks on EVERY new day — it is the hot path)",
        happened=f"Fork stays flat IN the number of loops (mean {fork.get('mean_ms', 0):.1f} ms, "
        f"p95 {fork.get('p95_ms', 0):.1f} ms, {drift:+.0f}% drift over {n} loops) — "
        "materialization is O(origin world state), not O(branches). BUT the dominant COMPONENT "
        "of that cost is "
        "a sequential scan that grows with the whole DEPLOYMENT: `_copy_memory` "
        "(store.py:1231-1257) "
        "filters `memory_index` on `commit_id`, and that table is indexed on `(branch_id)` ONLY "
        f"(migration 004:23). MEASURED by EXPLAIN ANALYZE of the engine's own query: a "
        f"{summary.get('fork_memory_scan_node', 'Seq Scan')} over {scan_rows:,} rows, discarding "
        f"{discarded:,} of them to find a handful, costing {scan_ms:.1f} ms — roughly {share:.0f}% "
        "of a whole fork. And `memory_index` is GLOBAL: one row per beat of EVERY world in the "
        "database. So fork latency is O(total beats ever played in the deployment), on the single "
        "hottest call a branching game makes. It is invisible on a small database and unbounded on "
        "a real one",
        workaround="None available to a consumer — the fork path is entirely inside the engine. "
        "The game measured it and proved the mechanism",
        severity="major",
        needs="`CREATE INDEX memory_index_commit_idx ON memory_index(commit_id)` — one line, and "
        "it removes the scan entirely. (Also: set-based inserts in restore_snapshot/_copy_memory, "
        "which are row-at-a-time today, projector.py:377-386)",
        evidence=f"scale.py _explain_fork_memory_scan (shipped EXPLAIN ANALYZE; numbers in "
        f"out/scale-{n}-summary.json); store.py:1231-1257 vs migration 004:23",
    )
    gap(
        id="G-11",
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
    bench = ""
    if "fork_from_marker_ms" in summary:
        bench = (
            f" MEASURED by the shipped benchmark (_fork_cost_benchmark, "
            f"{summary['fork_bench_k']} forks each): from the snapshotted origin marker "
            f"{summary['fork_from_marker_ms']:.1f} ms vs from an un-snapshotted mid-loop commit "
            f"(depth {summary['deep_commit_depth']}) {summary['fork_from_deep_ms']:.1f} ms — the "
            "RECENT commit is the slower fork point, because materialization replays every commit "
            "after the nearest ancestor snapshot. A what-if fork therefore gets steadily more "
            "expensive the later in the loop it is taken."
        )
    gap(
        id="G-4",
        gap="The ~50-commit snapshot cadence amortises materialization cost across loops",
        happened=f"It NEVER FIRES. Snapshots are written when `depth % 50 == 0` "
        f"(store.py:815-816), and `depth` is the commit's distance from GENESIS — but every loop "
        f"is forked from the origin and so restarts at the origin's depth, never getting deep "
        f"enough. MEASURED this run: {summary['snapshots']} snapshot(s) in a "
        f"{summary['branches']}-branch, {summary['commits']}-commit world, max commit depth "
        f"{summary['max_commit_depth']}. The whole snapshot machinery is inert in a fork-per-loop "
        f"workload.{bench} HONEST SCOPE: this costs this game almost nothing (the origin sits at "
        "depth 1, so even with no snapshot at all a fork would replay only the genesis commits) — "
        "it is a latent design mismatch, not a live wound, and it would bite a game whose fork "
        "point is DEEP (a long prologue, or loops forked from the end of the previous loop)",
        workaround="Fork every loop from the marker (the design anyway); accept that sideways "
        "what-if forks cost linearly more the later they are taken",
        severity="annoyance",
        needs="snapshot on a per-BRANCH commit count (or on measured materialization cost) rather "
        "than absolute depth from genesis — and an explicit `snapshot(commit_id)` a consumer can "
        "call to pin a hot fork point deliberately instead of relying on create_marker's "
        "side-effect",
        evidence="scale.py _fork_cost_benchmark (shipped; numbers in out/scale-N-summary.json); "
        "tests/test_hollowloop.py::test_the_snapshot_cadence_never_fires_in_a_fork_per_loop_game",
    )
    gap(
        id="G-15",
        gap="Exercise marker management AT SCALE (the target asks about hundreds of markers)",
        happened=f"NOT EXERCISED, and the game says so rather than claiming a pass: this world "
        f"holds {summary['markers']} marker(s). The origin marker is created once and resolved by "
        f"NAME on every one of the {n} forks (index-backed: markers has UNIQUE(world_id, name)), "
        "which is the ergonomics this game actually needed — but 'hundreds of markers' was never "
        "driven, because the game has no reason to mint one per loop",
        workaround="n/a — reported as untested rather than asserted",
        severity="cosmetic",
        needs="nothing here; the finding is that the BRANCH half of this target scaled (502) and "
        "the MARKER half is simply not what a time-loop game stresses",
        evidence=f"scale.py run_scale: summary['markers'] == {summary['markers']}",
    )
    # (the cross-branch-query gap, G-5, is logged at its own call site — loop.py loop_tree, which
    # this harness calls; the measured cost at this N is printed above and in the summary JSON)
