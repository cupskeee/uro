"""Encounters played turn by turn (D-112, Uro Loom request 19a): in a campaign that asks for it, a
fight pauses on the player character's turn, each later beat is the player's chosen action, and
the fight is rebuilt by replay each time, so it forks by construction. Deterministic (no LLM)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import start_campaign
from uro_core.domain.events import actor_created
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import ChoiceRefused, TurnChoice
from uro_core.pipeline.engine import BeatResult, Engine
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.uro_basic import UroBasic

_ATTACK = BeatPlan(
    triggers=["violence"], mechanics=[PlanMechanic(affordance="attack", target="a:brute")]
)


class _Narrator:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "Steel rings."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _engine(store: PostgresEventStore) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=_Narrator()), ruleset=UroBasic())


async def _campaign(store: PostgresEventStore, encounters: str = "turns"):  # type: ignore[no-untyped-def]
    world = await store.create_world(f"Turns-{new_id()}")
    c = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Ada",
        spec={"level": 3, "abilities": {"STR": 16}},
        seed=11,
        encounters=encounters,
    )
    await store.append_beat(c.branch_id, [actor_created(actor_id="a:brute", name="Brute")])
    return c


async def _head_events(store: PostgresEventStore, branch: str) -> list[Any]:
    b = await store.get_branch(branch)
    assert b is not None and b.head_commit is not None
    commit = await store.commit_detail(b.world_id, b.head_commit)
    assert commit is not None
    return commit.events


async def _types(store: PostgresEventStore, branch: str) -> list[str]:
    return [e.event_type for e in await _head_events(store, branch)]


async def _play_out(engine: Engine, campaign: Any, first: BeatResult) -> list[BeatResult]:
    """Choose the first legal action, with its first target, until the fight is decided."""
    beats, wait = [first], first.encounter
    for _ in range(60):
        if wait is None:
            return beats
        spec = wait.legal_actions[0]
        choice = TurnChoice(kind=spec.kind, target_id=spec.targets[0] if spec.targets else None)
        beat = await engine.run_beat(campaign, "p1", f"{spec.kind}", action=choice)
        beats.append(beat)
        wait = beat.encounter
    raise AssertionError("the fight never ended")


async def test_a_fight_waits_on_the_player_and_plays_out_turn_by_turn(
    store: PostgresEventStore,
) -> None:
    campaign = await _campaign(store)
    engine = _engine(store)
    opened = await engine.run_beat(campaign, "p1", "I attack the brute", plan=_ATTACK)
    assert opened.mode == "encounter" and opened.encounter is not None
    assert opened.encounter.actor_id == (await store.pc_for_participant(campaign.campaign_id, "p1"))
    assert opened.encounter.legal_actions  # the ruleset's own choices for the PC
    types = await _types(store, campaign.branch_id)
    assert "EncounterStarted" in types and "EncounterEnded" not in types
    assert types.count("ModeChanged") == 1  # in, not out: the fight is still on

    # the GET-able status says the same
    status = await engine.fight_status(campaign)
    assert status is not None and status.encounter_id == opened.encounter.encounter_id

    beats = await _play_out(engine, campaign, opened)
    last = beats[-1]
    assert last.encounter is None and last.mode == "encounter"
    types = await _types(store, campaign.branch_id)
    assert "EncounterEnded" in types and types.count("ModeChanged") == 1  # out
    assert await engine.fight_status(campaign) is None
    # each chosen action is on the record, so the fight replays
    turns = [
        e.payload
        for e in await store.branch_events(
            campaign.branch_id, event_type="EncounterTurnTaken", limit=500
        )
    ]
    assert any(t["chosen"] for t in turns) and any(t["chosen"] is None for t in turns)


async def test_a_character_in_a_fight_acts_only_through_its_actions(
    store: PostgresEventStore,
) -> None:
    campaign = await _campaign(store)
    engine = _engine(store)
    opened = await engine.run_beat(campaign, "p1", "I attack the brute", plan=_ATTACK)
    assert opened.encounter is not None
    with pytest.raises(ChoiceRefused) as err:  # a free-text beat mid-fight
        await engine.run_beat(campaign, "p1", "I look for the exit")
    assert err.value.legal_actions == opened.encounter.legal_actions
    with pytest.raises(ChoiceRefused, match="isn't a legal action"):
        await engine.run_beat(campaign, "p1", "x", action=TurnChoice(kind="cast-a-spell"))
    spec = next(a for a in opened.encounter.legal_actions if a.targets)
    with pytest.raises(ChoiceRefused, match="target"):
        await engine.run_beat(
            campaign, "p1", "x", action=TurnChoice(kind=spec.kind, target_id="a:nobody")
        )
    # nothing was committed by the refusals: the fight still waits on the same choice
    assert (await engine.fight_status(campaign)) == opened.encounter


async def test_an_action_with_no_fight_is_refused_and_auto_campaigns_are_unchanged(
    store: PostgresEventStore,
) -> None:
    auto = await _campaign(store, encounters="auto")
    engine = _engine(store)
    with pytest.raises(ChoiceRefused, match="no fight"):
        await engine.run_beat(auto, "p1", "x", action=TurnChoice(kind="attack"))
    beat = await engine.run_beat(auto, "p1", "I attack the brute", plan=_ATTACK)
    assert beat.encounter is None and beat.mode == "encounter"
    types = await _types(store, auto.branch_id)
    assert "EncounterEnded" in types and types.count("ModeChanged") == 2  # in and out at once
    with pytest.raises(ValueError, match="encounters"):
        await start_campaign(
            store,
            auto.world_id,
            auto.branch_id,
            participant_id="p9",
            new_pc_name="Z",
            encounters="realtime",
        )


async def test_a_fork_taken_mid_fight_carries_it_and_replays_the_same(
    store: PostgresEventStore,
) -> None:
    campaign = await _campaign(store)
    engine = _engine(store)
    opened = await engine.run_beat(campaign, "p1", "I attack the brute", plan=_ATTACK)
    assert opened.encounter is not None
    head = (await store.get_branch(campaign.branch_id)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(campaign.world_id, head, f"mid-{new_id()}")  # type: ignore[arg-type]
    on_fork = await start_campaign(
        store,
        campaign.world_id,
        fork.branch_id,
        participant_id="p1",
        adopt_actor_id=opened.encounter.actor_id,
        encounters="turns",
    )
    assert await engine.fight_status(on_fork) == opened.encounter  # the fight came along
    spec = opened.encounter.legal_actions[0]
    choice = TurnChoice(kind=spec.kind, target_id=spec.targets[0] if spec.targets else None)
    here = await engine.run_beat(campaign, "p1", "go", action=choice)
    there = await engine.run_beat(on_fork, "p1", "go", action=choice)

    async def last_turns(branch: str) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in e.payload.items() if k != "encounter_id"}
            for e in await _head_events(store, branch)
            if e.event_type == "EncounterTurnTaken"
        ]

    # the same choice on the same replayed fight rolls the same dice on both branches
    assert await last_turns(campaign.branch_id) == await last_turns(fork.branch_id)
    assert here.encounter == there.encounter
