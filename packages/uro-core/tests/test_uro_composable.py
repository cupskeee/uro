"""D-114: `uro-composable`, the built-in ruleset whose whole game is its settings.

The two example definitions (`examples/definitions/`) share no vocabulary and play through the
same code; any of them plays under any resolver. Covers: settings validation, the resolvers' exact
odds, creation choices with guidance, checks and their effects, tags and modifiers, gear, growing
through a pending choice, fights (in order, simultaneous, leaving, joining), and a campaign played
on a definition through the engine. Deterministic (no LLM)."""

import tomllib
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import (
    answer_character_choice,
    own_character,
    preview_character,
    rebuild_character,
    start_campaign,
)
from uro_core.domain.events import actor_created
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import TurnChoice, run_encounter
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets import registry
from uro_core.rulesets.base import (
    Action,
    CharSpec,
    CharSpecError,
    CheckRequest,
    Combatant,
    EncounterCtx,
    Holding,
    answer_choice,
    build_character,
    creation_guidance,
    join_encounter,
    pending_choices,
    spec_schema_of,
    standing,
    starting_holdings,
)
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_composable.resolvers import D20, Pool, TwoD6, odds

_DEFS = Path(__file__).resolve().parents[3] / "examples" / "definitions"
CLASSIC = tomllib.loads((_DEFS / "classic-six.toml").read_text())
FEELINGS = tomllib.loads((_DEFS / "six-feelings.toml").read_text())
_HERO = {
    "ratings": {"str": 3, "con": 3, "dex": 2, "wis": 2, "cha": 1, "int": 0},
    "picks": {"kits": ["soldier"]},
}


def _game(config: dict[str, Any] | None = None) -> Any:
    return registry.resolve("uro-composable", "", config)


def _with(config: dict[str, Any], **definition: Any) -> dict[str, Any]:
    out = deepcopy(config)
    out["definition"].update(definition)
    return out


def _sheet(game: Any, spec: dict[str, Any] | None = None, threat: str = "") -> dict[str, Any]:
    return build_character(game, CharSpec(data=spec or {}, threat=threat), Rng(0))


def _check(game: Any, sheet: dict[str, Any], action: str, seed: int, **kw: Any) -> Any:
    return game.resolve_check(CheckRequest(sheet=sheet, stat=action, **kw), Rng(seed))


def _seed_for(game: Any, sheet: dict[str, Any], action: str, band: str, **kw: Any) -> int:
    for seed in range(500):
        if _check(game, sheet, action, seed, **kw).outcome == band:
            return seed
    raise AssertionError(f"{action} never came up {band}")


# --- settings ---


def test_with_no_settings_it_is_a_game_with_no_checks() -> None:
    game = _game()
    assert game.affordances() == []
    assert _sheet(game) == {
        "ratings": {},
        "tracks": {},
        "tags": [],
        "picks": {},
        "pending": [],
        "sizes": {},
    }
    assert game.downtime(_sheet(game), 30) is None
    assert pending_choices(game, _sheet(game)) == []


def test_it_is_listed_beside_the_other_built_ins() -> None:
    assert "uro-composable" in registry.available()
    assert registry.resolve("uro-basic").id == "uro-basic"  # the others are as they were


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"colour": "red"}, "colour"),  # an unknown key anywhere
        ({"actions": [{"key": "x", "uses": ["nope"]}]}, "no rating 'nope'"),
        ({"tracks": [{"key": "t", "at_min": "ghost"}]}, "no tag 'ghost'"),
        ({"encounter": {"by": "dex"}, "tracks": []}, "harm"),
        ({"advancement": None}, "role advance needs advancement"),
        ({"creation": [{"choice": "pick", "from": "nowhere"}]}, "no list 'nowhere'"),
        ({"creation": [{"choice": "array", "steps": [9]}]}, "on the scale"),
        ({"foes": [{"key": "a", "steps": 1, "threat": ["scary"]}]}, "threat"),
        ({"foes": [{"key": "a", "steps": 1}]}, "name the threat words"),
        (
            {
                "advancement": {
                    "choice": "budget",
                    "points": 1,
                    "then": {"adjust": "xp", "by": "1d4"},
                }
            },
            "not dice",
        ),
        (
            {"ready_made": [{"name": "Nobody", "spec": {"ratings": {"str": 3}}}]},
            "doesn't build",
        ),
    ],
)
def test_settings_that_dont_hold_together_are_refused(change: dict[str, Any], says: str) -> None:
    with pytest.raises(ValueError, match=says):
        _game(_with(CLASSIC, **change))


