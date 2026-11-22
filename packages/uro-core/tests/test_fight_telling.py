"""D-118: a fight is told by name, and a player sees their own roll in it.

Found live (2026-11-22): the narrator was handed bare dice strings for a fight's turns, so it gave
a foe's blows to the player's character, wrote the damage as "points", and never said a character
had fallen; and a fight turn carried no roll for a client to show."""

from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import start_campaign
from uro_core.domain.events import actor_created, encounter_turn_taken
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import TurnChoice
from uro_core.pipeline.engine import Engine, fight_lines, outcome_line
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import Combatant
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.timeline.beats import for_players

_NAMES = {"a:ada": "Ada", "a:brute": "the Brute", "a:b": "Bo"}


def test_the_narrators_fight_lines_say_who_did_what_to_whom() -> None:
    turns = [
        encounter_turn_taken(
            encounter_id="e", actor_id="a:ada", action="attack", target_id="a:brute",
            trace="attack 17 vs AC 11 → hit for 4",
        ),
        encounter_turn_taken(
            encounter_id="e", actor_id="a:brute", action="attack", target_id="a:ada",
            trace="round 2: a:brute at a:ada: hit; a:b looks on", out=["a:ada"],
        ),
        encounter_turn_taken(encounter_id="e", actor_id="a:b", action="defend", trace=""),
    ]  # fmt: skip
    assert fight_lines(turns, _NAMES) == [
        "Ada (attack at the Brute): attack 17 vs AC 11 → hit for 4",
        "the Brute (attack at Ada): round 2: the Brute at Ada: hit; Bo looks on. "
        "Now out of the fight: Ada",
    ]
    # an actor the engine has no name for keeps its id, and a turn with no trace says nothing
    assert fight_lines(turns[:1], {}) == [
        "a:ada (attack at a:brute): attack 17 vs AC 11 → hit for 4"
    ]


def test_a_decided_fight_names_who_prevailed() -> None:
    sides = [
        Combatant(actor_id="a:ada", team="aggressor", sheet={}),
        Combatant(actor_id="a:b", team="aggressor", sheet={}),
        Combatant(actor_id="a:brute", team="defender", sheet={}),
    ]
    assert outcome_line("aggressor", sides, _NAMES) == "outcome: Ada, Bo prevail"
    assert outcome_line("defender", sides, _NAMES) == "outcome: the Brute prevails"
    assert outcome_line(None, sides, _NAMES) == "outcome: both sides fall"


class _Narrator:
    def __init__(self) -> None:
        self.told: list[str] = []

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        self.told.append("\n".join(m.content for m in req.messages))
        yield "Steel rings."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def test_a_players_fight_turn_shows_their_roll_live_and_read_back(
    store: PostgresEventStore,
) -> None:
    world = await store.create_world(f"Told-{new_id()}")
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="Ada",
        spec={"level": 3, "abilities": {"STR": 16}}, seed=11, encounters="turns",
    )  # fmt: skip
    await store.append_beat(c.branch_id, [actor_created(actor_id="a:brute", name="Brute")])
    narrator = _Narrator()
    engine = Engine(store, ProviderRouter(bindings={}, default=narrator), ruleset=UroBasic())
    attack = BeatPlan(
        triggers=["violence"], mechanics=[PlanMechanic(affordance="attack", target="a:brute")]
    )
    await engine.run_beat(c, "p1", "I swing at the brute", plan=attack)
    turn = await engine.run_beat(
        c, "p1", "Again", action=TurnChoice(kind="attack", target_id="a:brute")
    )
    pc = await store.pc_for_participant(c.campaign_id, "p1")

    # live: the acting player's own turn is an outcome with the roll the ruleset lets them see
    (mine,) = turn.outcomes
    assert (mine.actor_id, mine.affordance) == (pc, "attack") and mine.outcome in (
        "hit",
        "miss",
        "down",
    )
    view = mine.player_view
    assert view is not None and view["band"] in ("hit", "miss")
    assert view["total"] == view["dice"][0] + view["modifier"] and "against" in view

    # the narrator was told who struck whom, by name, and to keep the numbers out of the prose
    told = narrator.told[-1]
    assert "Ada (attack at Brute): attack " in told
    assert "never give a number" in told
    if "Brute (attack at Ada)" not in told:  # the brute acts unless the fight just ended
        assert "outcome: " in told

    # read back: the chronicle's beat keeps the roll, for the player who made it only
    record = (await store.beat_records(c.branch_id, 1))[0]
    assert [(k.affordance, k.player_view) for k in for_players([record])[0].checks] == [
        ("attack", view)
    ]
    # the turn's event says whom it was done to; a foe's own turn records no player view
    head = (await store.get_branch(c.branch_id)).head_commit  # type: ignore[union-attr]
    commit = await store.commit_detail(world.world_id, head)  # type: ignore[arg-type]
    turns = [e.payload for e in commit.events if e.event_type == "EncounterTurnTaken"]  # type: ignore[union-attr]
    assert turns
    assert all(t["target_id"] for t in turns if t["action"] == "attack")
    assert all(t["player_view"] is None for t in turns if t["actor_id"] == "a:brute")


def test_the_2d6_built_in_shows_a_player_their_own_fight_roll() -> None:
    """D-118, completed for `uro-pbta`: seizing and getting clear carry the roll, as its checks
    do. (`uro-pool` resolves a round's strikes together, so a strike's roll can land on another
    fighter's turn: it sets none yet.)"""
    from uro_core.pipeline.encounter import fighters_of, open_fight, take_turn
    from uro_core.rulesets import registry
    from uro_core.rulesets.base import CharSpec
    from uro_core.rulesets.rng import Rng

    ruleset = registry.resolve("uro-pbta")
    sheet = ruleset.new_character(CharSpec(), Rng(0))
    sides = [
        Combatant(actor_id="a:ada", team="aggressor", sheet=sheet, is_pc=True),
        Combatant(actor_id="a:brute", team="defender", sheet=sheet),
    ]
    step = open_fight(ruleset, sides, Rng(4), encounter_id="e:1")
    started = next(e.payload for e in step.events if e.event_type == "EncounterStarted")
    log = [e.payload for e in step.events if e.event_type == "EncounterTurnTaken"]
    assert step.waiting_on == "a:ada" and fighters_of(started, log)
    for kind, target in (("seize_by_force", "a:brute"), ("get_clear", None)):
        turn = take_turn(ruleset, started, log, "a:ada", TurnChoice(kind=kind, target_id=target))
        mine = next(
            e.payload
            for e in turn.events
            if e.event_type == "EncounterTurnTaken" and e.payload["chosen"] is not None
        )
        view = mine["player_view"]
        assert view["total"] == view["dice"][0] + view["modifier"] and view["band"]
        assert view["stat"] == ("forceful" if kind == "seize_by_force" else "quick")
