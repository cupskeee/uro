"""The game-systems requests in play (D-112, Uro Loom requests 15, 16, 18 and 19b): an unsheeted
foe's sheet comes from the spec or danger word it carries, checks and fights see what their actors
hold, a check's player-safe part reaches its own player only, and a ruleset with no checks plays
pure narration."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, item_created, sheet_updated
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.extraction import Extraction, ProposedActor, run_gauntlet
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import CharSpec, CheckRequest, CheckResult, EncounterCtx, EncounterState
from uro_core.rulesets.five_words import FIVE_WORDS
from uro_core.rulesets.rng import Rng as _Rng
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.worldpack.create import ruleset_problem
from uro_core.worldpack.models import ActorSeed
from uro_core.worldpack.parse import parse_pack


class _Narrator:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "It happens."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _engine(store: PostgresEventStore, ruleset: Any) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=_Narrator()), ruleset=ruleset)


async def _campaign(store: PostgresEventStore, ruleset: Any, *, gear: list[Any] | None = None):  # type: ignore[no-untyped-def]
    world = await store.create_world(f"gs-{new_id()}", starting_gear=gear)
    return await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Ada",
        new_pc_id="a:ada",
        pc_sheet=ruleset.new_character(CharSpec(), _Rng(0)),
        starting_items=gear,
        ruleset_id=ruleset.id,
        seed=3,
    )


async def _events(store: PostgresEventStore, branch: str, kind: str) -> list[dict[str, Any]]:
    return [e.payload for e in await store.branch_events(branch, event_type=kind, limit=100)]


# --- 15: an actor may carry a spec hint ---


async def test_an_unsheeted_foe_is_built_from_its_danger_word(store: PostgresEventStore) -> None:
    rs = UroBasic()
    campaign = await _campaign(store, rs)
    await store.append_beat(
        campaign.branch_id,
        [actor_created(actor_id="a:ogre", name="Ogre", threat="deadly", spec={"weapon_tier": 1})],
    )
    plan = BeatPlan(
        triggers=["violence"], mechanics=[PlanMechanic(affordance="attack", target="a:ogre")]
    )
    await _engine(store, rs).run_beat(campaign, "p1", "I attack the ogre", plan=plan)
    first = [
        p
        for p in await _events(store, campaign.branch_id, "SheetUpdated")
        if p["actor_id"] == "a:ogre"
    ][-1]["sheet"]  # its first sheet (events come head → genesis)
    assert first["level"] == 5 and first["abilities"]["STR"] == 16  # the danger word
    assert first["weapon_tier"] == 1  # ...and its own spec wins over what the word implies


def test_a_pack_actors_spec_must_build_under_the_packs_ruleset() -> None:
    pack = parse_pack(Path(__file__).parents[3] / "worlds" / "ashfall")
    bad = pack.model_copy(
        update={"actors": [*pack.actors, ActorSeed(id="a:x", name="X", spec={"mana": 3})]}
    )
    problem = ruleset_problem(bad)
    assert problem is not None and "a:x" in problem and "mana" in problem
    ok = pack.model_copy(
        update={"actors": [*pack.actors, ActorSeed(id="a:y", name="Y", threat="strong")]}
    )
    assert ruleset_problem(ok) is None
    # the words are the ruleset's (D-116): a pack parses with any, and its ruleset refuses it
    off = pack.model_copy(
        update={"actors": [*pack.actors, ActorSeed(id="a:z", name="Z", threat="boss")]}
    )
    problem = ruleset_problem(off)
    assert problem is not None and "a:z" in problem and "threat 'boss'" in problem


async def test_the_extractor_records_a_danger_word_for_a_new_person(
    store: PostgresEventStore,
) -> None:
    campaign = await _campaign(store, UroBasic())
    events = await run_gauntlet(
        store,
        campaign.branch_id,
        Extraction(
            actors=[
                ProposedActor(name="Varek", role="veteran", threat="Strong"),
                ProposedActor(name="Pim", threat="terrifying"),  # not the ruleset's word: dropped
            ]
        ),
        threat_words=FIVE_WORDS.words,
    )
    words = {
        e.payload["name"]: e.payload["threat"] for e in events if e.event_type == "ActorCreated"
    }
    assert words == {"Varek": "strong", "Pim": ""}
    # D-116: with no scale (no ruleset, or one that sizes nobody up) no word is ever recorded
    bare = await run_gauntlet(
        store,
        campaign.branch_id,
        Extraction(actors=[ProposedActor(name="Oda", threat="strong")]),
    )
    assert [e.payload["threat"] for e in bare if e.event_type == "ActorCreated"] == [""]
    # and a game's own word is kept as it is
    own = await run_gauntlet(
        store,
        campaign.branch_id,
        Extraction(actors=[ProposedActor(name="Brann", threat="menace")]),
        threat_words=("nuisance", "menace"),
    )
    assert [e.payload["threat"] for e in own if e.event_type == "ActorCreated"] == ["menace"]


def test_the_extractor_is_asked_in_the_rulesets_own_words() -> None:
    from uro_core.pipeline.extraction import build_extractor_messages
    from uro_core.pipeline.recall import RecallBundle
    from uro_core.rulesets.base import ThreatScale

    def system(scale: ThreatScale | None) -> str:
        return build_extractor_messages(
            RecallBundle(recent_beats=[], actors=[], claims=[], beliefs=[]),
            "A door.",
            threat=scale,
        )[0].content

    assert "`threat`" not in system(None)
    five = system(FIVE_WORDS)
    assert (
        "`threat` is how dangerous they would be in a fight, one of harmless, weak, average, "
        'strong, deadly ("a hulking veteran in plate" → strong; "a frightened child" → harmless).'
    ) in five
    own = system(ThreatScale(words=("nuisance", "menace"), measures="how hard they are to shake"))
    assert "`threat` is how hard they are to shake, one of nuisance, menace. Leave it out" in own


# --- 16: a check sees what its actor holds; 18: the player-safe part of a check ---


class _Seeing(UroBasic):
    """Uro Basic that records what each check and fight was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.checks: list[CheckRequest] = []
        self.fights: list[EncounterCtx] = []

    def resolve_check(self, req: CheckRequest, rng: _Rng) -> CheckResult:
        self.checks.append(req)
        return super().resolve_check(req, rng)

    def start_encounter(self, ctx: EncounterCtx, rng: _Rng) -> EncounterState:
        self.fights.append(ctx)
        return super().start_encounter(ctx, rng)