def test_a_pools_push_names_a_track_the_game_has() -> None:
    config = {**FEELINGS, "resolver": {"kind": "pool", "push": {"track": "strain"}}}
    with pytest.raises(ValueError, match="no track 'strain'"):
        _game(config)
    assert _game({**FEELINGS, "resolver": {"kind": "pool", "push": {"track": "composure"}}})


# --- the resolvers ---


def _not_fail(resolver: Any, step: int, difficulty: str = "normal") -> float:
    return float(1 - odds(resolver, step, difficulty).get("fail", 0))  # type: ignore[arg-type]


def test_the_three_resolvers_are_close_at_normal_and_all_get_harder() -> None:
    d20, two, pool = D20(), TwoD6(), Pool()
    assert [round(_not_fail(d20, s), 2) for s in range(6)] == [0.45, 0.55, 0.65, 0.75, 0.85, 0.95]
    assert [round(_not_fail(two, s), 2) for s in range(6)] == [0.42, 0.58, 0.72, 0.83, 0.92, 0.92]
    assert [round(_not_fail(pool, s), 2) for s in range(6)] == [0.5, 0.5, 0.75, 0.88, 0.94, 0.97]
    for step in range(1, 5):  # an ordinary rating: within 15 points of each other
        chances = [_not_fail(r, step) for r in (d20, two, pool)]
        assert max(chances) - min(chances) <= 0.15
    for resolver in (d20, two, pool):
        for step in range(6):
            by_difficulty = [
                _not_fail(resolver, step, d) for d in ("easy", "normal", "hard", "extreme")
            ]
            assert by_difficulty == sorted(by_difficulty, reverse=True)
            assert sum(odds(resolver, step, "normal").values()) == 1
        ladder = [_not_fail(resolver, s) for s in range(6)]
        assert ladder == sorted(ladder)  # a better rating never does worse
    assert "cost" in odds(two, 2, "normal") and "cost" not in odds(d20, 2, "normal")
    assert "great" not in odds(two, 5, "easy") and "great" in odds(TwoD6(great=True), 5, "easy")


@pytest.mark.parametrize("kind", ["d20", "2d6", "pool"])
def test_one_vocabulary_plays_under_any_resolver(kind: str) -> None:
    game = _game({**FEELINGS, "resolver": {"kind": kind}})
    sheet = _sheet(
        game,
        {"ratings": {"anger": 3, "fear": 2, "joy": 2, "sadness": 1, "disgust": 1, "surprise": 0}},
    )
    seen = {_check(game, sheet, "lash_out", seed).outcome for seed in range(200)}
    assert seen <= set(game.resolver.bands) and len(seen) >= 2
    view = _check(game, sheet, "lash_out", 1).player_view
    assert view["action"] == "lash_out" and view["rating"] == "anger" and view["word"] == "strong"
    assert view["dice"] and view["band"] in game.resolver.bands


# --- creation ---


