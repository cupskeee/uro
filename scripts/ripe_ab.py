#!/usr/bin/env python
"""Live A/B for the ripe nudge (D-102): does naming a well-developed, still-open plot beside the
intent, asking to bring it toward its end, make the narrator move it there — without overriding the
player, forcing it in, or settling it out of turn?

Forks each long run just before a beat where a play-made thread had been developed many times and
was in play (fork points chosen from judge labels of which threads each beat touched), and narrates
that beat with its recorded intent and with a neutral one, N times per arm: arm A is the engine's
prompt plus the candidate line (`RIPE`, below), arm B the prompt as it is (the director off in
both). A judge blind to the arm answers: (q1) does the narration move that plot toward its end — a
revelation that answers part of it, a confrontation with who is behind it, or a decisive choice put
to the player? (q2) does it contradict or override the player's action? (q3, when q1) is the move
natural or forced? (q4) does it settle the plot outright?

Ship gate (pre-registered): A beats B on q1 by >= 30 points overall and at >= 6 of the 9 fork
points; q2 is no worse than B by more than 2 in 90; q3 judges at most 20% of A's q1 narrations
forced. q4 is reported, not gated.

Live: narrator + judge calls on the registry's bindings (~320 calls). Nothing is committed but the
forks themselves. The engine is built with the director off, so arm B carries no line; the D-95 run
was made before the director existed, on the same prompt.

    uv run python scripts/ripe_ab.py <out.jsonl> <forkpoints.json> [--n 5]

forkpoints.json: [{"campaign": id, "intent": recorded intent, "thread": thread_id}, ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from dotenv import find_dotenv, load_dotenv
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.pacing import PacingSettings
from uro_core.providers.base import PLAYER_ACTION, Message
from uro_core.providers.registry import build_router_from_registry
from uro_core.rulesets.registry import resolve_for_campaign

DSN = os.environ.get("URO_DATABASE_URL", "postgresql://uro:uro@localhost:5433/uro")
NEUTRAL = "I look around and take stock of where things stand."

# The candidate line (D-102). It FAILED the gate below (+18 points, not +30) and was not shipped;
# kept here so the measurement can be re-run or a new wording tried against the same gate.
RIPE = (
    "A PLOT READY TO TURN: {stakes}. It has been building for a while. If it can fit the player's "
    "action, let this scene bring it closer to its end: a revelation, a confrontation, or a choice "
    "the player must make. Never at the cost of the player's action; settle it only if the action "
    "does."
)
JUDGE = (
    "You judge one turn of a game master's narration against one of the story's PLOT THREADS. "
    "q1: does the NARRATION move that plot toward its end: a revelation that answers part of its "
    "question, a confrontation with whoever is behind it, or a decisive choice put to the player? "
    "Merely mentioning or deepening the mystery is not enough. q2: does the narration contradict "
    "or override the PLAYER'S ACTION (have the character do something else instead, or skip what "
    "they tried)? q3: only if q1 is true, does the move come naturally from the scene and the "
    "action (true), or is it forced or contrived (false)? If q1 is false, q3 is null. q4: does "
    "the narration settle the plot outright (its question fully answered or its conflict ended)? "
    'Reply with JSON only: {"q1": true|false, "q2": true|false, "q3": true|false|null, '
    '"q4": true|false, "why": "<one sentence>"}'
)
LINEAGE = (
    "WITH RECURSIVE chain AS ("
    " SELECT c.commit_id, c.parent_id, c.depth FROM branches b JOIN commits c"
    " ON c.commit_id = b.head_commit WHERE b.branch_id = $1"
    " UNION ALL SELECT p.commit_id, p.parent_id, p.depth FROM commits p"
    " JOIN chain ch ON p.commit_id = ch.parent_id) "
)


async def _retry(fn, tries: int = 6):  # type: ignore[no-untyped-def]
    for attempt in range(tries):
        try:
            return await fn()
        except Exception:
            if attempt == tries - 1:
                raise
            await asyncio.sleep(min(120, 10 * 2**attempt))
    return None


async def main(out_path: str, points_path: str, n: int) -> None:
    load_dotenv(find_dotenv(usecwd=True))
    store = PostgresEventStore(DSN)
    await store.connect()
    out = open(out_path, "a")  # noqa: SIM115 — appended to across the whole run
    sem = asyncio.Semaphore(4)
    try:
        router = await build_router_from_registry(store)
        if router is None:
            raise SystemExit("the model-connection registry is empty")
        with open(points_path) as f:
            points = json.load(f)
        jobs = []
        for p in points:
            campaign = await store.get_campaign(p["campaign"])
            if campaign is None:
                raise SystemExit(f"no campaign {p['campaign']}")
            # the director itself off: arm B is the prompt with no line at all (D-95 review)
            engine = Engine(
                store,
                router,
                ruleset=await resolve_for_campaign(store, campaign),
                pacing=PacingSettings(quiet_after=0),
            )
            async with store.pool.acquire() as conn:
                parent = await conn.fetchval(
                    LINEAGE + "SELECT ch.parent_id FROM chain ch JOIN events e"
                    " ON e.commit_id = ch.commit_id WHERE e.event_type = 'BeatResolved'"
                    " AND e.payload->>'intent_text' = $2 ORDER BY ch.depth LIMIT 1",
                    campaign.branch_id,
                    p["intent"],
                )
            if parent is None:
                raise SystemExit(f"no beat with that intent in {p['campaign']}")
            fork = await store.fork_branch(campaign.world_id, parent, f"ripe-ab-{new_id()}")
            on = campaign.model_copy(update={"branch_id": fork.branch_id})
            threads = {t.thread_id: t for t in await store.list_threads(fork.branch_id)}
            thread = threads[p["thread"]]
            for intent, label in ((p["intent"], "recorded"), (NEUTRAL, "neutral")):
                jobs.append((engine, on, p, thread, intent, label))

        async def context(job):  # type: ignore[no-untyped-def]
            engine, on, p, thread, intent, label = job
            ctx = await _retry(lambda: engine._context(on, "player-1", intent, None))
            messages, _ = await engine._prepare_narration(on, ctx, intent)
            last = messages[-1]
            assert last.content.count(PLAYER_ACTION) == 1
            assert "A PLOT" not in last.content  # a true no-line control
            line = RIPE.format(stakes=thread.stakes.rstrip("."))
            with_line = [
                *messages[:-1],
                Message(
                    role="user",
                    content=last.content.replace(PLAYER_ACTION, line + "\n\n" + PLAYER_ACTION),
                ),
            ]
            previous = "\n\n".join(b.narration for b in ctx.recall.recent_beats[-2:])
            role = engine._narration_role(ctx, None)
            for arm, msgs in (("A", with_line), ("B", messages)):
                for k in range(n):

                    async def narrate(msgs: list[Message] = msgs) -> str:
                        async with sem:
                            chunks = [
                                c async for c in router.stream(role, msgs, branch_id=on.branch_id)
                            ]
                        return "".join(chunks).strip()

                    narration = await _retry(narrate)

                    async def ask(narration: str = narration) -> str:
                        async with sem:
                            return await router.complete(
                                "judge",
                                [
                                    Message(role="system", content=JUDGE),
                                    Message(
                                        role="user",
                                        content=(
                                            f"PLOT THREAD\n{thread.stakes}\n\n"
                                            f"PREVIOUS SCENE\n{previous}\n\n"
                                            f"PLAYER'S ACTION\n{intent}\n\n"
                                            f"NARRATION\n{narration}"
                                        ),
                                    ),
                                ],
                                json_mode=True,
                                temperature=0.0,
                                max_tokens=200,
                            )

                    try:
                        verdict = json.loads(await _retry(ask))
                    except ValueError:
                        verdict = {}
                    rec = {
                        "campaign": p["campaign"],
                        "k": p.get("k"),
                        "thread": thread.thread_id,
                        "intent": label,
                        "arm": arm,
                        "draw": k,
                        "q1": verdict.get("q1"),
                        "q2": verdict.get("q2"),
                        "q3": verdict.get("q3"),
                        "q4": verdict.get("q4"),
                        "why": verdict.get("why"),
                        "narration": narration,
                    }
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    tag = f"{p['campaign'][-6:]} beat {p.get('k')} {label:8} {arm} #{k}"
                    print(
                        f"{tag} q1={rec['q1']} q2={rec['q2']} q3={rec['q3']} q4={rec['q4']}",
                        flush=True,
                    )

        results = await asyncio.gather(*(context(j) for j in jobs), return_exceptions=True)
        failed = [r for r in results if isinstance(r, BaseException)]
        print("failed contexts:", [repr(f) for f in failed], flush=True)
    finally:
        out.close()
        await store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("points")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()
    asyncio.run(main(args.out, args.points, args.n))
