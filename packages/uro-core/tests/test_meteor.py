"""Phase 2 acceptance: the meteor test (docs/03, 10) — the reason this engine exists.

Fully deterministic (no LLM). One played campaign ends in a city-destroying event; from
the SAME event log we get, with no special-case code:
  (a) continue as the same character (adopt the PC who caused it),
  (b) a new life in the aftermath whose NPCs can retell campaign A's deeds as history,
  (c) a what-if forked from *before* the strike, coexisting with the others.
Asserted on committed events/projections, never prose.

Also covers the 2.2 substrate the test rides on: PC binding/release and deterministic
time-skip.
"""

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    CausedBy,
    actor_created,
    claim_recorded,
    place_created,
    place_destroyed,
    terrain_changed,
)
from uro_core.domain.ids import new_id
from uro_core.timeline.models import Campaign, World

PLAYER = "player-1"


async def _play_campaign_a(store: PostgresEventStore) -> tuple[World, Campaign]:
    """Play Campaign A to its meteor ending. Returns (world, campaign_a).

    Timeline on `main`: seed Vel + its Duke → start the campaign (wizard PC) → the party
    uncovers the Saltborn plot → [marker pre-meteor] → the wizard calls down the star that
    destroys Vel (History emits it as a thread consequence, caused_by=player_action) →
    end the campaign (wizard retires to NPC, marker campaign-a-end).
    """
    world = await store.create_world(f"Ashfall-{new_id()}")
    main = world.main_branch_id
    await store.append_beat(
        main,
        [
            place_created(
                place_id="p:vel", name="Vel", kind="settlement", description="a salt-port"
            ),
            actor_created(actor_id="a:duke", name="Duke Halbrecht", tier=2, role="ruler"),
        ],
    )
    camp_a = await store.start_campaign(
        world.world_id,
        main,
        participant_id=PLAYER,
        new_pc_name="Wizard Ysolde",
        new_pc_id="a:wizard",
    )
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:mera", name="Mera", tier=2, role="innkeeper"),
            claim_recorded(
                claim_id="c:saltborn",
                statement="The Saltborn cult festers beneath Vel.",
                subject_refs=["p:vel"],
                truth="true",
                origin="narration",
            ),
        ],
    )
    await store.create_marker(world.world_id, "pre-meteor", main)
    # THE METEOR — a player-caused cataclysm, committed by History as a thread consequence
    # (docs/03/12): TerrainChanged + PlaceDestroyed + the deed recorded as truth.
    cause = CausedBy(kind="player_action", participant_id=PLAYER)
    await store.append_beat(
        main,
        [
            terrain_changed(
                place_id="p:vel",
                description="a glassed crater where the salt-port stood",
                effects=["firestorm"],
                caused_by=cause,
            ),
            place_destroyed(
                place_id="p:vel", cause="a falling star the wizard called down", caused_by=cause
            ),
            claim_recorded(
                claim_id="c:meteor",
                statement="Wizard Ysolde called down the star that destroyed Vel.",
                subject_refs=["a:wizard", "p:vel"],
                truth="true",
                origin="narration",
                caused_by=cause,
            ),
        ],
    )
    await store.end_campaign(camp_a.campaign_id, "campaign-a-end", outcome="Vel destroyed")
    return world, camp_a


async def test_the_meteor_test(store: PostgresEventStore) -> None:
    world, _camp_a = await _play_campaign_a(store)
    main = world.main_branch_id

    # --- Campaign A's end state on main: Vel is a crater; the wizard has retired to NPC ---
    assert (await store.get_place(main, "p:vel")).status == "destroyed"
    assert (await store.get_claim(main, "c:meteor")).truth == "true"
    assert await store.get_actor(main, "a:wizard") is not None  # still a world actor
    assert await store.is_pc(main, "a:wizard") is False  # released — no longer a PC

    # --- (a) CONTINUE: same player adopts their old PC, faces what they caused ---
    cont = await store.fork_branch(world.world_id, "campaign-a-end", "continue")
    assert await store.is_pc(cont.branch_id, "a:wizard") is False  # NPC until re-bound
    camp_b = await store.start_campaign(
        world.world_id, cont.branch_id, participant_id=PLAYER, adopt_actor_id="a:wizard"
    )
    assert camp_b.campaign_id is not None
    assert await store.is_pc(cont.branch_id, "a:wizard") is True  # a PC again, on this branch
    assert (await store.get_place(cont.branch_id, "p:vel")).status == "destroyed"
    assert (await store.get_claim(cont.branch_id, "c:meteor")).truth == "true"

    # --- (b) NEW LIFE: a fresh farmer PC a year later; NPCs retell A's deeds as history ---
    farm = await store.fork_branch(world.world_id, "campaign-a-end", "aftermath")
    await store.time_skip(farm.branch_id, 365)
    await store.start_campaign(
        world.world_id,
        farm.branch_id,
        participant_id="player-2",
        new_pc_name="Bram the farmer",
        new_pc_id="a:farmer",
    )
    assert (
        await store.get_place(farm.branch_id, "p:vel")
    ).status == "destroyed"  # the crater is real
    assert (await store.get_claim(farm.branch_id, "c:meteor")).truth == "true"  # retellable history
    assert await store.is_pc(farm.branch_id, "a:farmer") is True
    assert (
        await store.is_pc(farm.branch_id, "a:wizard") is False
    )  # the wizard is just a legend here
    assert await store.current_world_time(farm.branch_id) == 365  # a year on

    # --- (c) WHAT-IF: forked BEFORE the decision; the meteor never happened here ---
    whatif = await store.fork_branch(world.world_id, "pre-meteor", "whatif")
    assert (await store.get_place(whatif.branch_id, "p:vel")).status == "active"  # Vel still stands
    assert (
        await store.get_claim(whatif.branch_id, "c:meteor") is None
    )  # no such deed on this branch
    assert (
        await store.get_claim(whatif.branch_id, "c:saltborn") is not None
    )  # but pre-fork history carried
    # pre-meteor is MID-campaign-A (before end/release), so the wizard is still an ACTIVE PC
    # here. This is the only assertion that exercises an active binding SURVIVING a fork — it
    # exercises the snapshot 'pcs' section + materialization, the exact path the signature
    # "continue as the one who caused it" divergence rides on (review inc 2.2).
    assert await store.is_pc(whatif.branch_id, "a:wizard") is True
    assert set(await store.active_pcs(whatif.branch_id)) == {"a:wizard"}

    # --- Cross-branch invariants: same actor_id, diverged PC-ness; no contamination ---
    assert (await store.get_actor(cont.branch_id, "a:wizard")).actor_id == "a:wizard"
    assert (await store.get_actor(farm.branch_id, "a:wizard")).actor_id == "a:wizard"  # same entity
    # sibling isolation: playing on what-if never touches the continue branch.
    await store.append_beat(whatif.branch_id, [place_created(place_id="p:newtown", name="Newtown")])
    assert await store.get_place(cont.branch_id, "p:newtown") is None
    assert (await store.get_place(cont.branch_id, "p:vel")).status == "destroyed"  # still a crater

    names = {b.name for b in await store.list_branches(world.world_id)}
    assert names == {"main", "continue", "aftermath", "whatif"}