def test_an_array_and_a_pick_make_a_character_and_guidance_counts_what_is_left() -> None:
    game = _game(CLASSIC)
    sheet = _sheet(game, _HERO)
    assert sheet["ratings"] == _HERO["ratings"] and sheet["picks"] == {"kits": ["soldier"]}
    assert sheet["tracks"]["hp"] == {"value": 12, "min": 0, "max": 12}  # 6 + 2 x con 3
    held = starting_holdings(game, CharSpec(data=_HERO))
    assert [(h.name, h.data) for h in held] == [
        ("sword", {}),
        ("mail shirt", {"tags": ["armor"]}),
        ("shield", {"tags": ["shield"]}),
    ]
    assert creation_guidance(game, CharSpec(data=_HERO)) == {"left": [], "complete": True}

    half = {"ratings": {"str": 3, "con": 3}}
    with pytest.raises(CharSpecError) as err:
        _sheet(game, half)
    assert {tuple(p.loc) for p in err.value.problems} == {("ratings",), ("picks", "kits")}
    assert creation_guidance(game, CharSpec(data=half)) == {
        "left": [
            {"choice": "array", "steps": [0, 1, 2, 2]},
            {"choice": "pick", "key": "kits", "count": 1},
        ],
        "complete": False,
    }
    # an empty spec is still the default character (what an unsheeted actor gets) ...
    assert _sheet(game)["ratings"] == dict.fromkeys(["str", "dex", "con", "int", "wis", "cha"], 2)
    # ... and guidance says everything it would take to make it a built one
    assert creation_guidance(game, CharSpec(data={}))["left"][0]["steps"] == [0, 1, 2, 2, 3, 3]
    for bad in ({"ratings": {"luck": 3}}, {"picks": {"kits": ["crown"]}}, {"level": 4}):
        with pytest.raises(CharSpecError):
            _sheet(game, bad)
    schema = spec_schema_of(game)
    assert set(schema["properties"]) == {"ratings", "picks"}  # no tier: that is never a spec's
    assert schema["properties"]["picks"]["properties"]["kits"]["items"]["enum"] == [
        "soldier",
        "wanderer",
    ]


def test_a_budget_is_counted_by_the_ruleset() -> None:
    budget = {"choice": "budget", "points": 4, "cost": [1, 1, 1, 2, 3], "caps": {"max": 4}}
    game = _game(_with(CLASSIC, creation=[budget], ready_made=[]))
    sheet = _sheet(game, {"ratings": {"str": 4, "dex": 3}})  # 3 for str (1 + 2), 1 for dex
    assert sheet["ratings"]["str"] == 4 and sheet["ratings"]["dex"] == 3
    assert creation_guidance(game, CharSpec(data={"ratings": {"str": 4}})) == {
        "left": [{"choice": "budget", "points": 1}],
        "complete": False,
    }
    _sheet(game, {"ratings": {"str": 4}})  # leaving a point unspent is allowed
    low = _sheet(game, {"ratings": {"str": 4, "dex": 3, "int": 1, "cha": 1}})  # two sold back
    assert low["ratings"]["int"] == 1
    with pytest.raises(CharSpecError, match="points spent of 4"):
        _sheet(game, {"ratings": {"str": 4, "dex": 4}})
    with pytest.raises(CharSpecError, match="from 0 to 4 here"):
        _sheet(game, {"ratings": {"str": 5}})


def test_a_rolled_character_is_the_seeds_and_the_same_every_time() -> None:
    rolled = {
        "choice": "roll",
        "dice": "3d6",
        "steps": [{"upto": 5, "step": 0}, {"upto": 8, "step": 1}, {"upto": 12, "step": 2},
                  {"upto": 15, "step": 3}, {"upto": 17, "step": 4}, {"upto": 18, "step": 5}],
    }  # fmt: skip
    game = _game(_with(CLASSIC, creation=[rolled], ready_made=[]))
    one, again, other = (
        _sheet(game, {"seed": 7}),
        _sheet(game, {"seed": 7}),
        _sheet(game, {"seed": 8}),
    )
    assert one == again and one["ratings"] != other["ratings"]
    assert all(0 <= s <= 5 for s in one["ratings"].values())
    with pytest.raises(CharSpecError, match="the dice set the ratings"):
        _sheet(game, {"seed": 7, "ratings": {"str": 5}})
    assert creation_guidance(game, CharSpec(data={})) == {
        "left": [{"choice": "roll", "dice": "3d6"}],
        "complete": False,
    }
    assert "seed" in spec_schema_of(game)["properties"]


