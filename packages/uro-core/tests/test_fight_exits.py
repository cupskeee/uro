"""D-114: a way out of a fight, actions that do something, and an NPC's first check.

Every built-in lets a combatant leave (flee / get clear / withdraw) and gives its holding action an
effect; one who got away is neither wounded nor looted; the game can close a fight undecided; and a
known NPC without a sheet rolls its check on a first sheet built from the hint it carries."""

from collections.abc import AsyncIterator
from typing import Any

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import start_campaign
from uro_core.domain.events import actor_created, item_created
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import TurnChoice
from uro_core.pipeline.engine import Engine
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
    EncounterOutcome,
)
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic


class _Narrator:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "Steel rings."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _pair(ruleset: Any, a: dict[str, Any] | None = None, b: dict[str, Any] | None = None) -> Any:
    """A two-sided fight under `ruleset`, each side a default character (or the given spec)."""
    sheets = [ruleset.new_character(CharSpec(data=s or {}), Rng(0)) for s in (a, b)]
    return ruleset.start_encounter(
        EncounterCtx(
            encounter_id="e",
            combatants=[
                Combatant(actor_id="a:hero", team="aggressor", sheet=sheets[0], is_pc=True),
                Combatant(actor_id="a:foe", team="defender", sheet=sheets[1]),
            ],
        ),
        Rng(1),
    )


def _until(ruleset: Any, state: Any, actor: str, kind: str, want: str, tries: int = 400) -> Any:
    """Play `kind` for `actor` over many seeds until an effect of kind `want` shows; the other
    side only holds (its own no-harm action), so nothing else changes the fight."""
    hold = {"uro-basic": "defend", "uro-pbta": "keep_cool", "uro-pool": "brace"}[ruleset.id]
    for seed in range(tries):
        st, rng = state, Rng(seed)
        for _ in range(4):
            who = ruleset.current_actor(st)
            if who is None:
                break
            act = Action(kind=kind if who == actor else hold, actor_id=who)
            st, effects = ruleset.resolve_action(st, act, rng)
            if any(e.kind == want and e.actor_id == actor for e in effects):
                return st
    raise AssertionError(f"{kind} never produced {want}")


def test_every_built_in_lets_a_combatant_leave() -> None:
    for rid, leave in (("uro-basic", "flee"), ("uro-pbta", "get_clear"), ("uro-pool", "withdraw")):
        ruleset = registry.resolve(rid)
        state = _pair(ruleset)
        assert leave in {a.kind for a in ruleset.legal_actions(state, "a:hero")}
        after = _until(ruleset, state, "a:hero", leave, "fled")
        outcome = ruleset.is_over(after)
        assert outcome is not None, rid
        assert outcome.fled == ["a:hero"] and outcome.winner_team == "defender"
        assert "a:hero" not in outcome.out_of_fight and "a:hero" not in outcome.survivors
        # ...and leaving can fail: the same action, another roll, leaves them in the fight
        _until(ruleset, state, "a:hero", leave, "caught")


def test_uro_basic_defend_makes_you_harder_to_hit_until_your_next_turn() -> None:
    ruleset = UroBasic()
    state = _pair(ruleset)
    hit_ac = set()
    for guarded in (False, True):
        st = state
        first = ruleset.current_actor(st)
        other = "a:foe" if first == "a:hero" else "a:hero"
        if ruleset.current_actor(st) != other:  # let `other` act first so it can guard
            st, _ = ruleset.resolve_action(
                st, Action(kind="attack", actor_id=first, target_id=other), Rng(0)
            )
        st, _ = ruleset.resolve_action(
            st,
            Action(kind="defend" if guarded else "attack", actor_id=other, target_id=first),
            Rng(0),
        )
        _, effects = ruleset.resolve_action(
            st, Action(kind="attack", actor_id=first, target_id=other), Rng(0)
        )
        hit_ac.add(effects[0].trace.split("vs AC ")[1].split(" ")[0])
    assert len(hit_ac) == 2  # the guarded attack was measured against a higher AC


def test_pbta_keep_cool_shakes_off_exposed() -> None:
    ruleset = registry.resolve("uro-pbta")
    state = _pair(ruleset)
    who = ruleset.current_actor(state)
    exposed = state.model_copy(deep=True)
    exposed.fighters[who].sheet.conditions.append("Exposed")  # type: ignore[attr-defined]
    after, effects = ruleset.resolve_action(exposed, Action(kind="keep_cool", actor_id=who), Rng(0))
    assert [e.kind for e in effects] == ["steadied"]
    assert "Exposed" not in ruleset.sheets(after)[who]["conditions"]