async def test_checks_and_fights_see_what_their_actors_hold(store: PostgresEventStore) -> None:
    rs = _Seeing()
    gear = ["rope", {"name": "longsword", "data": {"weapon_tier": 2}}]
    campaign = await _campaign(store, rs, gear=gear)
    await store.append_beat(
        campaign.branch_id,
        [
            actor_created(actor_id="a:guard", name="Guard"),
            item_created(
                item_id="i:key", name="iron key", owner_ref="a:guard", data={"opens": "gate"}
            ),
        ],
    )
    persuade = BeatPlan(
        triggers=["change_disposition"], mechanics=[PlanMechanic(affordance="persuade")]
    )
    result = await _engine(store, rs).run_beat(campaign, "p1", "I talk", plan=persuade)
    held = {h.name: h.data for h in rs.checks[0].holdings}
    assert held == {"rope": {}, "longsword": {"weapon_tier": 2}}

    attack = BeatPlan(
        triggers=["violence"], mechanics=[PlanMechanic(affordance="attack", target="a:guard")]
    )
    await _engine(store, rs).run_beat(campaign, "p1", "I attack the guard", plan=attack)
    by_actor = {c.actor_id: {h.name for h in c.holdings} for c in rs.fights[0].combatants}
    assert by_actor == {"a:ada": {"rope", "longsword"}, "a:guard": {"iron key"}}
    assert result.checks == 1