def test_a_foe_takes_the_tier_its_danger_word_picks_and_a_spec_cant_ask_for_one() -> None:
    game = _game(CLASSIC)
    brute = _sheet(game, threat="strong")
    assert set(brute["ratings"].values()) == {3} and brute["tracks"]["hp"]["max"] == 14
    assert _sheet(game, threat="deadly")["tracks"]["hp"] == {"value": 30, "min": 0, "max": 30}
    assert _sheet(game, threat="average")["tracks"]["hp"]["max"] == 10  # no tier claims it
    with pytest.raises(CharSpecError):  # or a player would build a boss (review)
        _sheet(game, {"tier": "boss"})


# --- checks ---


def test_a_checks_effects_land_and_a_track_at_its_end_wears_its_tag() -> None:
    game = _game(FEELINGS)
    ratings = {"anger": 3, "fear": 2, "joy": 2, "sadness": 1, "disgust": 1, "surprise": 0}
    sheet = _sheet(game, {"ratings": ratings})
    fail = _seed_for(game, sheet, "lash_out", "fail")
    after = _check(game, sheet, "lash_out", fail).sheet
    assert after["tracks"]["composure"]["value"] == 4 and after["tracks"]["growth"]["value"] == 1
    ok = _check(game, sheet, "lash_out", _seed_for(game, sheet, "lash_out", "success"))
    assert ok.sheet is None  # nothing in the definition for that band: the sheet is as it was
    assert _check(game, sheet, "slip_away", 3).sheet is None  # an action with no effects at all

    low = deepcopy(sheet)
    low["tracks"]["composure"]["value"] = 1
    spent = _check(game, low, "lash_out", _seed_for(game, low, "lash_out", "cost")).sheet
    assert spent["tracks"]["composure"]["value"] == 0 and spent["tags"] == ["overwhelmed"]
    # the tag's modifier now counts: every rating a step lower
    assert _check(game, spent, "lash_out", 0).player_view["step"] == 2
    assert _check(game, sheet, "lash_out", 0).player_view["step"] == 3
    # a day's rest renews the track, and the tag goes with it
    rested = game.downtime(spent, 1)
    assert rested["tracks"]["composure"]["value"] == 5 and rested["tags"] == []
    assert game.downtime(rested, 1) is None


def test_a_full_advance_track_opens_a_choice_the_player_answers() -> None:
    game = _game(FEELINGS)
    sheet = _sheet(game, {"ratings": {"anger": 3, "fear": 2, "joy": 2, "sadness": 1,
                                     "disgust": 1, "surprise": 0}})  # fmt: skip
    sheet["tracks"]["growth"]["value"] = 4
    grown = _check(game, sheet, "lash_out", _seed_for(game, sheet, "lash_out", "fail")).sheet
    assert grown["tracks"]["growth"]["value"] == 5 and grown["pending"] == ["advance"]
    (choice,) = pending_choices(game, grown)
    assert choice.id == "advance" and "1 to spend" in choice.prompt
    assert {"rating": "anger", "label": "Anger", "step": 3, "cost": 1} in choice.options
    for bad in ({"ratings": {"anger": 2}}, {"ratings": {"luck": 1}}, {"ratings": {}}, {"x": 1}):
        with pytest.raises(CharSpecError):
            answer_choice(game, grown, "advance", bad)
    after = answer_choice(game, grown, "advance", {"ratings": {"anger": 1}})
    assert after["ratings"]["anger"] == 4 and after["pending"] == []
    assert after["tracks"]["growth"]["value"] == 0  # the definition's `then`
    assert pending_choices(game, after) == []


