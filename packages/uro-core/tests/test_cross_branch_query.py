"""B5 — cross-branch query surface (docs/18): query_across + diff_branches. Deterministic."""

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import thread_created, thread_state_changed
from uro_core.domain.ids import new_id


async def _head(store: PostgresEventStore, branch_id: str) -> str:
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT head_commit FROM branches WHERE branch_id = $1", branch_id
        )


async def test_query_across_reads_many_branches_in_one_call(store: PostgresEventStore) -> None:
    world = await store.create_world(f"x-{new_id()}")
    main = world.main_branch_id
    await store.append_beat(main, [thread_created(thread_id="t:war", stakes="war", state="active")])
    # fork; the two lines diverge (the fork resolves the thread by hand)
    fork = await store.fork_branch(world.world_id, await _head(store, main), "sib")
    await store.append_beat(
        fork.branch_id, [thread_state_changed(thread_id="t:war", to_state="resolved")]
    )

    res = await store.query_across([main, fork.branch_id], ["threads"])
    assert res[main]["threads"][0]["state"] == "active"
    assert res[fork.branch_id]["threads"][0]["state"] == "resolved"


async def test_query_across_rejects_an_unknown_section(store: PostgresEventStore) -> None:
    import pytest

    world = await store.create_world(f"x2-{new_id()}")
    with pytest.raises(ValueError, match="unknown section"):
        await store.query_across([world.main_branch_id], ["bogus"])


async def test_diff_branches_reports_added_removed_changed(store: PostgresEventStore) -> None:
    world = await store.create_world(f"d-{new_id()}")
    main = world.main_branch_id
    await store.append_beat(
        main,
        [
            thread_created(thread_id="t:a", stakes="alpha", state="active"),
            thread_created(thread_id="t:b", stakes="beta", state="active"),
        ],
    )
    fork = await store.fork_branch(world.world_id, await _head(store, main), "sib")
    # on the fork: change t:a's state, add t:c; t:b unchanged
    await store.append_beat(
        fork.branch_id,
        [
            thread_state_changed(thread_id="t:a", to_state="resolved"),
            thread_created(thread_id="t:c", stakes="gamma", state="active"),
        ],
    )
    diff = (await store.diff_branches(main, fork.branch_id, ["threads"]))["threads"]
    assert [r["thread_id"] for r in diff["added"]] == ["t:c"]
    assert [r["thread_id"] for r in diff["changed"]] == ["t:a"]
    assert diff["removed"] == []
    # identical branches → no diff for that section
    assert "threads" not in await store.diff_branches(main, main, ["threads"])