async def test_a_checks_player_view_reaches_only_its_own_player(store: PostgresEventStore) -> None:
    rs = UroBasic()
    campaign = await _campaign(store, rs)
    await store.append_beat(campaign.branch_id, [actor_created(actor_id="a:npc", name="Sage")])
    await store.append_beat(
        campaign.branch_id,
        [
            # the NPC has a sheet so its check rolls
            sheet_updated(
                actor_id="a:npc", sheet=rs.new_character(CharSpec(), _Rng(0)), ruleset_id=rs.id
            )
        ],
    )
    plan = BeatPlan(
        triggers=["change_disposition", "search"],
        mechanics=[
            PlanMechanic(affordance="persuade"),
            PlanMechanic(affordance="perceive", actor="a:npc"),
        ],
    )
    result = await _engine(store, rs).run_beat(campaign, "p1", "the Sage and I both try", plan=plan)
    views = {o.actor_id: o.player_view for o in result.outcomes}
    assert views["a:ada"] is not None and views["a:ada"]["dice"]
    assert views["a:npc"] is None  # an NPC's numbers are hidden values
    logged = await _events(store, campaign.branch_id, "CheckResolved")
    assert all(p["player_view"] for p in logged)  # the omniscient log keeps both


# --- 19b: a ruleset with no checks ---


class _Diceless(UroBasic):
    id = "diceless"

    def affordances(self) -> list[Any]:
        return []


async def test_a_ruleset_with_no_checks_plays_pure_narration(store: PostgresEventStore) -> None:
    rs = _Diceless()
    campaign = await _campaign(store, rs)
    engine = _engine(store, rs)
    for intent in ("I look around", "I swing my sword at the shadow", "I talk to the innkeeper"):
        r = await engine.run_beat(campaign, "p1", intent)
        assert r.checks == 0 and r.mode == "freeroam" and r.outcomes == []
        assert r.narration
    assert await _events(store, campaign.branch_id, "CheckResolved") == []
    assert await _events(store, campaign.branch_id, "EncounterStarted") == []
    # and a client-supplied plan can't sneak a check in: there is nothing to invoke
    with pytest.raises(Exception, match="affordance"):
        await engine.run_beat(
            campaign,
            "p1",
            "I persuade",
            plan=BeatPlan(mechanics=[PlanMechanic(affordance="persuade")]),
        )


class _Sizing:
    """A narrator whose extractor sizes the newcomer up, and which records what it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def stream(self, req: Any) -> Any:
        yield "Varek, a hulking veteran in plate, blocks the door."

    async def complete(self, req: Any) -> str:
        self.asked.append(req.messages[0].content)
        return '{"actors": [{"name": "Varek", "role": "veteran", "threat": "strong"}]}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


async def test_play_without_mechanics_still_sizes_people_up_in_the_campaigns_game(
    store: PostgresEventStore,
) -> None:
    """D-116 review: `uro play --no-mechanics` binds no ruleset, but the campaign has a game. The
    engine is given that game's scale, so the word is asked for and kept; given none, neither."""
    from uro_core.pipeline.engine import Engine
    from uro_core.providers.router import ProviderRouter

    async def threat_after(scale: Any) -> tuple[str, str]:
        campaign = await _campaign(store, UroBasic())
        model = _Sizing()
        engine = Engine(store, ProviderRouter(bindings={}, default=model), threat=scale)
        await engine.run_beat(campaign, "p1", "I walk up to the door")
        rows = (await store.query_across([campaign.branch_id], ["actors"]))[campaign.branch_id]
        (varek,) = [r for r in rows["actors"] if r["name"] == "Varek"]
        return varek["threat"], model.asked[-1]

    word, prompt = await threat_after(FIVE_WORDS)
    assert word == "strong" and "`threat` is how dangerous" in prompt
    word, prompt = await threat_after(None)
    assert word == "" and "`threat`" not in prompt
