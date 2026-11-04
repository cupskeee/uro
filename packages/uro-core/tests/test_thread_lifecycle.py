"""D-102: a plot's lifecycle — a signal for every game, and the game's own control. Each thread
counts the beats that brought it into play (`developments`) and the beats it has been live
(`beats_live`), kept by the projector from each beat's recorded `threads` (D-95), so a fork, a
replay and a snapshot agree. A game moves a plot's state itself (`set_thread_state`), and the
world's rules react. (A "bring it toward its end" nudge was measured and not shipped: D-102.)"""

import json

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    beat_resolved,
    thread_created,
    thread_state_changed,
)
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.router import ProviderRouter


def _beat(*threads: str) -> object:
    return beat_resolved(
        beat_id=new_id(), participant_id="p1", intent_text="i", narration="n", threads=list(threads)
    )


async def _counts(store: PostgresEventStore, branch: str) -> dict[str, tuple[int, int, str]]:
    return {
        t.thread_id: (t.developments, t.beats_live, t.state)
        for t in await store.list_threads(branch)
    }


async def test_the_counts_follow_the_beats(store: PostgresEventStore) -> None:
    world = await store.create_world(
        f"lifecycle-{new_id()}",
        extra_events=[
            thread_created(thread_id="t:a", stakes="A", state="active", provenance="emergent"),
            thread_created(thread_id="t:sleep", stakes="Z", state="dormant"),
        ],
    )
    branch = world.main_branch_id
    await store.append_beat(branch, [_beat("t:a")])  # developed, and live a beat
    await store.append_beat(branch, [_beat()])  # live a beat
    # a beat that creates a plot: creation isn't development, and it starts living next beat
    await store.append_beat(
        branch,
        [
            _beat("t:new"),
            thread_created(thread_id="t:new", stakes="N", state="active", provenance="emergent"),
        ],
    )
    # a beat that settles t:a still counts for it
    await store.append_beat(
        branch,
        [
            _beat("t:a", "t:new"),
            thread_state_changed(thread_id="t:a", to_state="resolved", from_state="active"),
        ],
    )
    await store.append_beat(branch, [_beat()])
    counts = await _counts(store, branch)
    assert counts["t:a"] == (2, 4, "resolved")  # developed twice, live 4 beats, then settled
    assert counts["t:new"] == (1, 2, "active")
    assert counts["t:sleep"] == (0, 0, "dormant")  # a dormant plot doesn't age
    # a fork, replayed from history, agrees; so does one restored from a (v2) snapshot
    head = (await store.get_branch(branch)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(world.world_id, head, "replayed")  # type: ignore[arg-type]
    assert await _counts(store, fork.branch_id) == counts
    marker = await store.create_marker(world.world_id, "here", branch)  # snapshots the head
    fork2 = await store.fork_branch(world.world_id, marker.commit_id, "from-snapshot")
    assert await _counts(store, fork2.branch_id) == counts


async def test_an_old_snapshot_is_skipped_not_restored_short(store: PostgresEventStore) -> None:
    world = await store.create_world(
        f"old-snap-{new_id()}",
        extra_events=[thread_created(thread_id="t:a", stakes="A", state="active")],
    )
    branch = world.main_branch_id
    await store.append_beat(branch, [_beat("t:a")])
    marker = await store.create_marker(world.world_id, "m", branch)
    async with store.pool.acquire() as conn:  # make it a pre-D-102 snapshot: v1, no counts
        state = await conn.fetchval(
            "SELECT state FROM snapshots WHERE commit_id = $1", marker.commit_id
        )
        state = state if isinstance(state, dict) else json.loads(state)
        state["v"] = 1
        for row in state["threads"]:
            row.pop("developments"), row.pop("beats_live")
        await conn.execute(
            "UPDATE snapshots SET state = $2 WHERE commit_id = $1", marker.commit_id, state
        )
    fork = await store.fork_branch(world.world_id, marker.commit_id, "after")  # replays instead
    assert (await _counts(store, fork.branch_id))["t:a"] == (1, 1, "active")


_PACK = {
    "rules_api_version": 1,
    "rules": [
        {
            "id": "closing-the-feud-wakes-the-heir",
            "trigger": {
                "event": "ThreadStateChanged",
                "where": {"thread_id": "t:feud", "to_state": "resolved"},
            },
            "then": [{"do": "set_thread_state", "thread": "t:heir", "to": "active"}],
            "scope": {"world": True},
        }
    ],
}


def _engine(store: PostgresEventStore, **kw: object) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=StubProvider()), **kw)  # type: ignore[arg-type]


