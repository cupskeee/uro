"""The transactional outbox + relay (D-57, docs/07).

Asserts the guarantees, not the plumbing: a commit is published iff it exists (same transaction),
in commit order, at-least-once (a failed publish stays pending and is retried), never twice under
concurrent relays (SKIP LOCKED), and — against a real NATS JetStream server, when one is reachable —
a re-sent commit is deduplicated by the stream.
"""

import asyncio
import os

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created
from uro_core.domain.ids import new_id
from uro_core.outbox import InProcessPublisher, relay_outbox
from uro_core.ports.outbox import OutboxMessage

NATS_URL = os.environ.get("URO_TEST_NATS", "nats://localhost:4222")


@pytest.fixture
async def outbox(store: PostgresEventStore) -> PostgresEventStore:
    # The test DB accumulates commits from every other test; start each case with nothing pending.
    async with store.pool.acquire() as conn:
        await conn.execute("UPDATE outbox SET published_at = now() WHERE published_at IS NULL")
    return store


def _collector() -> tuple[InProcessPublisher, list[OutboxMessage]]:
    publisher, seen = InProcessPublisher(), []

    async def keep(message: OutboxMessage) -> None:
        seen.append(message)

    publisher.subscribe(keep)
    return publisher, seen


async def test_every_commit_is_queued_in_its_own_transaction(outbox: PostgresEventStore) -> None:
    world = await outbox.create_world(f"outbox-{new_id()}")  # genesis is a commit too
    branch = world.main_branch_id
    await outbox.append_beat(branch, [actor_created(actor_id="a:mara", name="Mara")])
    assert await outbox.outbox_pending() == 2
    # A commit rolled back with its transaction leaves no outbox row behind.
    with pytest.raises(RuntimeError):
        async with outbox.pool.acquire() as conn, conn.transaction():
            await outbox._append(conn, branch, [actor_created(actor_id="a:ghost", name="Ghost")])
            raise RuntimeError("the transaction fails after the commit was written")
    assert await outbox.outbox_pending() == 2


async def test_the_relay_publishes_commits_in_order_with_their_events(
    outbox: PostgresEventStore,
) -> None:
    world = await outbox.create_world(f"outbox-{new_id()}")
    first = await outbox.append_beat(
        world.main_branch_id, [actor_created(actor_id="a:1", name="A")]
    )
    second = await outbox.append_beat(
        world.main_branch_id, [actor_created(actor_id="a:2", name="B")]
    )
    publisher, seen = _collector()
    assert await relay_outbox(outbox, publisher, limit=2) == 3  # genesis + two beats, two batches
    mine = [m for m in seen if m.world_id == world.world_id]
    assert [m.commit_id for m in mine][1:] == [first.commit_id, second.commit_id]
    assert mine[2].parent_id == first.commit_id and mine[2].depth == second.depth
    assert [e["event_type"] for e in mine[2].events] == ["ActorCreated"]
    assert await relay_outbox(outbox, publisher) == 0  # nothing left — published exactly once


async def test_a_failed_publish_stays_pending_and_is_retried(outbox: PostgresEventStore) -> None:
    world = await outbox.create_world(f"outbox-{new_id()}")
    flaky, attempts = InProcessPublisher(), []

    async def fail_once(message: OutboxMessage) -> None:
        attempts.append(message.commit_id)
        if len(attempts) == 1:
            raise ConnectionError("broker unavailable")

    flaky.subscribe(fail_once)
    with pytest.raises(ConnectionError):
        await relay_outbox(outbox, flaky)
    assert await outbox.outbox_pending() == 1  # rolled back, not lost
    assert await relay_outbox(outbox, flaky) == 1  # the retry delivers it
    assert attempts[0] == attempts[1]  # the same commit, delivered at-least-once
    assert world.world_id  # (the genesis commit was the one in flight)


async def test_concurrent_relays_never_publish_a_commit_twice(outbox: PostgresEventStore) -> None:
    world = await outbox.create_world(f"outbox-{new_id()}")
    for i in range(9):
        await outbox.append_beat(
            world.main_branch_id, [actor_created(actor_id=f"a:{i}", name=f"N{i}")]
        )
    publisher, seen = InProcessPublisher(), []

    async def slow(message: OutboxMessage) -> None:
        await asyncio.sleep(0.01)  # hold the batch's row locks long enough to overlap
        seen.append(message.commit_id)

    publisher.subscribe(slow)
    counts = await asyncio.gather(*(relay_outbox(outbox, publisher, limit=3) for _ in range(3)))
    assert sum(counts) == 10 and len(seen) == len(set(seen)) == 10


