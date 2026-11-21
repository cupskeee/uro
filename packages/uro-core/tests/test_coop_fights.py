"""D-114: more than one player character in a fight played turn by turn.

A second player's character enters an open fight by attacking someone in it; the join is an event
in the fight's log, so every later turn replays it in its place (and a fork carries it). The fight
waits on one character at a time and says whose player that is. A character who leaves a fight
that goes on (beaten, or away) is free to play on. Deterministic (no LLM)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import join_campaign, start_campaign
from uro_core.domain.events import actor_created
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import ChoiceRefused, TurnChoice
from uro_core.pipeline.engine import BeatResult, Engine
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets import registry
from uro_core.rulesets.base import (
    Action,
    CharSpec,
    Combatant,
    EncounterCtx,
    join_encounter,
    standing,
)
from uro_core.rulesets.rng import Rng
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


def _engine(store: PostgresEventStore, ruleset: Any = None) -> Engine:
    return Engine(
        store, ProviderRouter(bindings={}, default=_Narrator()), ruleset=ruleset or UroBasic()
    )


async def _party(store: PostgresEventStore) -> tuple[Any, str, str]:
    """A two-player campaign playing fights turn by turn, and a brute who lasts a while."""
    world = await store.create_world(f"Coop-{new_id()}")
    c = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Ada",
        seed=5,
        encounters="turns",
    )
    await join_campaign(store, c, participant_id="p2", new_pc_name="Bo")
    await store.append_beat(
        c.branch_id, [actor_created(actor_id="a:brute", name="Brute", threat="deadly")]
    )
    ada = await store.pc_for_participant(c.campaign_id, "p1")
    bo = await store.pc_for_participant(c.campaign_id, "p2")
    assert ada and bo
    return c, ada, bo


async def _types(store: PostgresEventStore, branch: str) -> list[str]:
    b = await store.get_branch(branch)
    commit = await store.commit_detail(b.world_id, b.head_commit)  # type: ignore[union-attr,arg-type]
    return [e.event_type for e in commit.events]  # type: ignore[union-attr]


async def _act(engine: Engine, c: Any, wait: Any, kind: str | None = None) -> BeatResult:
    """Play the waiting character's turn: `kind` if it is legal, else its first legal action."""
    spec = next((a for a in wait.legal_actions if a.kind == kind), wait.legal_actions[0])
    choice = TurnChoice(kind=spec.kind, target_id=spec.targets[0] if spec.targets else None)
    return await engine.run_beat(c, wait.participant_id, spec.kind, action=choice)


# --- the port hooks, on every built-in ---


@pytest.mark.parametrize("ruleset_id", ["uro-basic", "uro-pbta", "uro-pool"])
def test_every_built_in_takes_a_latecomer_who_then_gets_a_turn(ruleset_id: str) -> None:
    ruleset = registry.resolve(ruleset_id)

    def sheet() -> Any:
        return ruleset.new_character(CharSpec(data={}), Rng(0))

    rng = Rng(3)
    state = ruleset.start_encounter(
        EncounterCtx(
            encounter_id="e",
            combatants=[
                Combatant(actor_id="a:hero", team="aggressor", sheet=sheet(), is_pc=True),
                Combatant(actor_id="a:foe", team="defender", sheet=sheet()),
            ],
        ),
        rng,
    )
    assert standing(ruleset, state) == {"a:hero", "a:foe"}
    late = Combatant(actor_id="a:late", team="aggressor", sheet=sheet(), is_pc=True)
    joined = join_encounter(ruleset, state, late, rng)
    assert joined is not None and standing(ruleset, joined) == {"a:hero", "a:foe", "a:late"}
    assert standing(ruleset, state) == {"a:hero", "a:foe"}  # the state it joined is untouched
    assert ruleset.current_actor(joined) == ruleset.current_actor(state)  # nobody loses a turn
    hold = {"uro-basic": "defend", "uro-pbta": "keep_cool", "uro-pool": "brace"}[ruleset_id]
    acted = []
    for _ in range(3):  # one pass: everyone holds, the latecomer among them
        who = ruleset.current_actor(joined)
        assert who is not None
        acted.append(who)
        joined, _ = ruleset.resolve_action(joined, Action(kind=hold, actor_id=who), rng)
    assert sorted(acted) == ["a:foe", "a:hero", "a:late"]
    # the foe may now be struck by, and strike, the latecomer
    assert "a:late" in {t for spec in ruleset.legal_actions(joined, "a:foe") for t in spec.targets}


# --- the engine ---