def test_an_action_with_a_cost_is_paid_for_or_fails_unrolled() -> None:
    focus = {"key": "focus", "min": 0, "max": 2, "start": "max"}
    spell = {"key": "weave", "uses": ["int"], "cost": {"track": "focus", "by": 1}}
    config = _with(CLASSIC, tracks=[*CLASSIC["definition"]["tracks"], focus],
                   actions=[*CLASSIC["definition"]["actions"], spell])  # fmt: skip
    game = _game(config)
    sheet = _sheet(game)
    once = _check(game, sheet, "weave", 1).sheet
    twice = _check(game, once, "weave", 1).sheet
    assert once["tracks"]["focus"]["value"] == 1 and twice["tracks"]["focus"]["value"] == 0
    dry = _check(game, twice, "weave", 1)
    assert dry.outcome == "fail" and dry.sheet is None and dry.player_view["unpaid"] is True


def test_a_pushed_pool_is_paid_for_on_its_track() -> None:
    config = {**FEELINGS, "resolver": {"kind": "pool", "push": {"track": "composure", "by": 2}}}
    game = _game(config)
    sheet = _sheet(game)
    pushed = next(
        r for r in (_check(game, sheet, "slip_away", s) for s in range(300))
        if r.player_view["pushed"]
    )  # fmt: skip
    assert pushed.sheet["tracks"]["composure"]["value"] == 3 and "pushed" in pushed.trace
    plain = next(
        r for r in (_check(game, sheet, "slip_away", s) for s in range(300))
        if not r.player_view["pushed"]
    )  # fmt: skip
    assert plain.sheet is None


# --- fights ---


def _fighters(game: Any, *, armored: bool = False) -> list[Combatant]:
    armor = [Holding(item_id="i:1", name="mail", data={"tags": ["armor"]})] if armored else []
    return [
        Combatant(actor_id="a:hero", team="aggressor", sheet=_sheet(game, _HERO), is_pc=True),
        Combatant(
            actor_id="a:foe", team="defender", sheet=_sheet(game, threat="weak"), holdings=armor
        ),
    ]


def test_a_fight_runs_to_its_end_and_replays_from_its_seed() -> None:
    game = _game(CLASSIC)
    events, outcome = run_encounter(game, _fighters(game), Rng(4), encounter_id="e")
    again, same = run_encounter(game, _fighters(game), Rng(4), encounter_id="e")
    assert [e.payload for e in events] == [e.payload for e in again] and outcome == same
    assert outcome.winner_team in ("aggressor", "defender")
    loser = "a:foe" if outcome.winner_team == "aggressor" else "a:hero"
    assert outcome.out_of_fight == [loser]
    final = {e.payload["actor_id"]: e.payload["sheet"] for e in events
             if e.event_type == "SheetUpdated"}  # fmt: skip
    assert final[loser]["tracks"]["hp"]["value"] == 0
    # the award goes to a player character still standing, and to nobody else
    won = next(s for s in range(50)
               if run_encounter(game, _fighters(game), Rng(s), encounter_id="e")[1].winner_team
               == "aggressor")  # fmt: skip
    events, _ = run_encounter(game, _fighters(game), Rng(won), encounter_id="e")
    final = {e.payload["actor_id"]: e.payload["sheet"] for e in events
             if e.event_type == "SheetUpdated"}  # fmt: skip
    assert final["a:hero"]["tracks"]["xp"]["value"] == 1
    assert final["a:foe"]["tracks"]["xp"]["value"] == 0


def test_turn_order_follows_the_rating_and_what_a_foe_holds_counts_against_you() -> None:
    game = _game(CLASSIC)
    state = game.start_encounter(
        EncounterCtx(encounter_id="e", combatants=_fighters(game, armored=True)), Rng(0)
    )
    assert game.current_actor(state) == "a:hero"  # dex 2 against the minion's 1
    kinds = [a.kind for a in game.legal_actions(state, "a:hero")]
    assert kinds == ["strike.str", "strike.dex", "retreat", "hold"]
    strike = Action(kind="strike.str", actor_id="a:hero", target_id="a:foe")
    _, effects = game.resolve_action(state, strike, Rng(1))
    assert effects[0].payload["step"] == 2  # strength 3, a step lower against armor
    bare = game.start_encounter(EncounterCtx(encounter_id="e", combatants=_fighters(game)), Rng(0))
    _, effects = game.resolve_action(bare, strike, Rng(1))
    assert effects[0].payload["step"] == 3
    with pytest.raises(ValueError, match="needs a target"):
        game.resolve_action(state, Action(kind="strike.str", actor_id="a:hero"), Rng(1))
    with pytest.raises(ValueError, match="no action of this game's fights"):
        game.resolve_action(state, Action(kind="persuade", actor_id="a:hero"), Rng(1))