# --- PC binding & release (the 2.2 substrate under the meteor) ---


async def test_pc_binding_adopt_and_release(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    await store.append_beat(main, [actor_created(actor_id="a:hero", name="Hero", tier=2)])

    # fresh PC path
    camp = await store.start_campaign(
        world.world_id, main, participant_id=PLAYER, new_pc_name="Fresh", new_pc_id="a:fresh"
    )
    assert await store.is_pc(main, "a:fresh") is True
    assert set(await store.active_pcs(main)) == {"a:fresh"}

    # end releases the PC → reverts to NPC, but the actor persists
    await store.end_campaign(camp.campaign_id, "the-end")
    assert await store.is_pc(main, "a:fresh") is False
    assert await store.active_pcs(main) == []
    assert await store.get_actor(main, "a:fresh") is not None


async def test_start_campaign_validation(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    with pytest.raises(ValueError):  # neither adopt nor new
        await store.start_campaign(world.world_id, main, participant_id=PLAYER)
    with pytest.raises(ValueError):  # both
        await store.start_campaign(
            world.world_id, main, participant_id=PLAYER, adopt_actor_id="a:x", new_pc_name="Y"
        )
    with pytest.raises(ValueError):  # adopt an actor that doesn't exist on the branch
        await store.start_campaign(
            world.world_id, main, participant_id=PLAYER, adopt_actor_id="a:ghost"
        )


# --- deterministic time-skip ---


async def test_time_skip_advances_and_records(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    assert await store.current_world_time(main) == 0

    commit = await store.time_skip(main, 400, reason="a fallow winter")
    assert await store.current_world_time(main) == 400

    # the skip is recorded as TimeAdvanced + AdaptationApplied on one commit (no BeatResolved).
    async with store.pool.acquire() as conn:
        types = [
            r["event_type"]
            for r in await conn.fetch(
                "SELECT event_type FROM events WHERE commit_id = $1 ORDER BY seq", commit.commit_id
            )
        ]
        ta = await conn.fetchrow(
            "SELECT payload, caused_by FROM events "
            "WHERE commit_id = $1 AND event_type = 'TimeAdvanced'",
            commit.commit_id,
        )
    assert types == ["TimeAdvanced", "AdaptationApplied"]
    assert ta["payload"]["from_day"] == 0 and ta["payload"]["to_day"] == 400
    assert ta["caused_by"]["kind"] == "history" and ta["caused_by"]["pass"] == "timeskip"

    # skips stack: a second one advances from the current day, not from 0.
    await store.time_skip(main, 100)
    assert await store.current_world_time(main) == 500

    with pytest.raises(ValueError):
        await store.time_skip(main, 0)  # non-positive rejected


async def test_time_skip_is_carried_and_isolated_on_fork(store: PostgresEventStore) -> None:
    # A skip on one fork advances only that fork's clock; a sibling stays put.
    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    head = await store.append_beat(main, [actor_created(actor_id="a:x", name="X")])
    a = await store.fork_branch(world.world_id, head.commit_id, "a")
    b = await store.fork_branch(world.world_id, head.commit_id, "b")
    await store.time_skip(a.branch_id, 100)
    assert await store.current_world_time(a.branch_id) == 100
    assert await store.current_world_time(b.branch_id) == 0  # sibling untouched
    assert await store.current_world_time(main) == 0
