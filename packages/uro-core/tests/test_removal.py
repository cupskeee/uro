"""D-99: removing a world, a branch or a campaign — a privacy wipe, for good. A world takes
everything it holds with it (campaigns, history, projections, memories, tokens, its players' notes,
its metered model calls) and nothing of another world; a branch takes only what it alone reaches;
`main` and shared branches are refused for a campaign."""

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, beat_resolved
from uro_core.domain.ids import new_id
from uro_core.metering import LLMCall
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.timeline.removal import delete_campaign


async def _beat(store: PostgresEventStore, branch: str, text: str) -> str:
    commit = await store.append_beat(
        branch,
        [beat_resolved(beat_id=new_id(), participant_id="p1", intent_text=text, narration=text)],
    )
    await store.add_memory(
        branch_id=branch,
        commit_id=commit.commit_id,
        kind="beat",
        text=text,
        vector=hashing_embedding(text),
        entity_refs=[],
    )
    await store.record_llm_call(
        LLMCall(
            stage_tag="narrator", prompt_hash="h", latency_ms=1, branch_id=branch, response=text
        )
    )
    return commit.commit_id


async def _counts(store: PostgresEventStore, world_id: str, branches: list[str]) -> dict[str, int]:
    """Rows left for a world, looked up by its id and by its branch ids (taken beforehand, so a
    removed `branches` row can't hide rows keyed on them)."""
    by_world = {
        "commits": "SELECT count(*) FROM commits WHERE world_id = $1",
        "branches": "SELECT count(*) FROM branches WHERE world_id = $1",
        "campaigns": "SELECT count(*) FROM campaigns WHERE world_id = $1",
        "notes": "SELECT count(*) FROM participant_notes WHERE world_ref = $1",
        "outbox": "SELECT count(*) FROM outbox WHERE world_id = $1",
    }
    by_branch = {
        "memories": "SELECT count(*) FROM memory_index WHERE branch_id = ANY($1::text[])",
        "actors": "SELECT count(*) FROM proj_actors WHERE branch_id = ANY($1::text[])",
        "pcs": "SELECT count(*) FROM proj_pcs WHERE branch_id = ANY($1::text[])",
        "llm_calls": "SELECT count(*) FROM llm_calls WHERE branch_id = ANY($1::text[])",
    }
    async with store.pool.acquire() as conn:
        out = {k: await conn.fetchval(v, world_id) for k, v in by_world.items()}
        out |= {k: await conn.fetchval(v, branches) for k, v in by_branch.items()}
    return out


async def _branches(store: PostgresEventStore, world_id: str) -> list[str]:
    async with store.pool.acquire() as conn:
        rows = await conn.fetch("SELECT branch_id FROM branches WHERE world_id = $1", world_id)
    return [r["branch_id"] for r in rows]


async def test_a_world_is_removed_whole_and_another_is_untouched(store: PostgresEventStore) -> None:
    doomed = await store.create_world(
        f"doomed-{new_id()}", extra_events=[actor_created(actor_id="a:x", name="X")]
    )
    kept = await store.create_world(f"kept-{new_id()}")
    c = await store.start_campaign(
        doomed.world_id, doomed.main_branch_id, participant_id="p1", new_pc_name="Ash"
    )
    await store.mint_token(f"hash-{new_id()}", "p1", c.campaign_id)
    await store.participant_remember("p1", doomed.world_id, "the code is 4471")
    await _beat(store, doomed.main_branch_id, "the same words in both worlds")
    await _beat(store, kept.main_branch_id, "the same words in both worlds")  # a shared embedding
    head = (await store.get_branch(doomed.main_branch_id)).head_commit  # type: ignore[union-attr]
    await store.fork_branch(doomed.world_id, head, "what-if")  # type: ignore[arg-type]
    doomed_branches = await _branches(store, doomed.world_id)
    kept_branches = await _branches(store, kept.world_id)
    assert all(v > 0 for k, v in (await _counts(store, doomed.world_id, doomed_branches)).items())
    before_kept = await _counts(store, kept.world_id, kept_branches)

    removed = await store.delete_world(doomed.world_id)

    assert removed["worlds"] == 1 and removed["campaigns"] == 1 and removed["branches"] == 2
    assert removed["session_tokens"] == 1 and removed["participant_notes"] == 1
    assert removed["llm_calls"] == 1 and removed["memories"] == 2  # main + the fork's copy
    assert await store.get_world(doomed.world_id) is None
    assert all(v == 0 for v in (await _counts(store, doomed.world_id, doomed_branches)).values())
    assert await _counts(store, kept.world_id, kept_branches) == before_kept  # untouched
    hits = await store.search(kept.main_branch_id, hashing_embedding("the same words"), 3)
    assert hits and hits[0].text == "the same words in both worlds"  # its embedding survived
    with pytest.raises(KeyError):
        await store.delete_world(doomed.world_id)