def test_leaving_and_joining_a_fight() -> None:
    game = _game(CLASSIC)
    start = game.start_encounter(EncounterCtx(encounter_id="e", combatants=_fighters(game)), Rng(0))
    retreat = Action(kind="retreat", actor_id="a:hero")
    away = next(
        st for st, effects in (game.resolve_action(start, retreat, Rng(s)) for s in range(100))
        if any(e.kind == "fled" for e in effects)
    )  # fmt: skip
    outcome = game.is_over(away)
    assert outcome is not None and outcome.fled == ["a:hero"] and outcome.out_of_fight == []
    assert outcome.winner_team == "defender" and standing(game, away) == {"a:foe"}

    late = Combatant(actor_id="a:late", team="aggressor", sheet=_sheet(game, _HERO), is_pc=True)
    joined = join_encounter(game, start, late, Rng(0))
    assert joined is not None and standing(game, joined) == {"a:hero", "a:foe", "a:late"}
    assert game.current_actor(joined) == game.current_actor(start)
    acted = []
    for _ in range(3):
        who = game.current_actor(joined)
        acted.append(who)
        joined, _ = game.resolve_action(joined, Action(kind="hold", actor_id=who), Rng(0))
    assert sorted(acted) == ["a:foe", "a:hero", "a:late"]


def test_a_simultaneous_round_lands_all_at_once() -> None:
    config = _with(CLASSIC, encounter={"by": "simultaneous"})
    game = _game(config)
    fighters = _fighters(game)
    for c in fighters:  # both one good hit from out
        c.sheet["tracks"]["hp"]["value"] = 1
    start = game.start_encounter(EncounterCtx(encounter_id="e", combatants=fighters), Rng(0))
    for seed in range(400):
        rng, state = Rng(seed), start
        first = game.current_actor(state)
        other = "a:foe" if first == "a:hero" else "a:hero"
        state, effects = game.resolve_action(
            state, Action(kind="strike.str", actor_id=first, target_id=other), rng
        )
        assert effects == [] and game.current_actor(state) == other  # nothing until all commit
        state, effects = game.resolve_action(
            state, Action(kind="strike.str", actor_id=other, target_id=first), rng
        )
        outcome = game.is_over(state)
        if outcome is not None and outcome.winner_team is None:
            assert sorted(outcome.out_of_fight) == ["a:foe", "a:hero"]  # each downed the other
            return
    raise AssertionError("no round in 400 seeds had both land")


# --- a campaign on a definition, through the engine ---


class _Narrator:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "The hall goes quiet."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def _campaign(store: PostgresEventStore, config: dict[str, Any], **kw: Any) -> Any:
    world = await store.create_world(f"Comp-{new_id()}")
    return await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Ada",
        ruleset_id="uro-composable",
        ruleset_config=config,
        seed=3,
        **kw,
    )


