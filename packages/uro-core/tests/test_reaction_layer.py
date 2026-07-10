"""Reaction Layer INC-1: the post-beat hook (docs/17, D-33). Deterministic — no LLM.

Proves the mechanism a hardcoded trusted rule stands in for the future pack interpreter: after a
beat commits, a post-beat pass reads the just-committed state and commits any consequence as a
SEPARATE caused_by=module beat — and that consequence survives a fork (rebuilt by replay), exactly
like any other event. INC-3 replaces the hardcoded rule body with the pack-data interpreter.
"""

import json
from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import DomainEvent, actor_died, thread_created
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter


class _Stub:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "x"

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _engine(store: PostgresEventStore) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=_Stub()))


async def _campaign_with_dormant_thread(store: PostgresEventStore):  # type: ignore[no-untyped-def]
    world = await store.create_world(f"react-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [thread_created(thread_id="t:feud", stakes="the miners' feud", state="dormant")],
    )
    return world, campaign


async def _head(store: PostgresEventStore, branch_id: str) -> str:
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT head_commit FROM branches WHERE branch_id = $1", branch_id
        )


async def _states(store: PostgresEventStore, branch_id: str) -> dict[str, str]:
    return {t.thread_id: t.state for t in await store.list_threads(branch_id)}


async def test_death_activates_dormant_thread_as_a_module_beat(store: PostgresEventStore) -> None:
    _, campaign = await _campaign_with_dormant_thread(store)
    branch = campaign.branch_id
    died = [actor_died(actor_id="a:mook", cause="slain in the brawl")]
    await store.append_beat(branch, died)  # the trigger beat (a death committed)
    await _engine(store)._react(campaign, await _head(store, branch), died)

    assert (await _states(store, branch))["t:feud"] == "active"  # the dormant thread woke
    # the consequence is one module-caused ThreadStateChanged — auditable, un-laundered provenance
    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT e.caused_by FROM events e JOIN commits c ON c.commit_id = e.commit_id "
            "JOIN branches b ON b.world_id = c.world_id "
            "WHERE b.branch_id = $1 AND e.event_type = 'ThreadStateChanged'",
            branch,
        )
    assert len(rows) == 1
    cb = rows[0]["caused_by"]
    cb = cb if isinstance(cb, dict) else json.loads(cb)
    assert cb["kind"] == "module" and cb["rule_id"] == "inc1:death-activates-thread"


async def test_reaction_survives_a_fork(store: PostgresEventStore) -> None:
    world, campaign = await _campaign_with_dormant_thread(store)
    branch = campaign.branch_id
    await store.append_beat(branch, [actor_died(actor_id="a:mook")])
    await _engine(store)._react(
        campaign, await _head(store, branch), [actor_died(actor_id="a:mook")]
    )
    # fork AFTER the reaction — the module consequence must rebuild by replay on the sibling
    fork = await store.fork_branch(world.world_id, await _head(store, branch), "aftermath")
    assert (await _states(store, fork.branch_id))["t:feud"] == "active"


async def test_no_death_is_a_no_op(store: PostgresEventStore) -> None:
    _, campaign = await _campaign_with_dormant_thread(store)
    branch = campaign.branch_id
    head_before = await _head(store, branch)
    await _engine(store)._react(campaign, head_before, [])  # no ActorDied in the trigger
    assert (await _states(store, branch))["t:feud"] == "dormant"  # untouched
    assert await _head(store, branch) == head_before  # no empty module commit


async def test_no_dormant_thread_is_a_no_op(store: PostgresEventStore) -> None:
    world = await store.create_world(f"react-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    # a thread that is already active — the death rule must not touch it
    await store.append_beat(
        campaign.branch_id, [thread_created(thread_id="t:x", stakes="s", state="active")]
    )
    head_before = await _head(store, campaign.branch_id)
    events: list[DomainEvent] = [actor_died(actor_id="a:x")]
    await _engine(store)._react(campaign, head_before, events)
    assert await _head(store, campaign.branch_id) == head_before  # nothing to do → no commit