async def _fight(store: PostgresEventStore):  # type: ignore[no-untyped-def]
    world = await store.create_world(f"Exit-{new_id()}", starting_gear=["purse"])
    c = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p",
        new_pc_name="Ada",
        seed=2,
        encounters="turns",
    )
    await store.append_beat(
        c.branch_id,
        [
            actor_created(actor_id="a:brute", name="Brute", threat="strong"),
            item_created(item_id="i:club", name="club", owner_ref="a:brute"),
        ],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Narrator()), ruleset=UroBasic())
    plan = BeatPlan(
        triggers=["violence"], mechanics=[PlanMechanic(affordance="attack", target="a:brute")]
    )
    opened = await engine.run_beat(c, "p", "I attack the brute", plan=plan)
    assert opened.encounter is not None
    return c, engine


async def test_one_who_flees_is_neither_wounded_nor_looted(store: PostgresEventStore) -> None:
    c, engine = await _fight(store)
    pc = await store.pc_for_participant(c.campaign_id, "p")
    ended = None
    for _ in range(40):
        beat = await engine.run_beat(c, "p", "I run", action=TurnChoice(kind="flee"))
        if beat.encounter is None:
            ended = beat
            break
    assert ended is not None, "the hero never got away (or was downed) in 40 tries"
    end = (await store.branch_events(c.branch_id, event_type="EncounterEnded", limit=1))[0].payload
    if pc in end["outcome"]["fled"]:  # got away: no wound claim about them, their purse is theirs
        claims = await store.branch_events(c.branch_id, event_type="ClaimRecorded", limit=50)
        assert not any(pc in e.entity_refs for e in claims)
        assert "purse" in {h["name"] for h in await store.holdings(c.branch_id, pc)}  # type: ignore[arg-type]
    else:  # cut off every time and beaten down: the ordinary lost fight
        assert pc in end["outcome"]["out_of_fight"]


async def test_a_fled_outcome_leaves_no_wound_and_no_loot(store: PostgresEventStore) -> None:
    """The deterministic half of the test above: whatever the dice did, an outcome that lists
    the hero as fled produces no fallout for them, and the same outcome without `fled` does."""
    c, engine = await _fight(store)
    pc = await store.pc_for_participant(c.campaign_id, "p")
    assert pc is not None
    sheet = UroBasic().new_character(CharSpec(data={}), Rng(0))
    combatants = [
        Combatant(actor_id=pc, team="aggressor", sheet=sheet, is_pc=True),
        Combatant(actor_id="a:brute", team="defender", sheet=sheet),
    ]
    away = EncounterOutcome(winner_team="defender", survivors=["a:brute"], fled=[pc])
    assert await engine._combat_consequences(c.branch_id, away, combatants) == []
    beaten = EncounterOutcome(winner_team="defender", survivors=["a:brute"], out_of_fight=[pc])
    fallout = await engine._combat_consequences(c.branch_id, beaten, combatants)
    assert [e.event_type for e in fallout] == ["ClaimRecorded", "ItemTransferred"]


async def test_the_game_can_close_a_fight_undecided(store: PostgresEventStore) -> None:
    c, engine = await _fight(store)
    assert await engine.fight_status(c) is not None
    encounter_id = await engine.abandon_fight(c)
    assert encounter_id is not None and await engine.fight_status(c) is None
    end = (await store.branch_events(c.branch_id, event_type="EncounterEnded", limit=1))[0].payload
    assert end["outcome"] == {"abandoned": True}
    claims = await store.branch_events(c.branch_id, event_type="ClaimRecorded", limit=50)
    assert claims == []  # nobody won, nobody was wounded
    assert "club" in {h["name"] for h in await store.holdings(c.branch_id, "a:brute")}
    assert await engine.abandon_fight(c) is None  # nothing left to close
    after = await engine.run_beat(c, "p", "I walk away and look around")
    assert after.mode == "freeroam"  # play goes on as usual


async def test_a_known_npc_rolls_its_check_on_a_first_sheet(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Npc-{new_id()}")
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", new_pc_name="Ada"
    )
    await store.append_beat(
        c.branch_id, [actor_created(actor_id="a:sage", name="Sage", threat="strong")]
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Narrator()), ruleset=UroBasic())
    plan = BeatPlan(
        triggers=["search"], mechanics=[PlanMechanic(affordance="perceive", actor="a:sage")]
    )
    beat = await engine.run_beat(c, "p", "The Sage studies the door", plan=plan)
    assert [o.actor_id for o in beat.outcomes] == ["a:sage"]  # it rolled (it used to be skipped)
    assert beat.outcomes[0].player_view is None  # an NPC's numbers stay hidden
    sheet = await store.get_sheet(c.branch_id, "a:sage")
    assert sheet is not None and sheet["level"] == 3  # built from its danger word, and kept
