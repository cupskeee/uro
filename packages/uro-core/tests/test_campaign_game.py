"""A campaign's game and its players' characters (D-112, Uro Loom requests 11, 12, 13 and 17):
a campaign pins its own ruleset and settings, a fork carries the story's pin, a PC is built from a
spec (or refused with the ruleset's problems), a build can be previewed, changed until the story
starts, and read back by its own player."""

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import (
    CharacterFrozen,
    join_campaign,
    own_character,
    preview_character,
    rebuild_character,
    start_campaign,
)
from uro_core.domain.events import beat_resolved
from uro_core.domain.ids import new_id
from uro_core.rulesets import registry
from uro_core.rulesets.base import CharSpecError


async def _world(store: PostgresEventStore, **kw):  # type: ignore[no-untyped-def]
    return await store.create_world(f"Game-{new_id()}", **kw)


async def _beat(store: PostgresEventStore, branch_id: str) -> None:
    await store.append_beat(
        branch_id,
        [beat_resolved(beat_id=new_id(), participant_id="p", intent_text="go", narration="...")],
    )


async def test_a_campaign_plays_the_game_it_names(store: PostgresEventStore) -> None:
    world = await _world(store)  # a uro-basic world
    c = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p",
        new_pc_name="Ash",
        ruleset_id="uro-pool",
        ruleset_config={"trait_cap": 2},
    )
    again = await store.get_campaign(c.campaign_id)
    assert again is not None and again.ruleset_id == "uro-pool"
    assert again.ruleset_config == {"trait_cap": 2}
    played = await registry.resolve_for_campaign(store, again)
    assert played.id == "uro-pool" and played.config.trait_cap == 2  # type: ignore[attr-defined]
    sheet = await store.get_sheet(world.main_branch_id, await _pc(store, c.campaign_id))
    assert sheet is not None and "traits" in sheet  # the PC was built under the campaign's game


async def test_named_settings_alone_keep_the_worlds_ruleset(store: PostgresEventStore) -> None:
    world = await _world(store, ruleset_id="uro-pbta")
    c = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p",
        new_pc_name="Ash",
        ruleset_config={"xp_per_advance": 2},
    )
    assert (c.ruleset_id, c.ruleset_config) == ("uro-pbta", {"xp_per_advance": 2})


async def test_settings_the_ruleset_refuses_start_nothing(store: PostgresEventStore) -> None:
    world = await _world(store)
    with pytest.raises(ValueError, match="uro-pool"):
        await start_campaign(
            store,
            world.world_id,
            world.main_branch_id,
            participant_id="p",
            new_pc_name="Ash",
            ruleset_id="uro-pool",
            ruleset_config={"nope": 1},
        )
    with pytest.raises(KeyError):
        await start_campaign(
            store,
            world.world_id,
            world.main_branch_id,
            participant_id="p",
            new_pc_name="Ash",
            ruleset_id="no-such-game",
        )
    assert await store.list_campaigns(world.world_id) == []


async def test_a_fork_carries_the_pin_of_the_story_it_came_from(store: PostgresEventStore) -> None:
    world = await _world(store)
    story = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p",
        new_pc_name="Ash",
        ruleset_id="uro-pool",
        ruleset_config={"push_strain": 3},
    )
    await _beat(store, world.main_branch_id)
    head = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
    what_if = await store.fork_branch(world.world_id, head, f"what-if-{new_id()}")  # type: ignore[arg-type]
    # a campaign begun on the What if, naming nothing, plays the story's game
    on_fork = await start_campaign(
        store, world.world_id, what_if.branch_id, participant_id="q", new_pc_name="Bryn"
    )
    assert (on_fork.ruleset_id, on_fork.ruleset_config) == ("uro-pool", {"push_strain": 3})
    # ...and so does a fork of that fork, through the inherited pin
    head2 = (await store.get_branch(what_if.branch_id)).head_commit  # type: ignore[union-attr]
    deeper = await store.fork_branch(world.world_id, head2, f"deeper-{new_id()}")  # type: ignore[arg-type]
    pin = await store.inherited_ruleset_pin(deeper.branch_id)
    assert pin is not None and (pin.id, pin.config) == ("uro-pool", {"push_strain": 3})
    # on the root branch, a campaign naming nothing still plays the world's, as before
    other = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="r", new_pc_name="Cael"
    )
    assert (other.ruleset_id, other.ruleset_config) == ("uro-basic", None)
    assert story.ruleset_id == "uro-pool"


async def test_a_fork_of_a_story_on_its_worlds_game_follows_the_world(
    store: PostgresEventStore,
) -> None:
    world = await _world(store, ruleset_id="uro-pbta")
    await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", new_pc_name="Ash"
    )
    head = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(world.world_id, head, f"f-{new_id()}")  # type: ignore[arg-type]
    assert await store.inherited_ruleset_pin(fork.branch_id) is None