async def test_a_branch_takes_only_what_it_alone_reaches(store: PostgresEventStore) -> None:
    world = await store.create_world(f"branchy-{new_id()}")
    main = world.main_branch_id
    shared = await _beat(store, main, "before the fork")
    fork = await store.fork_branch(world.world_id, shared, "what-if")
    mine = await _beat(store, fork.branch_id, "only on the fork")
    child = await store.fork_branch(world.world_id, mine, "what-if-2")  # forks from the fork
    await _beat(store, child.branch_id, "only on the child")
    later = await _beat(store, fork.branch_id, "the fork goes on")
    c = await store.start_campaign(
        world.world_id, fork.branch_id, participant_id="p1", new_pc_name="Ash"
    )
    main_beats = await store.recent_beats(main, 10)

    removed = await store.delete_branch(fork.branch_id)

    assert removed["branches"] == 1 and removed["campaigns"] == 1
    assert removed["commits"] >= 1  # the fork's own later commits
    assert (
        await store.get_branch(fork.branch_id) is None
        and await store.get_campaign(c.campaign_id) is None
    )
    async with store.pool.acquire() as conn:
        alive = {
            r["commit_id"]
            for r in await conn.fetch(
                "SELECT commit_id FROM commits WHERE world_id = $1", world.world_id
            )
        }
    assert shared in alive and mine in alive  # main's history, and what the child still reaches
    assert later not in alive  # only the deleted fork reached it
    assert await store.recent_beats(main, 10) == main_beats
    assert [b.intent_text for b in await store.recent_beats(child.branch_id, 10)][
        -1
    ] == "only on the child"
    with pytest.raises(ValueError, match="main"):
        await store.delete_branch(main)


async def test_a_campaign_is_removed_with_its_own_branch_only(store: PostgresEventStore) -> None:
    world = await store.create_world(f"camp-{new_id()}")
    on_main = await store.start_campaign(
        world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="A"
    )
    with pytest.raises(ValueError, match="main branch"):
        await delete_campaign(store, on_main.campaign_id)
    head = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(world.world_id, head, "solo")  # type: ignore[arg-type]
    solo = await store.start_campaign(
        world.world_id, fork.branch_id, participant_id="p2", new_pc_name="B"
    )
    duo = await store.start_campaign(
        world.world_id, fork.branch_id, participant_id="p3", new_pc_name="C"
    )
    with pytest.raises(ValueError, match="shared"):
        await delete_campaign(store, solo.campaign_id)
    await store.delete_branch(fork.branch_id)  # the branch way removes both
    assert await store.get_campaign(duo.campaign_id) is None
    fork2 = await store.fork_branch(world.world_id, head, "solo-2")  # type: ignore[arg-type]
    alone = await store.start_campaign(
        world.world_id, fork2.branch_id, participant_id="p2", new_pc_name="B"
    )
    removed = await delete_campaign(store, alone.campaign_id)
    assert removed["campaigns"] == 1 and await store.get_branch(fork2.branch_id) is None
    assert await store.get_campaign(on_main.campaign_id) is not None  # main's campaign stays
    with pytest.raises(KeyError):
        await delete_campaign(store, alone.campaign_id)
