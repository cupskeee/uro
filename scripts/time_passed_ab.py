#!/usr/bin/env python
"""Live A/B for D-84's TIME PASSED line: does telling the narrator that days went by since the
turns it is shown change what it narrates on the first beat after a time-skip?

Forks each long run just before the first beat after each of its skips, and narrates that beat
with its recorded intent and with a neutral one, N times per arm: arm A as the engine builds the
prompt (the line included), arm B the same prompt without the line. A blind judge answers two
questions per narration: (q1) does it treat the stated gap as having passed (it acknowledges the
days or opens a new moment) rather than continuing the pre-gap moment? (q2) does it contradict or
override the player's action? Ship gate (D-84): A beats B on q1 by >= 30 points overall and in at
least 4 of the 6 fork points, and q2 is no worse than B within 1 in 60.

Live: narrator + judge calls on the registry's bindings (~250 calls). Nothing is committed but the
forks themselves.

    uv run python scripts/time_passed_ab.py <out.jsonl> <campaign_id>:<beats.jsonl> [...] [--n 5]
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
from uro_core.pipeline.recall import time_passed_line
from uro_core.providers.base import Message
from uro_core.providers.registry import build_router_from_registry
from uro_core.rulesets.registry import resolve_for_campaign

DSN = os.environ.get("URO_DATABASE_URL", "postgresql://uro:uro@localhost:5433/uro")
NEUTRAL = "I look around and take stock of where things stand."
JUDGE = (
    "You judge one turn of a game master's narration. Between the PREVIOUS SCENE and this turn, "
    "the stated number of days went by. Answer two questions. q1: does the NARRATION treat that "
    "time as having passed — it acknowledges the days, or opens on a new moment — rather than "
    "continuing the previous scene's moment as if no time had passed? q2: does the narration "
    "contradict or override the PLAYER'S ACTION (have the character do something else instead)? "
    'Reply with JSON only: {"q1": true|false, "q2": true|false, "why": "<one sentence>"}'
)
LINEAGE = (
    "WITH RECURSIVE chain AS ("
    " SELECT c.commit_id, c.parent_id, c.depth FROM branches b JOIN commits c"
    " ON c.commit_id = b.head_commit WHERE b.branch_id = $1"
    " UNION ALL SELECT p.commit_id, p.parent_id, p.depth FROM commits p"
    " JOIN chain ch ON p.commit_id = ch.parent_id) "
)


async def _retry(fn, tries: int = 4):  # type: ignore[no-untyped-def]
    for attempt in range(tries):
        try:
            return await fn()
        except Exception:
            if attempt == tries - 1:
                raise
            await asyncio.sleep(5 * (attempt + 1))
    return None


async def main(out_path: str, runs: list[str], n: int) -> None:
    load_dotenv(find_dotenv(usecwd=True))
    store = PostgresEventStore(DSN)
    await store.connect()
    out = open(out_path, "a")  # noqa: SIM115 — appended to across the whole run
    sem = asyncio.Semaphore(4)
    try:
        router = await build_router_from_registry(store)
        if router is None:
            raise SystemExit("the model-connection registry is empty")
        jobs = []
        for run in runs:
            campaign_id, beats_path = run.split(":", 1)
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                raise SystemExit(f"no campaign {campaign_id}")
            engine = Engine(store, router, ruleset=await resolve_for_campaign(store, campaign))
            with open(beats_path) as f:
                rows = [json.loads(line) for line in f]
            after_skip = [rows[k + 1] for k, r in enumerate(rows[:-1]) if r["kind"] == "SKIP"]
            for row in after_skip:
                async with store.pool.acquire() as conn:
                    parent = await conn.fetchval(
                        LINEAGE + "SELECT ch.parent_id FROM chain ch JOIN events e"
                        " ON e.commit_id = ch.commit_id WHERE e.event_type = 'BeatResolved'"
                        " AND e.payload->>'intent_text' = $2 ORDER BY ch.depth LIMIT 1",
                        campaign.branch_id,
                        row["intent"],
                    )
                fork = await store.fork_branch(campaign.world_id, parent, f"tp-ab-{new_id()}")
                on = campaign.model_copy(update={"branch_id": fork.branch_id})
                for intent, label in ((row["intent"], "recorded"), (NEUTRAL, "neutral")):
                    jobs.append((engine, on, row["i"], campaign_id, intent, label))

        async def context(job):  # type: ignore[no-untyped-def]
            engine, on, i, campaign_id, intent, label = job
            ctx = await _retry(lambda: engine._context(on, "player-1", intent, None))
            messages, _ = await engine._prepare_narration(on, ctx, intent)
            days = ctx.recall.days_since_last_scene
            line = time_passed_line(days)
            with_line = messages
            last = messages[-1]
            assert days > 0 and line in last.content, (i, days)
            without = [
                *messages[:-1],
                Message(role="user", content=last.content.replace(line + "\n\n", "")),
            ]
            previous = "\n\n".join(b.narration for b in ctx.recall.recent_beats[-2:])
            role = engine._narration_role(ctx, None)
            for arm, msgs in (("A", with_line), ("B", without)):
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
                                            f"PREVIOUS SCENE\n{previous}\n\n"
                                            f"DAYS THAT WENT BY\n{days}\n\n"
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
                        "campaign": campaign_id,
                        "i": i,
                        "intent": label,
                        "arm": arm,
                        "draw": k,
                        "days": days,
                        "q1": verdict.get("q1"),
                        "q2": verdict.get("q2"),
                        "why": verdict.get("why"),
                        "narration": narration,
                    }
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    tag = f"{campaign_id[-6:]} beat {i} {label:8} {arm} #{k}"
                    print(f"{tag} q1={rec['q1']} q2={rec['q2']}", flush=True)

        results = await asyncio.gather(*(context(j) for j in jobs), return_exceptions=True)
        failed = [r for r in results if isinstance(r, BaseException)]
        print("failed contexts:", [type(f).__name__ for f in failed], flush=True)
    finally:
        out.close()
        await store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("runs", nargs="+", help="campaign_id:path/to/beats.jsonl")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()
    asyncio.run(main(args.out, args.runs, args.n))