async def _nats_or_skip():  # type: ignore[no-untyped-def]
    nats = pytest.importorskip("nats")
    try:
        nc = await asyncio.wait_for(nats.connect(NATS_URL, max_reconnect_attempts=0), timeout=2)
    except Exception:
        pytest.skip(f"no NATS JetStream at {NATS_URL} (docker compose --profile nats up -d)")
    return nc


async def test_nats_jetstream_receives_commits_and_drops_a_resend(
    outbox: PostgresEventStore,
) -> None:
    from uro_core.adapters.nats import NatsJetStreamPublisher

    nc = await _nats_or_skip()
    stream = f"UROTEST{new_id()}"
    publisher = NatsJetStreamPublisher(NATS_URL, stream=stream, subject_prefix=stream.lower())
    await publisher.connect()
    try:
        world = await outbox.create_world(f"outbox-{new_id()}")
        commit = await outbox.append_beat(
            world.main_branch_id, [actor_created(actor_id="a:x", name="X")]
        )
        assert await relay_outbox(outbox, publisher) == 2
        js = nc.jetstream()
        assert (await js.stream_info(stream)).state.messages == 2
        last = await js.get_last_msg(
            stream, f"{stream.lower()}.{world.world_id}.{world.main_branch_id}"
        )
        assert OutboxMessage.model_validate_json(last.data).commit_id == commit.commit_id
        # A relay that crashed after publishing but before marking re-sends the commit: dropped.
        async with outbox.pool.acquire() as conn:
            await conn.execute(
                "UPDATE outbox SET published_at = NULL WHERE commit_id = $1", commit.commit_id
            )
        assert await relay_outbox(outbox, publisher) == 1
        assert (await js.stream_info(stream)).state.messages == 2  # deduplicated by Nats-Msg-Id
        await js.delete_stream(stream)
    finally:
        await publisher.close()
        await nc.close()


# --- holistic-review fixes (2026-10-30) ----------------------------------------------------


async def test_a_zero_batch_is_refused_not_spun(outbox: PostgresEventStore) -> None:
    # Review: limit=0 never drained and never satisfied `sent < limit` — a hot loop.
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await relay_outbox(outbox, InProcessPublisher(), limit=0)


async def test_the_relay_daemon_survives_a_publish_error(outbox: PostgresEventStore) -> None:
    # Review: one publish error ended `uro outbox relay`, stranding every world's commits.
    from uro_core.outbox import relay_forever

    world = await outbox.create_world(f"outbox-{new_id()}")
    flaky, delivered, stop = InProcessPublisher(), [], asyncio.Event()

    async def fail_then_deliver(message: OutboxMessage) -> None:
        if not delivered and not getattr(fail_then_deliver, "failed", False):
            fail_then_deliver.failed = True  # type: ignore[attr-defined]
            raise ConnectionError("broker briefly unavailable")
        delivered.append(message.commit_id)
        stop.set()

    flaky.subscribe(fail_then_deliver)
    await asyncio.wait_for(relay_forever(outbox, flaky, interval=0.01, stop=stop), timeout=10)
    assert delivered and await outbox.outbox_pending() == 0  # retried and delivered, still running
    assert world.world_id


async def test_a_stream_that_misses_our_prefix_is_refused_at_connect() -> None:
    # Review: an existing stream capturing another prefix made EVERY publish fail, forever.
    from uro_core.adapters.nats import NatsJetStreamPublisher

    nc = await _nats_or_skip()
    stream = f"UROTEST{new_id()}"
    js = nc.jetstream()
    await js.add_stream(name=stream, subjects=[f"{stream.lower()}-other.>"])
    try:
        publisher = NatsJetStreamPublisher(NATS_URL, stream=stream, subject_prefix=stream.lower())
        with pytest.raises(RuntimeError, match="captures"):
            await publisher.connect()
        await publisher.close()
    finally:
        await js.delete_stream(stream)
        await nc.close()