async def test_a_campaign_plays_a_definition_from_creation_to_a_fight(
    store: PostgresEventStore,
) -> None:
    c = await _campaign(store, CLASSIC, spec=_HERO, encounters="turns")
    game = await registry.resolve_for_campaign(store, c)
    assert game.id == "uro-composable" and game.d.id == "classic-six"  # pinned on the campaign
    engine = Engine(store, ProviderRouter(bindings={}, default=_Narrator()), ruleset=game)
    mine = await own_character(store, c, "p1")
    assert mine.sheet is not None and mine.sheet["ratings"]["str"] == 3
    assert sorted(h["name"] for h in mine.holdings) == ["mail shirt", "shield", "sword"]

    talk = BeatPlan(
        triggers=["change_disposition"], mechanics=[PlanMechanic(affordance="persuade")]
    )
    beat = await engine.run_beat(c, "p1", "I talk the guard round", plan=talk)
    (check,) = beat.outcomes
    assert check.affordance == "persuade" and check.outcome in ("fail", "success", "great")
    assert check.player_view is not None and check.player_view["rating"] == "cha"

    await store.append_beat(
        c.branch_id, [actor_created(actor_id="a:brute", name="Brute", threat="weak")]
    )
    attack = BeatPlan(
        triggers=["violence"], mechanics=[PlanMechanic(affordance="strike.str", target="a:brute")]
    )
    wait = (await engine.run_beat(c, "p1", "I attack the brute", plan=attack)).encounter
    assert wait is not None and wait.participant_id == "p1"
    foe = await store.get_sheet(c.branch_id, "a:brute")
    assert foe is not None and foe["tracks"]["hp"]["max"] == 4  # the tier its danger word picks
    for _ in range(60):
        if wait is None:
            break
        spec = wait.legal_actions[0]
        choice = TurnChoice(kind=spec.kind, target_id=spec.targets[0] if spec.targets else None)
        wait = (await engine.run_beat(c, "p1", spec.kind, action=choice)).encounter
    assert wait is None and await store.open_encounters(c.branch_id) == []


async def test_a_preview_says_what_is_left_and_a_rebuild_swaps_the_kit(
    store: PostgresEventStore,
) -> None:
    c = await _campaign(store, CLASSIC, spec=_HERO)
    half = await preview_character(
        store, branch_id=c.branch_id, campaign=c, spec={"ratings": {"str": 3}}
    )
    assert half.ok is False and half.guidance is not None
    assert half.guidance["left"][0] == {"choice": "array", "steps": [0, 1, 2, 2, 3]}
    whole = await preview_character(store, branch_id=c.branch_id, campaign=c, spec=_HERO)
    assert whole.ok and whole.guidance == {"left": [], "complete": True}

    other = {**_HERO, "picks": {"kits": ["wanderer"]}}
    rebuilt = await rebuild_character(store, c, "p1", spec=other)
    assert sorted(h["name"] for h in rebuilt.holdings) == ["bedroll", "lantern", "staff"]
    # a ruleset that says nothing about guidance previews as before
    world = await store.create_world(f"Basic-{new_id()}")
    plain = await preview_character(store, branch_id=world.main_branch_id, spec={})
    assert plain.ok and plain.guidance is None


async def test_growing_is_answered_through_the_characters_own_choices(
    store: PostgresEventStore,
) -> None:
    c = await _campaign(store, FEELINGS, spec={"ratings": {"anger": 3, "fear": 2, "joy": 2,
                                                             "sadness": 1, "disgust": 1,
                                                             "surprise": 0}})  # fmt: skip
    game = await registry.resolve_for_campaign(store, c)
    pc = await store.pc_for_participant(c.campaign_id, "p1")
    assert pc is not None
    sheet = await store.get_sheet(c.branch_id, pc)
    assert sheet is not None
    full = game.progression(sheet, type("A", (), {"data": {"adjust": {"growth": 5}}})())
    from uro_core.domain.events import sheet_updated

    await store.append_beat(
        c.branch_id, [sheet_updated(actor_id=pc, sheet=full, ruleset_id=game.id)]
    )
    mine = await own_character(store, c, "p1")
    assert [ch.id for ch in mine.choices] == ["advance"]
    after = await answer_character_choice(store, c, "p1", "advance", {"ratings": {"joy": 1}})
    assert after.sheet is not None and after.sheet["ratings"]["joy"] == 3 and after.choices == []


# --- found by the D-114 review ---