async def test_a_second_player_joins_by_attacking_and_the_fight_plays_out(
    store: PostgresEventStore,
) -> None:
    c, ada, bo = await _party(store)
    engine = _engine(store)
    opened = await engine.run_beat(c, "p1", "I attack the brute", plan=_ATTACK)
    assert opened.encounter is not None and opened.encounter.participant_id == "p1"

    joined = await engine.run_beat(c, "p2", "I wade in beside Ada", plan=_ATTACK)
    assert joined.mode == "encounter" and joined.encounter is not None
    assert joined.encounter.encounter_id == opened.encounter.encounter_id  # the same fight
    types = await _types(store, c.branch_id)
    assert "EncounterJoined" in types and "EncounterStarted" not in types
    assert "ModeChanged" not in types  # the table was already in the fight
    assert any("joins the fight" in t for t in joined.check_traces)
    assert (await store.open_encounters(c.branch_id))[0][1][-1]["actor_id"]  # one ordered log

    # both players now see the same fight, and it says whose move it is
    for p in ("p1", "p2"):
        status = await engine.fight_status(c, p)
        assert status == joined.encounter
    seen, wait = set(), joined.encounter
    for _ in range(80):
        if wait is None:
            break
        assert wait.participant_id in ("p1", "p2")
        assert wait.actor_id == {"p1": ada, "p2": bo}[wait.participant_id]
        seen.add(wait.participant_id)
        wait = (await _act(engine, c, wait)).encounter  # every beat replays the join in place
    assert wait is None, "the fight never ended"
    assert seen == {"p1", "p2"}  # each player took turns of their own
    assert await store.open_encounters(c.branch_id) == []
    ended = (await store.branch_events(c.branch_id, event_type="EncounterEnded", limit=1))[0]
    everyone = {*ended.payload["outcome"]["survivors"], *ended.payload["outcome"]["out_of_fight"]}
    assert everyone == {ada, bo, "a:brute"}


async def test_out_of_turn_is_refused_and_says_whose_move_it_is(store: PostgresEventStore) -> None:
    c, _ada, _bo = await _party(store)
    engine = _engine(store)
    await engine.run_beat(c, "p1", "I attack the brute", plan=_ATTACK)
    joined = await engine.run_beat(c, "p2", "I wade in", plan=_ATTACK)
    wait = joined.encounter
    assert wait is not None
    other = "p2" if wait.participant_id == "p1" else "p1"
    with pytest.raises(ChoiceRefused) as plain:  # free text from the one who must wait
        await engine.run_beat(c, other, "I look around")
    assert plain.value.waiting_on == wait.actor_id and plain.value.legal_actions == []
    with pytest.raises(ChoiceRefused) as acted:  # and an action out of turn
        await engine.run_beat(c, other, "x", action=TurnChoice(kind="defend"))
    assert acted.value.waiting_on == wait.actor_id
    with pytest.raises(ChoiceRefused) as mine:  # the one it waits on, without an action
        await engine.run_beat(c, wait.participant_id, "I look around")  # type: ignore[arg-type]
    assert mine.value.waiting_on is None and mine.value.legal_actions == wait.legal_actions
    assert await engine.fight_status(c) == wait  # nothing was committed


