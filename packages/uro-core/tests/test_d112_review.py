"""The D-112 phase-end review's fixes, pinned: one open fight never hides another, a fight turn
commits once, a fork follows its latest story's game, fighters rest after the fight, only the
acting player's own character goes into a fight, an adopted character keeps its name, hidden values
stay out of player reads, and rules that could never fire are refused."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import (
    CharacterFrozen,
    _strict_expressible,
    join_campaign,
    rebuild_character,
    start_campaign,
)
from uro_core.domain.events import actor_created, beat_resolved
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import TurnChoice
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.extraction import ProposedActor
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import spec_schema_of
from uro_core.rulesets.registry import resolve
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.worldpack.rules import Rule
from uro_server.app import _redact_for_player, create_app, engine_deps


class _Narrator:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "Steel rings."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _engine(store: PostgresEventStore) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=_Narrator()), ruleset=UroBasic())


def _attack(target: str, actor: str = "") -> BeatPlan:
    return BeatPlan(
        triggers=["violence"],
        mechanics=[PlanMechanic(affordance="attack", target=target, actor=actor)],
    )


async def _party(store: PostgresEventStore):  # type: ignore[no-untyped-def]
    world = await store.create_world(f"Rev-{new_id()}")
    c = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p",
        new_pc_name="Ada",
        spec={"level": 3},
        seed=5,
        encounters="turns",
    )
    q_pc = await join_campaign(store, c, participant_id="q", new_pc_name="Bo", spec={"level": 3})
    await store.append_beat(
        c.branch_id,
        [
            actor_created(actor_id="a:brute", name="Brute"),
            actor_created(actor_id="a:wolf", name="Wolf"),
        ],
    )
    return c, q_pc


async def test_one_open_fight_never_hides_another(store: PostgresEventStore) -> None:
    c, _ = await _party(store)
    engine = _engine(store)
    first = await engine.run_beat(c, "p", "I attack the brute", plan=_attack("a:brute"))
    assert first.encounter is not None
    # q can't pull the brute (already fighting) into a second fight: it plays as free-roam
    pulled = await engine.run_beat(c, "q", "I attack the brute too", plan=_attack("a:brute"))
    assert pulled.encounter is None and pulled.mode == "freeroam"
    # q's own fight with the wolf opens beside p's, and each player sees their own
    own = await engine.run_beat(c, "q", "I attack the wolf", plan=_attack("a:wolf"))
    assert own.encounter is not None
    assert len(await store.open_encounters(c.branch_id)) == 2
    p_wait, q_wait = await engine.fight_status(c, "p"), await engine.fight_status(c, "q")
    assert p_wait is not None and q_wait is not None
    assert p_wait.encounter_id == first.encounter.encounter_id
    assert q_wait.encounter_id == own.encounter.encounter_id


async def test_a_beat_cant_send_another_players_character_into_a_fight(
    store: PostgresEventStore,
) -> None:
    c, q_pc = await _party(store)
    beat = await _engine(store).run_beat(
        c, "p", "Bo, attack the wolf!", plan=_attack("a:wolf", actor=q_pc)
    )
    assert beat.mode == "freeroam" and await store.open_encounters(c.branch_id) == []


async def test_a_fight_turn_commits_only_from_the_turns_it_saw(store: PostgresEventStore) -> None:
    c, _ = await _party(store)
    engine = _engine(store)
    first = await engine.run_beat(c, "p", "I attack the brute", plan=_attack("a:brute"))
    assert first.encounter is not None
    started, turns = (await store.open_encounters(c.branch_id))[0]
    with pytest.raises(ValueError, match="moved on"):  # a turn computed from stale turns
        await store.append_fight_beat(c.branch_id, started["encounter_id"], len(turns) + 1, [])
    spec = first.encounter.legal_actions[0]
    choice = TurnChoice(kind=spec.kind, target_id=spec.targets[0] if spec.targets else None)
    await engine.run_beat(c, "p", "go", action=choice)  # the fresh one commits


async def test_a_fork_follows_the_latest_story_before_it(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Fork-{new_id()}")
    await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="a",
        new_pc_name="A",
        ruleset_id="uro-pool",
    )
    await start_campaign(  # a later story on the same branch plays the world's game
        store, world.world_id, world.main_branch_id, participant_id="b", new_pc_name="B"
    )
    head = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(world.world_id, head, f"f-{new_id()}")  # type: ignore[arg-type]
    assert await store.inherited_ruleset_pin(fork.branch_id) is None


async def test_fighters_rest_when_the_fight_is_over(store: PostgresEventStore) -> None:
    class Healing(UroBasic):  # Uro Basic has no rest of its own; this one heals to full
        def downtime(self, sheet: dict[str, Any], days: int) -> dict[str, Any] | None:
            return {**sheet, "hp": sheet["max_hp"]}

    c, q_pc = await _party(store)
    engine = _engine(store)
    await engine.run_beat(c, "p", "I attack the brute", plan=_attack("a:brute"))
    fighter = await store.pc_for_participant(c.campaign_id, "p")
    from uro_core.domain.events import sheet_updated

    for actor in (fighter, q_pc):
        sheet = await store.get_sheet(c.branch_id, actor)  # type: ignore[arg-type]
        await store.append_beat(
            c.branch_id,
            [sheet_updated(actor_id=actor, sheet={**sheet, "hp": 1}, ruleset_id="uro-basic")],  # type: ignore[arg-type,dict-item]
        )
    rested = Engine(store, ProviderRouter(bindings={}, default=_Narrator()), ruleset=Healing())
    await rested.agenda_tick(c.branch_id, 3, reason="rest")
    assert (await store.get_sheet(c.branch_id, fighter))["hp"] == 1  # type: ignore[arg-type,index]
    q_sheet = await store.get_sheet(c.branch_id, q_pc)
    assert q_sheet is not None and q_sheet["hp"] == q_sheet["max_hp"]  # not fighting: rested


async def test_an_adopted_character_keeps_its_name(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Name-{new_id()}")
    await store.append_beat(
        world.main_branch_id, [actor_created(actor_id="a:king", name="King Bram", tier=3)]
    )
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", adopt_actor_id="a:king"
    )
    with pytest.raises(CharacterFrozen, match="keeps its name"):
        await rebuild_character(store, c, "p", name="Bob")
    view = await rebuild_character(store, c, "p", spec={"level": 2})  # its build may still change
    assert view.name == "King Bram"


async def test_adopting_a_pack_actor_builds_from_its_own_spec(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Seed-{new_id()}")
    await store.append_beat(
        world.main_branch_id,
        [actor_created(actor_id="a:vet", name="Vet", spec={"level": 4}, threat="strong")],
    )
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", adopt_actor_id="a:vet"
    )
    assert (await store.get_sheet(c.branch_id, "a:vet"))["level"] == 4  # type: ignore[index]


async def test_a_player_reads_no_npcs_spec_or_threat(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Hide-{new_id()}")
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="bob", new_pc_name="Bo"
    )
    await store.append_beat(
        c.branch_id, [actor_created(actor_id="a:ogre", name="Ogre", threat="deadly")]
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Narrator()))
    deps = engine_deps(store, engine, {"op": "alice", "pl": "bob"}, admin_tokens={"op"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(deps)), base_url="http://t"
    ) as client:
        path = f"/campaigns/{c.campaign_id}/state?sections=actors"
        mine = (await client.get(f"{path}&token=pl")).json()["state"]["actors"]
        assert all("threat" not in a and "spec" not in a for a in mine)
        ops = (await client.get(f"{path}&token=op")).json()["state"]["actors"]
        assert {a["name"]: a["threat"] for a in ops}["Ogre"] == "deadly"


def test_a_dry_run_shows_a_player_only_their_own_numbers() -> None:
    events: list[dict[str, Any]] = [
        {"event_type": "CheckResolved", "payload": {"actor_id": "a:me", "player_view": {"d": 1}}},
        {"event_type": "CheckResolved", "payload": {"actor_id": "a:npc", "player_view": {"d": 2}}},
        {"event_type": "SheetUpdated", "payload": {"actor_id": "a:npc", "sheet": {"hp": 3}}},
        {"event_type": "SheetUpdated", "payload": {"actor_id": "a:me", "sheet": {"hp": 9}}},
        {"event_type": "EncounterStarted", "payload": {"encounter_id": "e", "combatants": [{}]}},
    ]
    out = _redact_for_player(events, "a:me")
    assert [e["payload"].get("player_view") for e in out[:2]] == [{"d": 1}, None]
    assert [e["payload"]["actor_id"] for e in out if e["event_type"] == "SheetUpdated"] == ["a:me"]
    assert "combatants" not in out[-1]["payload"]


def test_a_non_string_threat_drops_the_word_not_the_person() -> None:
    assert ProposedActor.model_validate({"name": "Pim", "threat": 7}).threat == ""


@pytest.mark.parametrize(
    "trigger",
    [
        {"event": "CheckResolved", "where": {"player_view": "x"}},
        {"event": "EncounterTurnTaken", "where": {"chosen": "x"}},
        {"event": "ActorRenamed"},
    ],
)
def test_rules_that_could_never_fire_are_refused(trigger: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="never"):
        Rule.model_validate(
            {
                "id": "r",
                "trigger": trigger,
                "then": [{"do": "adjust_counter", "scope_ref": "f:h", "key": "k", "delta": 1}],
                "scope": {"world": True},
            }
        )


def test_the_built_in_specs_survive_strict_structured_outputs() -> None:
    for rid in ("uro-basic", "uro-pbta", "uro-pool"):
        assert _strict_expressible(spec_schema_of(resolve(rid)))
    open_map = {
        "type": "object",
        "properties": {"m": {"type": "object", "additionalProperties": {"type": "integer"}}},
    }
    assert not _strict_expressible(open_map)


async def test_a_story_beat_freezes_after_the_first_turn(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Frz-{new_id()}")
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", new_pc_name="Ada"
    )
    await store.append_beat(
        c.branch_id,
        [beat_resolved(beat_id=new_id(), participant_id="p", intent_text="go", narration=".")],
    )
    with pytest.raises(CharacterFrozen):
        await rebuild_character(store, c, "p", name="Ade")
