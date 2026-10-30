#!/usr/bin/env python
"""Replay a world's RECORDED narrations through the CURRENT extractor (the D-51 tuning harness).

Isolates the extractor from the narrator: the prose is fixed — read from the source world's
`BeatResolved` events — so runs across extractor versions compare like with like (a live re-run
would change the story too). Each beat replays, in order, onto a FRESH world: recall is assembled
as in live play (recent beats + state so far), the extractor runs on the recorded narration, and
the gauntlet commits. A per-beat log and a summary then set the replay beside what the original
run committed. Analyze further from Postgres (the replay world is named in the output).

    uv run python scripts/extraction_replay.py Solara                  # the registry's extractor
    uv run python scripts/extraction_replay.py Solara --provider stub  # dry-run the harness

Live mode spends one extractor call per beat (+ up to 2 re-asks) on the `extractor` role binding
of the model-connection registry (D-47) — the same model live play used. No narrator calls.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter
from typing import Any

from dotenv import find_dotenv, load_dotenv
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import DomainEvent, actor_created, beat_resolved
from uro_core.domain.ids import new_id
from uro_core.pipeline.extraction import (
    _statement_key,
    build_extractor_messages,
    parse_extraction,
    run_gauntlet,
)
from uro_core.pipeline.recall import assemble_recall
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.base import Message
from uro_core.providers.registry import build_router_from_registry
from uro_core.providers.router import ProviderRouter

DSN = os.environ.get("URO_DATABASE_URL", "postgresql://uro:uro@localhost:5433/uro")
RECENCY = 8  # the Engine default
_CREATED = ("ActorCreated", "PlaceCreated", "FactionCreated", "ThreadCreated", "ClaimRecorded")


def _label(e: DomainEvent | dict[str, Any]) -> str:
    t, p = (e.event_type, e.payload) if isinstance(e, DomainEvent) else (e["type"], e["payload"])
    if t == "EdgeAdded":
        return f"Edge {p['src']} -{p['rel_type']}-> {p['dst']}"
    if t == "ThreadStateChanged":
        return f"Thread {p['thread_id']} -> {p['to_state']}"
    text = p.get("name") or p.get("stakes") or p.get("statement") or ""
    return f"{t.removesuffix('Created').removesuffix('Recorded')}: {text}"


async def _beat_branch(store: PostgresEventStore, world_id: str) -> tuple[str, int]:
    """The source world's branch with the most beats (its played campaign)."""
    best = ("", -1)
    for b in await store.list_branches(world_id):
        n = len(await store.recent_beats(b.branch_id, 10_000))
        best = max(best, (b.branch_id, n), key=lambda x: x[1])
    return best


async def _summary(store: PostgresEventStore, world_id: str) -> dict[str, int]:
    # Only commits that hold a played beat — so the campaign's starting PC (and any pack genesis)
    # isn't counted as something the extractor produced.
    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT e.event_type, e.payload FROM events e JOIN commits c USING (commit_id) "
            "WHERE c.world_id = $1 AND EXISTS (SELECT 1 FROM events b WHERE b.commit_id = "
            "c.commit_id AND b.event_type = 'BeatResolved') ORDER BY c.depth, e.seq",
            world_id,
        )
    counts = Counter(r["event_type"] for r in rows)
    seen: set[tuple[str, frozenset[str], str]] = set()
    dup_claims = 0
    for r in rows:
        if r["event_type"] == "ClaimRecorded":
            p = r["payload"]
            key = (_statement_key(p["statement"]), frozenset(p["subject_refs"]), p["truth"])
            dup_claims += key in seen
            seen.add(key)
    return {
        "beats": counts["BeatResolved"],
        "actors": counts["ActorCreated"],
        "places": counts["PlaceCreated"],
        "factions": counts["FactionCreated"],
        "threads": counts["ThreadCreated"],
        "threads resolved": counts["ThreadStateChanged"],
        "edges": counts["EdgeAdded"],
        "claims": counts["ClaimRecorded"],
        "claims (exact repeats)": dup_claims,
    }