async def test_a_fork_taken_after_a_join_carries_the_whole_fight(
    store: PostgresEventStore,
) -> None:
    c, _ada, bo = await _party(store)
    engine = _engine(store)
    await engine.run_beat(c, "p1", "I attack the brute", plan=_ATTACK)
    joined = await engine.run_beat(c, "p2", "I wade in", plan=_ATTACK)
    assert joined.encounter is not None
    head = (await store.get_branch(c.branch_id)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(c.world_id, head, f"mid-{new_id()}")  # type: ignore[arg-type]
    _started, log = (await store.open_encounters(fork.branch_id))[0]
    assert [e["actor_id"] for e in log if "combatant" in e] == [bo]
    on_fork = await start_campaign(
        store,
        c.world_id,
        fork.branch_id,
        participant_id=joined.encounter.participant_id,  # type: ignore[arg-type]
        adopt_actor_id=joined.encounter.actor_id,
        encounters="turns",
    )
    there = await engine.fight_status(on_fork)
    assert there == joined.encounter  # the same turn, the same choices
    here_beat = await _act(engine, c, joined.encounter, "defend")
    there_beat = await _act(engine, on_fork, there, "defend")
    assert here_beat.check_traces == there_beat.check_traces  # the same dice on both branches


async def test_one_who_leaves_a_fight_that_goes_on_plays_on(store: PostgresEventStore) -> None:
    c, ada, bo = await _party(store)
    engine = _engine(store)
    await engine.run_beat(c, "p1", "I attack the brute", plan=_ATTACK)
    wait = (await engine.run_beat(c, "p2", "I wade in", plan=_ATTACK)).encounter
    left = None
    for _ in range(80):  # Ada tries to get away; Bo holds the line
        assert wait is not None, "the fight ended before anyone left it"
        beat = await _act(engine, c, wait, "flee" if wait.participant_id == "p1" else "defend")
        wait = beat.encounter
        gone = [p for p in ("p1", "p2") if await engine.fight_status(c, p) is None]
        if wait is not None and gone:
            left = gone[0]
            break
    assert left is not None and wait is not None and wait.participant_id != left
    pc = {"p1": ada, "p2": bo}[left]
    # the sheet they left with was committed in that beat, and is theirs again
    types = await _types(store, c.branch_id)
    assert "SheetUpdated" in types and "EncounterEnded" not in types
    as_left = await store.get_sheet(c.branch_id, pc)
    after = await engine.run_beat(c, left, "I catch my breath by the wall")
    assert after.mode == "freeroam" and after.encounter is None
    # they can't walk back in, and the fight doesn't wait on them
    again = await engine.run_beat(c, left, "I attack the brute", plan=_ATTACK)
    assert again.mode == "freeroam"
    assert (await engine.fight_status(c)) == wait
    # ending the fight leaves their sheet alone
    assert await engine.abandon_fight(c) is not None
    closing = await store.branch_events(c.branch_id, event_type="SheetUpdated", limit=3)
    b = await store.get_branch(c.branch_id)
    last = await store.commit_detail(b.world_id, b.head_commit)  # type: ignore[union-attr,arg-type]
    assert closing and pc not in {
        e.payload["actor_id"]
        for e in last.events  # type: ignore[union-attr]
        if e.event_type == "SheetUpdated"
    }
    assert await store.get_sheet(c.branch_id, pc) == as_left


async def test_a_ruleset_that_takes_no_latecomers_keeps_the_free_roam_fallback(
    store: PostgresEventStore,
) -> None:
    class NoJoin(UroBasic):
        join_encounter = None  # type: ignore[assignment]

    c, _ada, _bo = await _party(store)
    engine = _engine(store, NoJoin())
    opened = await engine.run_beat(c, "p1", "I attack the brute", plan=_ATTACK)
    beat = await engine.run_beat(c, "p2", "I wade in", plan=_ATTACK)
    assert beat.mode == "freeroam" and beat.encounter is None
    assert "EncounterJoined" not in await _types(store, c.branch_id)
    assert await engine.fight_status(c, "p2") is None  # Bo is not in it
    assert await engine.fight_status(c, "p1") == opened.encounter


async def test_another_campaign_on_the_branch_cant_walk_into_the_fight(
    store: PostgresEventStore,
) -> None:
    c, _ada, _bo = await _party(store)
    engine = _engine(store)
    opened = await engine.run_beat(c, "p1", "I attack the brute", plan=_ATTACK)
    other = await start_campaign(  # same branch, its own campaign, fights in one beat
        store, c.world_id, c.branch_id, participant_id="p9", new_pc_name="Cy"
    )
    beat = await engine.run_beat(other, "p9", "I attack the brute", plan=_ATTACK)
    assert beat.mode == "freeroam" and beat.encounter is None
    assert "EncounterJoined" not in await _types(store, c.branch_id)
    turns = await start_campaign(  # nor one that plays turn by turn but has no one in the fight
        store, c.world_id, c.branch_id, participant_id="p8", new_pc_name="Di", encounters="turns"
    )
    beat = await engine.run_beat(turns, "p8", "I attack the brute", plan=_ATTACK)
    assert beat.mode == "freeroam" and await engine.fight_status(c) == opened.encounter


async def test_one_already_beaten_doesnt_enter_a_fight(store: PostgresEventStore) -> None:
    from uro_core.domain.events import sheet_updated

    c, _ada, bo = await _party(store)
    engine = _engine(store)
    opened = await engine.run_beat(c, "p1", "I attack the brute", plan=_ATTACK)
    down = {**(await store.get_sheet(c.branch_id, bo) or {}), "hp": 0}
    await store.append_beat(
        c.branch_id, [sheet_updated(actor_id=bo, sheet=down, ruleset_id="uro-basic")]
    )
    beat = await engine.run_beat(c, "p2", "I crawl into the fray", plan=_ATTACK)
    assert beat.mode == "freeroam" and await engine.fight_status(c, "p2") is None
    assert await engine.fight_status(c) == opened.encounter
