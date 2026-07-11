"""HOLLOWLOOP — a time-loop roguelike where every loop is a real branch of the world's history.

    uv run python examples/games/hollowloop/game.py              # play it
    uv run python examples/games/hollowloop/game.py --demo       # the scripted story, no input
    uv run python examples/games/hollowloop/game.py --scale 60   # the branching-at-scale harness

Deterministic with the scripted provider and no API key. `--provider openai` narrates with a real
model (the clue EXTRACTION stays scripted, or the keystones would not be recognisable and the
game would not be a game) — opt-in, never required.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import frictionlog
import loop as loopmod
import script
from codex import Codex, open_codex
from frictionlog import gap
from loop import Loop, Vale, begin_loop, bootstrap, can_ring, choose, commit_the_fall, options
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.pipeline.recall import assemble_recall
from uro_core.worldpack.rules import RulePack
from world import (
    CLUES,
    DOOM_SEGMENT,
    DSN_DEFAULT,
    KEYSTONES,
    ORIGIN_REF,
    PLACE_NAMES,
    RULE_PACK,
    VALE,
    who_is_at,
)

OUT_DIR = Path(__file__).resolve().parent / "out"


def _validate_pack() -> None:
    """Fail LOUD on a bad rule pack. The engine will not: `Engine.react`/`agenda_tick` swallow a
    ValidationError into a logger.warning (engine.py:388-389, 420-421) and the whole pack goes
    silently dark — a one-word typo disables every reaction in the world with no error."""
    RulePack(**RULE_PACK)


async def connect(dsn: str | None = None) -> PostgresEventStore:
    store = PostgresEventStore(dsn or os.environ.get("URO_DATABASE_URL", DSN_DEFAULT))
    await store.connect()
    await store.migrate()
    return store


# --------------------------------------------------------------------------------------------
# Views: the commit graph IS the UI
# --------------------------------------------------------------------------------------------


def render_loops(rows: list[dict[str, Any]]) -> str:
    w = max((len(r["name"]) for r in rows), default=8) + 2
    out = ["", "THE LOOP TREE  (every line is a real Uro branch)", ""]
    out.append(f"  {'branch':<{w}} {'seg':>3}  {'the Vale':<10} {'doom':<10} clues")
    for r in rows:
        clues = ",".join(r["clues"]) or "—"
        out.append(f"  {r['name']:<{w}} {r['segment']:>3}  {r['vale']:<10} {r['doom']:<10} {clues}")
    return "\n".join(out)


def render_codex(codex: Codex) -> str:
    out = ["", f"THE LOOPWALKER'S CODEX  ({codex.kind})", ""]  # type: ignore[attr-defined]
    known = codex.known()
    by_key = {e.key: e for e in codex.entries()}
    for key in KEYSTONES:
        clue = CLUES[key]
        if key in known:
            e = by_key[key]
            out.append(f"  [x] {key}  {clue['title']:<24} learned in {e.loop} @ seg {e.segment}")
            out.append(f"          “{clue['statement']}”")
        else:
            out.append(f"  [ ] {key}  {clue['title']:<24} — unknown")
    if codex.complete():
        out.append("\n  You know everything. The bell is enough. Get the key; be at the tower.")
    return "\n".join(out)


async def render_scene(loop: Loop, codex: Codex) -> str:
    """What the Loopwalker sees. Read from Uro, never from game memory."""
    state = await loopmod.read_loop(loop.vale.store, loop.branch_id)
    here = PLACE_NAMES[loop.place]
    folk = [n for n in who_is_at(loop.place, loop.segment)]
    lines = [
        "",
        f"  {loop.name} · segment {loop.segment}/{DOOM_SEGMENT} — {loop.segment_name}",
        f"  {here}. The doom is {state['doom']}."
        + (" You carry the tower key." if loop.holds_key else ""),
    ]
    if folk:
        from world import NPC_NAMES

        lines.append("  Here: " + ", ".join(NPC_NAMES[a] for a in folk))
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# The scripted story (Stages 1-6 end to end) — what --demo prints and the golden test asserts
# --------------------------------------------------------------------------------------------

# loop 1: learn K1, K2, K3, then witness the Fall (which teaches K4)
DISCOVERY_ROUTE = ["go:p:chapel", "talk:a:aldis", "talk:a:sela", "go:p:well", "talk:a:wren", "wait"]
# loop 2: the perfect loop — key, tower, ring at nightfall
WINNING_ROUTE = ["go:p:well", "search", "go:p:tower", "wait", "wait", "wait"]


async def _run_route(loop: Loop, codex: Codex, route: list[str], *, verbose: bool) -> None:
    for key in route:
        opts = {o.key: o for o in options(loop, codex)}
        assert key in opts, f"{key} not available in {loop.name} @ seg {loop.segment}: {list(opts)}"
        if verbose:
            print(await render_scene(loop, codex))
            print(f"  > {opts[key].label}")
        narration = await choose(loop, opts[key], codex)
        if verbose:
            print(f"\n  {narration}\n")


async def story(vale: Vale, codex: Codex, *, verbose: bool = False) -> dict[str, Any]:
    """The whole game, deterministically: discover → die → return knowing → break the loop."""
    store = vale.store
    result: dict[str, Any] = {}

    # --- LOOP 1: the doomed day, and what it teaches -----------------------------------------
    l1 = await begin_loop(vale, 1)
    if verbose:
        print(f"\n=== {l1.name} ===\n\n  {script.WAKE}")
    await _run_route(l1, codex, DISCOVERY_ROUTE[:4], verbose=verbose)

    # A sideways fork, MID-LOOP, from an arbitrary commit (not the origin marker) — the
    # fork-from-anywhere leg. The main line is untouched and plays on below.
    whatif = await begin_loop(
        vale, 0, from_ref=l1.last_commit, name=f"whatif-{l1.name}-seg{l1.segment}"
    )
    whatif.segment, whatif.place = l1.segment, l1.place
    whatif.discovered = list(l1.discovered)
    await _run_route(whatif, codex, ["go:p:forge"], verbose=False)
    whatif.outcome = "abandoned (sideways)"
    result["whatif"] = {
        "name": whatif.name,
        "forked_at_segment": l1.segment,
        "state": await loopmod.read_loop(store, whatif.branch_id),
    }
    if verbose:
        print(
            f"  [sideways fork taken: {whatif.name} — a what-if of this very moment, kept in "
            f"the tree. The main loop plays on.]\n"
        )

    await _run_route(l1, codex, DISCOVERY_ROUTE[4:], verbose=verbose)

    # What does the GM actually see as the doom closes? Sampled at last light, BEFORE the Fall —
    # after it, t:doom is `dead` and recall drops it (only active/offered threads are carried).
    dread = await assemble_recall(store, l1.branch_id, "what is coming?", 6)
    result["narrator_saw"] = {
        "active_threads": [t.stakes for t in dread.active_threads],
        "rumors": sorted({c.statement for c in dread.claims if c.truth != "true"}),
        "facts": sorted({c.statement for c in dread.claims if c.truth == "true"}),
    }

    fall = await commit_the_fall(l1)
    await codex.record("K4", loop=l1.name, segment=DOOM_SEGMENT)
    if verbose:
        print(f"  {fall}\n")
    result["loop1"] = {
        "discovered": sorted(l1.discovered),
        "outcome": l1.outcome,
        "state": await loopmod.read_loop(store, l1.branch_id),
    }

    # --- THE KNOWLEDGE BOUNDARY: the world forgot, the Loopwalker did not ---------------------
    l2 = await begin_loop(vale, 2)
    fresh = await store.list_claims(l2.branch_id)
    fresh_statements = {c.statement for c in fresh}
    world_remembers = sorted(k for k in KEYSTONES if CLUES[k]["statement"] in fresh_statements)
    vale_place = await store.get_place(l2.branch_id, VALE)
    result["boundary"] = {
        "codex_knows": sorted(codex.known()),
        "world_remembers": world_remembers,  # must be [] — the fork reset it
        "vale_is": vale_place.status if vale_place else "?",  # must be "active" — pristine again
    }
    if verbose:
        print(f"=== {l2.name} ===\n\n  {script.WAKE}")
        print(
            f"\n  [the Vale is {result['boundary']['vale_is']} again; this branch has never "
            f"heard of {sorted(codex.known())} — but you remember every word]\n"
        )

    # --- LOOP 2: the perfect loop ------------------------------------------------------------
    await _run_route(l2, codex, WINNING_ROUTE, verbose=verbose)
    assert can_ring(l2, codex), (
        f"the win should be available: codex={sorted(codex.known())} key={l2.holds_key} "
        f"place={l2.place} seg={l2.segment}"
    )
    opts = {o.key: o for o in options(l2, codex)}
    if verbose:
        print(await render_scene(l2, codex))
        print(f"  > {opts['ring'].label}")
    win = await choose(l2, opts["ring"], codex)
    if verbose:
        print(f"\n  {win}\n")
    result["loop2"] = {
        "outcome": l2.outcome,
        "state": await loopmod.read_loop(store, l2.branch_id),
        "holds_key": l2.holds_key,
    }

    result["tree"] = await loopmod.loop_tree(store, vale.world_id)
    result["markers"] = sorted(m.name for m in await store.list_markers(vale.world_id))
    return result


# --------------------------------------------------------------------------------------------
# Interactive play
# --------------------------------------------------------------------------------------------


async def play(vale: Vale, codex: Codex) -> None:
    n = 1
    while True:
        loop = await begin_loop(vale, n)
        print(f"\n=== {loop.name} ===\n\n  {script.WAKE}")
        while loop.segment < DOOM_SEGMENT:
            print(await render_scene(loop, codex))
            opts = options(loop, codex)
            for i, o in enumerate(opts, 1):
                print(f"    {i:>2}. {o.label}")
            print("     (or: codex / loops / whatif / quit)")
            raw = (await _ainput("  > ")).strip().lower()
            if raw in {"quit", "q"}:
                return
            if raw == "codex":
                print(render_codex(codex))
                continue
            if raw == "loops":
                print(render_loops(await loopmod.loop_tree(vale.store, vale.world_id)))
                continue
            if raw == "whatif":
                wi = await begin_loop(
                    vale,
                    0,
                    from_ref=loop.last_commit or ORIGIN_REF,
                    name=f"whatif-{loop.name}-seg{loop.segment}-{new_id()[:4]}",
                )
                print(f"  [forked sideways: {wi.name} — it will keep, unplayed, in the tree]")
                continue
            if not raw.isdigit() or not 1 <= int(raw) <= len(opts):
                print("  (no)")
                continue
            print(f"\n  {await choose(loop, opts[int(raw) - 1], codex)}\n")
        # segment 6 — the doom
        if can_ring(loop, codex):
            print(await render_scene(loop, codex))
            print("    1. RING THE SKY-BELL\n    2. do nothing, and watch it fall")
            if (await _ainput("  > ")).strip() == "1":
                print(f"\n  {await choose(loop, options(loop, codex)[0], codex)}\n")
                print(render_loops(await loopmod.loop_tree(vale.store, vale.world_id)))
                print("\n  You broke the loop. There is no tomorrow to wake into. Good.")
                return
        print(f"\n  {await commit_the_fall(loop)}\n")
        await codex.record("K4", loop=loop.name, segment=DOOM_SEGMENT)
        n += 1


async def _ainput(prompt: str) -> str:
    return await asyncio.get_event_loop().run_in_executor(None, input, prompt)


# --------------------------------------------------------------------------------------------


async def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", action="store_true", help="run the scripted story, no input")
    ap.add_argument("--scale", type=int, metavar="N", help="run the N-loop scale harness")
    ap.add_argument("--codex", choices=["file", "branch"], default="file")
    ap.add_argument("--provider", default="stub", help="stub (default) | openai | anthropic")
    ap.add_argument("--print-log", action="store_true", help="print the friction + refusal logs")
    args = ap.parse_args()

    _validate_pack()
    OUT_DIR.mkdir(exist_ok=True)
    store = await connect()
    try:
        if args.scale:
            from scale import run_scale

            await run_scale(store, args.scale, OUT_DIR)
        else:
            vale = await bootstrap(store, OUT_DIR, f"Vale of Mourn ({new_id()[:6]})")
            if args.provider != "stub":
                _wire_real_narrator(vale, args.provider)
            codex = await open_codex(
                args.codex, store=store, world_id=vale.world_id, out_dir=OUT_DIR
            )
            if args.demo:
                r = await story(vale, codex, verbose=True)
                print(render_codex(codex))
                print(render_loops(r["tree"]))
            else:
                await play(vale, codex)
    finally:
        await store.close()
    if args.print_log:
        frictionlog.print_gap_table()
        frictionlog.print_refusal_log()
        frictionlog.print_timings()


def _wire_real_narrator(vale: Vale, provider: str) -> None:
    """Opt-in real model: the NARRATOR is live; the extractor stays scripted (a real extractor
    would paraphrase the keystones and the clue-matching — which is prose-keyed because Uro mints
    claim ids — would stop recognising them; see GAP_REPORT)."""
    from uro_cli.wiring import build_router

    real = build_router(provider, None)
    scripted = vale.provider

    class Hybrid:
        async def stream(self, req: Any) -> AsyncIterator[str]:
            scripted.served.append(req.messages[-1].content)
            async for chunk in real.stream("narrator", req.messages):
                yield chunk

        async def complete(self, req: Any) -> str:
            return await scripted.complete(req)

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return await scripted.embed(texts)

    vale.engine._router = ProviderRouterShim(Hybrid())  # type: ignore[attr-defined]
    gap(
        gap="Narrate with a real model while keeping the game's fact extraction deterministic",
        happened="The provider port is all-or-nothing per role, and the ROUTER is a private "
        "engine field — there is no supported way to bind a real narrator and a scripted "
        "extractor without reaching into Engine._router",
        workaround="game.py _wire_real_narrator swaps a hybrid router in by hand",
        severity="cosmetic",
        needs="public per-role provider binding on Engine (the ProviderRouter already has "
        "`bindings` — expose it)",
        evidence="game.py _wire_real_narrator; uro_core/providers/router.py",
    )


class ProviderRouterShim:
    """The engine calls router.stream(role, messages) / router.complete(role, messages)."""

    def __init__(self, provider: Any) -> None:
        self._p = provider

    def stream(self, role: str, messages: Any) -> Any:
        from uro_core.providers.base import CompletionRequest

        return self._p.stream(CompletionRequest(messages=messages, stage_tag=role))

    async def complete(self, role: str, messages: Any) -> str:
        from uro_core.providers.base import CompletionRequest

        return await self._p.complete(CompletionRequest(messages=messages, stage_tag=role))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._p.embed(texts)


if __name__ == "__main__":
    asyncio.run(_main())
