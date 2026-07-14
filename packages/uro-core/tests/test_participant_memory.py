"""B8 (docs/18, D-36) — participant-scoped memory that survives a fork. Deterministic, no LLM.

The lane is keyed on (participant_id, world_ref), NOT branch_id: it is not a projection, not in the
snapshot tables, and never touched by fork_branch — so a player's out-of-world knowledge survives a
fork/reset (time-loop / NG+) and can never leak into world canon or NPC belief.
"""

from uro_core.adapters.postgres.projector import _SNAPSHOT_TABLES
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import place_created
from uro_core.domain.ids import new_id
from uro_core.pipeline.recall import assemble_recall


async def _head(store: PostgresEventStore, branch_id: str) -> str:
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT head_commit FROM branches WHERE branch_id = $1", branch_id
        )


def test_participant_notes_is_not_a_fork_copied_projection() -> None:
    # structural guard: the lane must NOT be a snapshot table, or a fork would copy/reset it
    assert "participant_notes" not in _SNAPSHOT_TABLES


async def test_note_survives_a_fork(store: PostgresEventStore) -> None:
    world = await store.create_world(f"loop-{new_id()}")
    await store.participant_remember(
        "p1", world.world_id, "the vault code is 4-7-2", key="vault", pinned=True
    )
    # fork the world back (a new branch off the current head) — the time-loop reset
    fork = await store.fork_branch(
        world.world_id, await _head(store, world.main_branch_id), "loop-2"
    )
    # the player still knows it on the forked branch (it's world-scoped, untouched by the fork)
    notes = await store.participant_notes("p1", world.world_id)
    assert [n.text for n in notes] == ["the vault code is 4-7-2"]
    # and it is genuinely outside the branch axis: a fork exists but the note isn't branch-keyed
    assert fork.branch_id != world.main_branch_id


async def test_dedup_by_key_is_last_writer_wins(store: PostgresEventStore) -> None:
    world = await store.create_world(f"dedup-{new_id()}")
    await store.participant_remember("p1", world.world_id, "code is 111", key="vault")
    await store.participant_remember("p1", world.world_id, "code is 222", key="vault")  # re-learn
    notes = await store.participant_notes("p1", world.world_id)
    assert [n.text for n in notes] == ["code is 222"]  # one row, overwritten


async def test_scoped_to_participant_and_world(store: PostgresEventStore) -> None:
    a = await store.create_world(f"wa-{new_id()}")
    b = await store.create_world(f"wb-{new_id()}")
    await store.participant_remember("p1", a.world_id, "secret A")
    assert len(await store.participant_notes("p1", a.world_id)) == 1
    assert await store.participant_notes("p2", a.world_id) == []  # other participant: none
    assert await store.participant_notes("p1", b.world_id) == []  # other world: none


async def test_recall_surfaces_pinned_always_and_triggered_when_mentioned(
    store: PostgresEventStore,
) -> None:
    world = await store.create_world(f"recall-{new_id()}")
    branch = world.main_branch_id
    await store.participant_remember("p1", world.world_id, "you have died here before", pinned=True)
    await store.participant_remember(
        "p1", world.world_id, "the vault code is 4-7-2", key="vault", entity_refs=["vault"]
    )
    # intent mentions the vault → both the pinned note and the triggered note surface
    r = await assemble_recall(
        store, branch, "I approach the vault", 8, participant_id="p1", world_ref=world.world_id
    )
    texts = {n.text for n in r.participant_notes}
    assert texts == {"you have died here before", "the vault code is 4-7-2"}
    # intent does NOT mention the vault → only the pinned note surfaces
    r2 = await assemble_recall(
        store, branch, "I look at the sky", 8, participant_id="p1", world_ref=world.world_id
    )
    assert {n.text for n in r2.participant_notes} == {"you have died here before"}
    # no participant_id/world_ref → nothing (party isolation / off by default)
    r3 = await assemble_recall(store, branch, "I approach the vault", 8)
    assert r3.participant_notes == []


async def test_note_never_becomes_canon_or_a_belief(store: PostgresEventStore) -> None:
    # leak-closure: a surfaced note is NOT a claim and NOT any actor's belief (guards the one
    # structural-by-omission invariant against a regression that wires notes into extraction).
    world = await store.create_world(f"leak-{new_id()}")
    branch = world.main_branch_id
    await store.append_beat(branch, [place_created(place_id="p:vault", name="Vault")])
    await store.participant_remember(
        "p1", world.world_id, "the vault code is 4-7-2", key="vault", entity_refs=["vault"]
    )
    r = await assemble_recall(
        store, branch, "I approach the Vault", 8, participant_id="p1", world_ref=world.world_id
    )
    assert any("4-7-2" in n.text for n in r.participant_notes)  # it DID surface to the player
    # …but it is nowhere in canon: no claim carries it, no belief references it
    claims = await store.list_claims(branch)
    assert not any("4-7-2" in c.statement for c in claims)
    for actor in await store.list_actors(branch):
        assert await store.beliefs_of(branch, actor.actor_id) == []