async def test_a_game_moves_a_plot_and_the_world_reacts(store: PostgresEventStore) -> None:
    world = await store.create_world(
        f"game-sets-{new_id()}",
        rule_pack=_PACK,
        extra_events=[
            thread_created(thread_id="t:feud", stakes="The feud.", state="active"),
            thread_created(thread_id="t:heir", stakes="The heir.", state="dormant"),
        ],
    )
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    engine = _engine(store)
    commit = await engine.set_thread_state(campaign, "t:feud", "resolved")  # an authored plot too
    assert commit.commit_id
    states = {t.thread_id: t.state for t in await store.list_threads(campaign.branch_id)}
    assert states == {"t:feud": "resolved", "t:heir": "active"}  # the world's rule answered
    with pytest.raises(KeyError):
        await engine.set_thread_state(campaign, "t:nope", "active")
    with pytest.raises(ValueError, match="unknown thread state"):
        await engine.set_thread_state(campaign, "t:heir", "finished")
    with pytest.raises(ValueError, match="already"):
        await engine.set_thread_state(campaign, "t:heir", "active")


# --- review fixes (D-100..D-102 phase-end review) ---


async def _played(store: PostgresEventStore) -> tuple[str, str]:
    world = await store.create_world(
        f"rebuild-{new_id()}",
        extra_events=[
            thread_created(thread_id="t:a", stakes="A", state="active", provenance="emergent")
        ],
    )
    for _ in range(3):
        await store.append_beat(world.main_branch_id, [_beat("t:a")])
    return world.world_id, world.main_branch_id


async def test_a_rebuild_counts_a_campaign_played_before_the_counts(
    store: PostgresEventStore,
) -> None:
    world_id, branch = await _played(store)
    # as a branch looks right after migration 027: its rows defaulted to 0, and a snapshot taken
    # from them since would carry the zeros on
    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE proj_threads SET developments = 0, beats_live = 0 WHERE branch_id = $1", branch
        )
    await store.create_marker(world_id, "after-upgrade", branch)
    assert (await _counts(store, branch))["t:a"][:2] == (0, 0)
    done = await store.rebuild_projections(world_id)
    assert done["branches"] == 1 and done["snapshots_dropped"] >= 1
    assert (await _counts(store, branch))["t:a"][:2] == (3, 3)  # its whole past counted
    marker = next(m for m in await store.list_markers(world_id) if m.name == "after-upgrade")
    fork = await store.fork_branch(world_id, marker.commit_id, "after")  # no stale snapshot left
    assert (await _counts(store, fork.branch_id))["t:a"][:2] == (3, 3)


async def test_a_newer_snapshot_replaces_an_older_one_only(store: PostgresEventStore) -> None:
    world_id, branch = await _played(store)
    m = await store.create_marker(world_id, "m1", branch)
    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE snapshots SET state = jsonb_set(state, '{v}', '1'::jsonb) WHERE commit_id = $1",
            m.commit_id,
        )
    await store.create_marker(world_id, "m2", branch)  # same head: re-snapshots it
    async with store.pool.acquire() as conn:
        v = await conn.fetchval(
            "SELECT (state->>'v')::int FROM snapshots WHERE commit_id = $1", m.commit_id
        )
        await conn.execute(  # a current one is never overwritten
            "UPDATE snapshots SET state_hash = 'kept' WHERE commit_id = $1", m.commit_id
        )
    await store.create_marker(world_id, "m3", branch)
    async with store.pool.acquire() as conn:
        kept = await conn.fetchval(
            "SELECT state_hash FROM snapshots WHERE commit_id = $1", m.commit_id
        )
    assert v == 2 and kept == "kept"


async def test_a_branch_diff_ignores_the_counts(store: PostgresEventStore) -> None:
    world_id, branch = await _played(store)
    head = (await store.get_branch(branch)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(world_id, head, "sibling")  # type: ignore[arg-type]
    await store.append_beat(fork.branch_id, [_beat("t:a")])  # one more beat on the fork
    diff = await store.diff_branches(branch, fork.branch_id, ["threads"])
    assert diff == {}  # the same plots; only their counts moved


async def test_a_plot_moves_on_a_world_whose_ruleset_is_gone(store: PostgresEventStore) -> None:
    from uro_server.app import engine_deps

    world = await store.create_world(
        f"lost-ruleset-{new_id()}",
        ruleset_id="uro-basic",
        extra_events=[thread_created(thread_id="t:a", stakes="A", state="active")],
    )
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    async with store.pool.acquire() as conn:  # a plugin ruleset uninstalled after creation
        await conn.execute(
            "UPDATE events SET payload = jsonb_set(payload, '{ruleset_id}', '\"uro-gone\"') "
            "WHERE event_type = 'WorldGenesis' AND commit_id IN "
            "(SELECT commit_id FROM commits WHERE world_id = $1)",
            world.world_id,
        )
    deps = engine_deps(store, _engine(store), {"tok": "p1"})
    assert deps.set_thread_state is not None
    out = await deps.set_thread_state(campaign.campaign_id, "t:a", "resolved")
    assert out["state"] == "resolved"
