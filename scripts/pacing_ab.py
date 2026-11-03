#!/usr/bin/env python
"""Live A/B for a pacing nudge: does naming one quiet plot thread beside the intent make the
narrator bring it back into the scene, without overriding the player or forcing it in?

Forks each long run just before a beat where a live thread had gone quiet (fork points chosen from
judge labels of which threads each beat touched), and narrates that beat with its recorded intent
and with a neutral one, N times per arm: arm A is the engine's prompt plus the pacing line naming
the quiet thread, arm B the engine's prompt as it is. A judge blind to the arm answers: (q1) does
the narration bring that thread into play? (q2) does it contradict or override the player's
action? (q3, when q1) does the thread follow naturally from the scene, or is it forced?

Ship gate (pre-registered): A beats B on q1 by >= 30 points overall and at >= 6 of the 8 fork
points; q2 is no worse than B by more than 2 in 80; and q3 judges at most 20% of A's q1 narrations
forced.

Live: narrator + judge calls on the registry's bindings (~320 calls). Nothing is committed but the
forks themselves.

    uv run python scripts/pacing_ab.py <out.jsonl> <forkpoints.json> [--n 5]

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
from uro_core.providers.base import PLAYER_ACTION, Message
from uro_core.providers.registry import build_router_from_registry
from uro_core.rulesets.registry import resolve_for_campaign

DSN = os.environ.get("URO_DATABASE_URL", "postgresql://uro:uro@localhost:5433/uro")
NEUTRAL = "I look around and take stock of where things stand."
LINE = (
    "A PLOT TO KEEP ALIVE: {stakes} If it can fit the player's action, let this scene touch it: a "
    "reminder, a person, a clue or a consequence tied to it. Never at the cost of the player's "
    "action, and don't resolve it unless the action does."
)
JUDGE = (
    "You judge one turn of a game master's narration against one of the story's PLOT THREADS. "
    "q1: does the NARRATION bring that thread into play: it mentions the thread's plot, develops "
    "it, advances it or complicates it? Sharing a setting, a mood or a common word is not enough. "
    "q2: does the narration contradict or override the PLAYER'S ACTION (have the character do "
    "something else instead, or skip what they tried)? q3: only if q1 is true, does the thread "
    "come in naturally from the scene and the action (true), or is it forced or contrived "
    "(false)? If q1 is false, q3 is null. Reply with JSON only: "
    '{"q1": true|false, "q2": true|false, "q3": true|false|null, "why": "<one sentence>"}'
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
            engine = Engine(store, router, ruleset=await resolve_for_campaign(store, campaign))
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
            fork = await store.fork_branch(campaign.world_id, parent, f"pacing-ab-{new_id()}")
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
            line = LINE.format(stakes=thread.stakes.rstrip(".") + ".")
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
                        "why": verdict.get("why"),
                        "narration": narration,
                    }
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    tag = f"{p['campaign'][-6:]} beat {p.get('k')} {label:8} {arm} #{k}"
                    print(f"{tag} q1={rec['q1']} q2={rec['q2']} q3={rec['q3']}", flush=True)

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