async def _extract(router: ProviderRouter, messages: list[Message]) -> tuple[Any, int]:
    """1 attempt + 2 re-asks, as Engine._extract does. Returns (Extraction | None, calls)."""
    for attempt in range(1, 4):
        raw = await router.complete(
            "extractor", messages, json_mode=True, temperature=0.1, max_tokens=4096
        )
        extraction = parse_extraction(raw)
        if extraction is not None:
            return extraction, attempt
        messages = [
            *messages,
            Message(
                role="user",
                content="That was not a parseable JSON object matching the schema. "
                "Return ONLY the corrected JSON object, nothing else.",
            ),
        ]
    return None, 3


async def _main(source: str, provider: str) -> None:
    load_dotenv(find_dotenv(usecwd=True))  # URO_SECRET_KEY decrypts the registry's credentials
    store = PostgresEventStore(DSN)
    await store.connect()
    try:
        src = await store.get_world_by_name(source)
        if src is None:
            raise SystemExit(f"no world named {source!r}")
        src_branch, n = await _beat_branch(store, src.world_id)
        beats = await store.recent_beats(src_branch, 10_000)
        if provider == "stub":
            router = ProviderRouter({}, default=StubProvider())
        else:
            built = await build_router_from_registry(store)
            if built is None:
                raise SystemExit("the model-connection registry is empty (see `uro provider`)")
            router = built
        policy = await store.get_extraction_policy()
        replay = await store.create_world(f"replay-{source}-{new_id()}")
        branch = replay.main_branch_id
        # Seed the source campaign's PCs (outside any beat): live, the PC is a KNOWN actor once
        # named, so without it the replay would mint a duplicate the original never could.
        async with store.pool.acquire() as conn:
            pcs = await conn.fetch(
                "SELECT a.name, a.role FROM proj_pcs p JOIN proj_actors a ON "
                "a.branch_id = p.branch_id AND a.actor_id = p.actor_id WHERE p.branch_id = $1",
                src_branch,
            )
        if pcs:
            await store.append_beat(
                branch,
                [
                    actor_created(actor_id=f"a:{new_id()}", name=r["name"], tier=2, role=r["role"])
                    for r in pcs
                ],
            )
        print(f"replaying {n} beats of {source!r} → world {replay.name!r} ({provider})\n")

        calls = 0
        for i, b in enumerate(beats, 1):
            recall = await assemble_recall(store, branch, b.intent_text, RECENCY)
            extraction, used = await _extract(router, build_extractor_messages(recall, b.narration))
            calls += used
            events = (
                await run_gauntlet(store, branch, extraction, policy=policy) if extraction else []
            )
            await store.append_beat(
                branch,
                [
                    beat_resolved(
                        beat_id=new_id(),
                        participant_id=b.participant_id,
                        intent_text=b.intent_text,
                        narration=b.narration,
                    ),
                    *events,
                ],
            )
            note = "" if extraction else "  (unparseable after re-asks)"
            kinds = (*_CREATED, "EdgeAdded", "ThreadStateChanged")
            shown = [_label(e) for e in events if e.event_type in kinds]
            print(f"beat {i:>2}: {' || '.join(shown) or '(nothing)'}{note}")

        before = await _summary(store, src.world_id)
        after = await _summary(store, replay.world_id)
        print(f"\n{'':24}{'original':>10}{'replay':>10}")
        for k in before:
            print(f"{k:24}{before[k]:>10}{after[k]:>10}")
        print(f"{'extractor calls':24}{'':>10}{calls:>10}")
    finally:
        await store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("world", help="name of the source world whose narrations to replay")
    ap.add_argument("--provider", choices=["registry", "stub"], default="registry")
    args = ap.parse_args()
    asyncio.run(_main(args.world, args.provider))
