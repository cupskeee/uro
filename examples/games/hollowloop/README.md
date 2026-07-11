# HOLLOWLOOP

*Outer Wilds / Majora's Mask, but the loop is a literal branch of the world's history.*

You are the Loopwalker. You wake at dawn in the **Vale of Mourn**, a village that dies every
night: at last light a falling star strikes and everything ends. You wake again at dawn, in the
same pristine morning, remembering everything. **The villagers remember nothing.** Across loops
you piece together what the Fall is, what can ward it, where the key is hidden, and the exact
hour the sky breaks — and on one perfect loop you ring the Sky-Bell at the instant of the Fall
and break the cycle.

Every loop is a **real `fork_branch` from one fixed origin marker**. The world resets by
construction; the Codex does not. The Fall is a committed `PlaceDestroyed` on that loop's branch.
This is [the meteor test](../../../packages/uro-core/tests/test_meteor.py) as an entire game.

**The biggest wall:** not performance — 500 loops, flat fork latency — but *knowledge*. Uro has
no concept of anything that survives a fork, which is the one thing a time-loop game is made of.
See [`GAP_REPORT.md`](GAP_REPORT.md).

## Run it

```sh
docker compose up -d --wait                                  # Postgres on host port 5433
uv run python examples/games/hollowloop/game.py              # play it
uv run python examples/games/hollowloop/game.py --demo       # the scripted story, no input
uv run python examples/games/hollowloop/game.py --scale 60   # the branching-at-scale harness
uv run python examples/games/hollowloop/game.py --scale 500 --print-log   # the full evidence run
uv run pytest examples/games/hollowloop/tests                # the contract (12 tests)
```

Deterministic with the scripted provider and **no API key**. `--codex file|branch` picks the
Codex backend (both are implemented — see below). `--provider openai|anthropic` binds a real model
to the **narrator role only**, through the router's public per-role `bindings` seam; the clue
*extraction* stays scripted deliberately (a real extractor would paraphrase the keystones, and
clue identity is prose-keyed because the engine mints claim ids — G-2 — so the game would stop
recognising its own clues). Opt-in, never required. The tests are not collected by `just test`
(the root `testpaths` is `packages/`); run them explicitly as above.

Evidence under `out/` is **regenerated**, not committed: `--scale N` writes `out/scale-N.csv`
(per loop) and `out/scale-N-summary.json` (every number the GAP report quotes — fork percentiles,
snapshot count, the fork-cost benchmark, and the `EXPLAIN ANALYZE` of the fork's memory scan).

## One loop

Seven segments — dawn, morning, noon, afternoon, dusk, last light, **the Fall** — mapped onto
`world_day` 0-6, advanced with `engine.agenda_tick(branch, 1)` per beat (which is also what fires
the Reaction Layer's rising dread). Each action is one Uro beat. NPCs move on a fixed schedule, so
being in the right place at the right segment is what lets you learn things:

| clue | what you learn | where |
|---|---|---|
| **K1** the nature of the Fall | it is a stone, and it lands at last light | Elder Aldis, chapel, seg 0-2 |
| **K2** the ward | the Sky-Bell can hold the sky if rung at the instant it strikes | Chaplain Sela, chapel (needs K1) |
| **K3** the hidden key | Wren hid the tower key in the old well | Wren, the well, seg 3-4 |
| **K4** the hour | the star falls at nightfall, and the bell must ring *then* | witness a full loop — or Harrow, tower (needs K1) |

Assemble all four in the **Codex**, retrieve the key, be at the tower at nightfall, and `ring`.

Commands: a numbered menu (`go` / `talk` / `wait` / `search` / `ring`), plus `look`, `whatif`
(fork sideways from this branch's head and **play it**), `back` (return to the line you left),
`loops` (the fork tree), `codex`, `quit`.

## The knowledge boundary (the research target)

A clue discovered in a loop is a durable Uro **claim on that loop's branch**. The next loop is a
fresh fork from the origin, so `list_claims(loop-0002)` has **never heard of it** — while the CLI
still offers the intent it unlocked. That asymmetry is the whole game, and Uro can only express
half of it, so the **Loopwalker's Codex** is the game's one piece of persistent state. The brief
asked for a JSON file *or* a never-forked Uro branch, with a justification; **both are
implemented** (`codex.py`) so the comparison rests on evidence: the file is the *honest* boundary
(out-of-world knowledge, stored out of world), the branch is the *durable* one (real Uro state,
and — being host-authored — it can hold the stable `k:K1` ids the extractor denies the loop
branches). Verdict in the GAP report.

## What it exercises, and what it found

Posture A (embedded `uro_core`): `create_world` + inline `rule_pack`, `start_campaign`,
`create_marker`/`resolve_ref`/`list_branches`, `fork_branch` (×500), `run_beat` with a scripted
provider, the extractor + gauntlet, `agenda_tick` (the Reaction Layer's dread ladder),
`append_beat` (`PlaceDestroyed`, `ItemTransferred`, `ThreadStateChanged`, authored claims),
`assemble_recall`, and the projections behind every read.

Headline findings (all evidenced in the GAP report, all reproducible from the commands above):

- **Fork latency is flat in the number of loops**: 500 loops, 502 branches, 16,526 events —
  `fork_branch` mean **8 ms**, O(origin world state) not O(branches). The branching substrate
  passed the test it was built for.
- **...but every fork sequentially scans a global table.** `EXPLAIN ANALYZE` of the engine's own
  fork query shows a **`Seq Scan` over `memory_index` discarding ~17,000 rows** — 30-60% of a
  fork — because that table is indexed on `(branch_id)` while the fork filters on `commit_id`.
  It holds a row per beat of *every world in the database*. **One index fixes it.**
- **The ~50-commit snapshot cadence never fires** in a fork-per-loop world (exactly **one**
  snapshot in a 502-branch world) — so forking from the *ancient* origin (5.5 ms) is **faster**
  than forking from a *recent* mid-loop commit (9.0 ms, depth 18).
- **There is no cross-branch query API**: drawing the loop tree — the core UI of a branching game
  — costs N × 4 round-trips, **937 ms at 502 loops**.
- **The extractor won't let a game key its own facts** (`claim_id` is minted, `truth` is derived),
  so cross-loop clue identity is prose string-matching.
- **A campaign cannot be rebound onto a fork.** The `model_copy` trick appears to work and is
  correct only *by coincidence* — proven in `tests/test_hollowloop.py`.