def test_an_advancement_is_answered_once_whatever_its_then_does() -> None:
    config = deepcopy(FEELINGS)
    config["definition"]["advancement"]["then"] = {"adjust": "composure", "by": -1}
    game = _game(config)
    sheet = _sheet(game)
    sheet["tracks"]["growth"]["value"] = 5
    sheet["pending"] = ["advance"]
    after = answer_choice(game, sheet, "advance", {"ratings": {"anger": 1}})
    assert after["pending"] == [] and after["tracks"]["growth"]["value"] == 0
    assert after["tracks"]["composure"]["value"] == 4  # the `then` still happened


def test_a_sheet_from_another_definition_still_reads() -> None:
    classic, feelings = _game(CLASSIC), _game(FEELINGS)
    foreign = _sheet(feelings)
    classic.downtime(foreign, 1)  # brought up to this game: it used to be a KeyError
    assert pending_choices(classic, foreign) == []
    result = _check(classic, foreign, "persuade", 1)
    assert result.outcome in ("fail", "success", "great")
    assert set(classic.progression(foreign, type("A", (), {"data": {}})())["ratings"]) == {
        "str", "dex", "con", "int", "wis", "cha"
    }  # fmt: skip


def test_a_push_is_paid_for_after_the_actions_own_cost() -> None:
    config = deepcopy(FEELINGS)
    config["resolver"] = {"kind": "pool", "push": {"track": "composure", "by": 1}}
    config["definition"]["actions"][1]["cost"] = {"track": "composure", "by": 1}  # slip_away
    game = _game(config)
    sheet = _sheet(game)
    sheet["tracks"]["composure"]["value"] = 1  # enough for the action, not for a push as well
    for seed in range(200):
        result = _check(game, sheet, "slip_away", seed)
        assert result.player_view["pushed"] is False
        assert result.sheet["tracks"]["composure"]["value"] == 0


def test_leaving_a_fight_thats_undecided_tidies_what_only_lasts_a_fight() -> None:
    config = deepcopy(CLASSIC)
    d = config["definition"]
    d["tags"] = [{"key": "staggered", "lasts": "encounter"}]
    d["tracks"].append({"key": "guard", "min": 0, "max": 2, "start": "max",
                        "renew": {"encounter": True}})  # fmt: skip
    game = _game(config)
    state = game.start_encounter(EncounterCtx(encounter_id="e", combatants=_fighters(game)), Rng(0))
    hero = state.fighters["a:hero"].sheet
    hero.tags.append("staggered")
    hero.tracks["guard"].value = 0
    assert game.sheets(state)["a:hero"]["tags"] == ["staggered"]  # as the fight has it
    left = game.leave_encounter(state, "a:hero")  # as the one leaving takes it
    assert left["tags"] == [] and left["tracks"]["guard"]["value"] == 2
    assert game.sheets(state)["a:hero"]["tags"] == ["staggered"]  # the fight's state untouched


def test_in_a_simultaneous_round_one_beaten_as_they_turn_to_go_is_out_whoever_went_first() -> None:
    for first in ("a:hero", "a:foe"):
        game = _game(_with(CLASSIC, encounter={"by": "simultaneous"}))
        fighters = _fighters(game)
        if first == "a:foe":
            fighters.reverse()
        next(c for c in fighters if c.actor_id == "a:hero").sheet["tracks"]["hp"]["value"] = 1
        start = game.start_encounter(EncounterCtx(encounter_id="e", combatants=fighters), Rng(0))
        seen = set()
        for seed in range(300):
            rng, state = Rng(seed), start
            for who in fighters:
                act = (
                    Action(kind="retreat", actor_id="a:hero")
                    if who.actor_id == "a:hero"
                    else Action(kind="strike.str", actor_id="a:foe", target_id="a:hero")
                )
                state, effects = game.resolve_action(state, act, rng)
            kinds = {e.kind for e in effects}
            hero = state.fighters["a:hero"]
            if "out" in kinds:  # struck down in the round they tried to leave
                assert not hero.fled and "fled" not in kinds
                seen.add("out")
            elif "fled" in kinds:
                assert hero.fled
                seen.add("fled")
        assert seen == {"out", "fled"}
