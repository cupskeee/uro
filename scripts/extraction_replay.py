#!/usr/bin/env python
"""Replay a world's RECORDED narrations through the CURRENT extractor (the D-51 tuning harness).

Isolates the extractor from the narrator: the prose is fixed — read from the source world's
`BeatResolved` events — so runs across extractor versions compare like with like (a live re-run
would change the story too). Each beat replays, in order, onto a FORK of the source world taken
just before its first beat — so the extractor starts from exactly what the original saw: the
pack's cast, the campaign's PC, a seeded rumor and who believes it. Recall is assembled as in live
play (recent beats + state so far), the extractor runs on the recorded narration, and the gauntlet
commits. A per-beat log and a summary then set the replay beside what the original run committed.
Analyze further from Postgres (the replay branch is named in the output). Commits between beats
that weren't beats (a time-skip, a reaction) aren't replayed.

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
from uro_core.domain.events import DomainEvent, beat_resolved
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


_LINEAGE = (
    "WITH RECURSIVE chain AS ("
    "  SELECT c.commit_id, c.parent_id, c.depth FROM commits c "
    "  JOIN branches b ON b.head_commit = c.commit_id WHERE b.branch_id = $1 "
    "  UNION ALL SELECT p.commit_id, p.parent_id, p.depth FROM commits p "
    "  JOIN chain ch ON p.commit_id = ch.parent_id) "
)
_HAS_BEAT = (
    "EXISTS (SELECT 1 FROM events b WHERE b.commit_id = ch.commit_id "
    "AND b.event_type = 'BeatResolved')"
)


async def _before_first_beat(store: PostgresEventStore, branch_id: str) -> str:
    """The commit just before the branch's first beat: the state its play started from."""
    async with store.pool.acquire() as conn:
        parent = await conn.fetchval(
            f"{_LINEAGE} SELECT ch.parent_id FROM chain ch WHERE {_HAS_BEAT} "
            "ORDER BY ch.depth LIMIT 1",
            branch_id,
        )
    if parent is None:
        raise SystemExit("the source has no beats to replay")
    return str(parent)


async def _summary(store: PostgresEventStore, branch_id: str) -> dict[str, int]:
    # Only commits that hold a played beat, along the branch's own history — so the campaign's
    # starting PC, any pack genesis and the shared pre-play state aren't counted as something the
    # extractor produced.
    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            f"{_LINEAGE} SELECT e.event_type, e.payload FROM chain ch "
            f"JOIN events e ON e.commit_id = ch.commit_id WHERE {_HAS_BEAT} "
            "ORDER BY ch.depth, e.seq",
            branch_id,
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


async def _extract(
    router: ProviderRouter, messages: list[Message], store: PostgresEventStore, branch_id: str
) -> tuple[Any, int]:
    """1 attempt + 2 re-asks, as Engine._extract does. Returns (Extraction | None, calls). Each call
    is metered onto the replay branch (D-66), so its full response can be read afterwards."""
    for attempt in range(1, 4):
        raw = await router.complete(
            "extractor",
            messages,
            json_mode=True,
            temperature=0.1,
            max_tokens=4096,
            branch_id=branch_id,
            recorder=store.record_llm_call,
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
        src = await store.get_world(source)  # an id, else a name that names exactly one world
        if src is None:
            named = [w for w in await store.list_worlds() if w.name == source]
            if len(named) > 1:
                ids = ", ".join(w.world_id for w in named)
                raise SystemExit(f"{len(named)} worlds are named {source!r}; give an id: {ids}")
            if not named:
                raise SystemExit(f"no world {source!r}")
            src = named[0]
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
        start = await _before_first_beat(store, src_branch)
        fork = await store.fork_branch(src.world_id, start, f"replay-{new_id()}")
        branch = fork.branch_id
        print(f"replaying {n} beats of {src.name!r} → branch {fork.name!r} ({provider})\n")

        calls = 0
        for i, b in enumerate(beats, 1):
            recall = await assemble_recall(store, branch, b.intent_text, RECENCY)
            messages = build_extractor_messages(recall, b.narration)
            extraction, used = await _extract(router, messages, store, branch)
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

        before = await _summary(store, src_branch)
        after = await _summary(store, branch)
        print(f"\n{'':24}{'original':>10}{'replay':>10}")
        for k in before:
            print(f"{k:24}{before[k]:>10}{after[k]:>10}")
        print(f"{'extractor calls':24}{'':>10}{calls:>10}")
    finally:
        await store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("world", help="the source world (id or name) whose narrations to replay")
    ap.add_argument("--provider", choices=["registry", "stub"], default="registry")
    args = ap.parse_args()
    asyncio.run(_main(args.world, args.provider))