async def _pc(store: PostgresEventStore, campaign_id: str, participant: str = "p") -> str:
    actor = await store.pc_for_participant(campaign_id, participant)
    assert actor is not None
    return actor


async def test_a_pc_is_built_from_its_spec_or_refused(store: PostgresEventStore) -> None:
    world = await _world(store)
    c = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p",
        new_pc_name="Ash",
        spec={"abilities": {"STR": 16}, "weapon_tier": 2},
    )
    sheet = await store.get_sheet(world.main_branch_id, await _pc(store, c.campaign_id))
    assert sheet is not None and sheet["abilities"]["STR"] == 16 and sheet["weapon_tier"] == 2
    with pytest.raises(CharSpecError) as err:
        await start_campaign(
            store,
            world.world_id,
            world.main_branch_id,
            participant_id="q",
            new_pc_name="Bryn",
            spec={"abilities": {"STR": 40}},
        )
    assert err.value.problems[0].loc == ["abilities", "STR"]
    # a join is built the same way, under the campaign's game
    actor = await join_campaign(store, c, participant_id="q", new_pc_name="Bryn", spec={"level": 3})
    assert (await store.get_sheet(world.main_branch_id, actor))["level"] == 3  # type: ignore[index]
    with pytest.raises(CharSpecError):
        await join_campaign(store, c, participant_id="r", new_pc_name="Cael", spec={"mana": 2})


async def test_an_adopted_sheeted_actor_takes_no_spec(store: PostgresEventStore) -> None:
    world = await _world(store)
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", new_pc_name="Ash"
    )
    ash = await _pc(store, c.campaign_id)
    await store.end_campaign(c.campaign_id, f"end-{new_id()}")
    with pytest.raises(CharSpecError, match="adopted"):
        await start_campaign(
            store,
            world.world_id,
            world.main_branch_id,
            participant_id="p",
            adopt_actor_id=ash,
            spec={"level": 2},
        )


async def test_a_preview_builds_and_writes_nothing(store: PostgresEventStore) -> None:
    world = await _world(store)
    before = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
    ok = await preview_character(
        store, branch_id=world.main_branch_id, spec={"traits": {"might": 3}}, ruleset_id="uro-pool"
    )
    assert ok.ok and ok.ruleset == "uro-pool" and ok.sheet["traits"]["might"] == 3  # type: ignore[index]
    bad = await preview_character(
        store,
        branch_id=world.main_branch_id,
        spec={"traits": {"might": 3}},
        ruleset_id="uro-pool",
        ruleset_config={"trait_cap": 2},
    )
    assert not bad.ok and bad.problems[0].loc == ["traits", "might"] and bad.sheet is None
    after = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
    assert before == after


async def test_a_build_can_change_until_the_story_starts(store: PostgresEventStore) -> None:
    world = await _world(store)
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", new_pc_name="Ash"
    )
    view = await own_character(store, c, "p")
    assert view.build_open and view.name == "Ash" and view.sheet["level"] == 1  # type: ignore[index]
    view = await rebuild_character(store, c, "p", spec={"level": 2}, name="Ashe")
    assert view.name == "Ashe" and view.sheet["level"] == 2 and view.build_open  # type: ignore[index]
    with pytest.raises(CharSpecError):  # still the ruleset's validation
        await rebuild_character(store, c, "p", spec={"level": 99})
    await _beat(store, world.main_branch_id)
    with pytest.raises(CharacterFrozen, match="story has started"):
        await rebuild_character(store, c, "p", spec={"level": 3})
    with pytest.raises(CharacterFrozen):
        await rebuild_character(store, c, "p", name="Ashen")
    view = await own_character(store, c, "p")
    assert not view.build_open and view.name == "Ashe" and view.sheet["level"] == 2  # type: ignore[index]
    # a player who joins later has their own window, from their own binding
    await join_campaign(store, c, participant_id="q", new_pc_name="Bryn")
    assert (await rebuild_character(store, c, "q", spec={"level": 2})).sheet["level"] == 2  # type: ignore[index]


async def test_a_participant_reads_their_own_character_and_holdings(
    store: PostgresEventStore,
) -> None:
    world = await store.create_world(f"Gear-{new_id()}", starting_gear=["lantern", "rope"])
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", new_pc_name="Ash"
    )
    view = await own_character(store, c, "p")
    assert sorted(h["name"] for h in view.holdings) == ["lantern", "rope"]
    assert all(h["data"] == {} for h in view.holdings)
    with pytest.raises(KeyError):
        await own_character(store, c, "nobody")
