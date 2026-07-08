"""Phase 6 ACCEPTANCE (OQ-13, D-30): the alien ruleset plays end-to-end. Deterministic — no LLM.

The phase thesis: the engine is game-agnostic (D-1). Proof — a structurally non-d20 ruleset
(uro_pbta: 2d6 vs 7/10, a harm clock, moves, no hp/ac) runs through the SAME port, runner,
pipeline, persistence, and fork machinery as uro_basic, and produces a graded consequence a
binary d20 result cannot express (the 7-9 PARTIAL). Asserted on committed events/projections.
"""

from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, sheet_updated
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import run_encounter
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets import registry
from uro_core.rulesets.base import CharSpec, Combatant
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.rulesets.uro_pbta import Sheet as PbtaSheet
from uro_core.rulesets.uro_pbta import UroPbtA

PBTA = UroPbtA()
D20 = UroBasic()


def _pbta(forceful: int) -> dict:
    return PBTA.new_character(CharSpec(data={"stats": {"forceful": forceful}}), Rng(0))


def _d20(abilities: dict[str, int]) -> dict:
    return D20.new_character(CharSpec(data={"abilities": abilities}), Rng(0))


# --- 1. the alien conflict resolves decisively and reaches the THREE-tier band (seed-invariant) ---


def test_pbta_conflict_always_resolves_and_uses_three_tiers() -> None:
    def combs() -> list[Combatant]:
        return [
            Combatant(actor_id="a:ash", team="party", sheet=_pbta(3)),
            Combatant(actor_id="a:bane", team="foes", sheet=_pbta(-3)),
        ]

    tiers: set[str] = set()
    for seed in range(200):
        events, outcome = run_encounter(PBTA, combs(), Rng(seed), encounter_id="e")
        assert outcome.winner_team in ("party", "foes")  # a decisive result every seed
        assert len(outcome.out_of_fight) >= 1  # never a draw or turn-cap stall
        tiers |= {e.payload["result"] for e in events if e.event_type == "EncounterTurnTaken"}
    # miss / partial / full ALL occur across play — the graded band a binary success:bool can't hold
    assert {"miss", "partial", "full"} <= tiers


# --- 2. THE HEADLINE: a 7-9 partial leaves a persistent canonical consequence, carried on a fork -


async def test_partial_consequence_persists_and_forks(store: PostgresEventStore) -> None:
    world = await store.create_world(
        f"Ember-{new_id()}", ruleset_id="uro-pbta", ruleset_version=">=0"
    )
    main = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        new_pc_name="Ash",
        new_pc_id="a:ash",
        pc_sheet=_pbta(3),
        ruleset_id="uro-pbta",
        ruleset_version=">=0",
    )
    assert campaign.ruleset_id == "uro-pbta"
    await store.append_beat(main, [actor_created(actor_id="a:bane", name="Bane", tier=2)])

    # A conflict with a KNOWN 7-9 partial (seed 4): Ash seizes Bane, succeeds, but is left Exposed.
    combatants = [
        Combatant(actor_id="a:ash", team="party", sheet=_pbta(3)),
        Combatant(actor_id="a:bane", team="foes", sheet=_pbta(-3)),
    ]
    events, outcome = run_encounter(PBTA, combatants, Rng(4), encounter_id="e:1")
    results = [e.payload["result"] for e in events if e.event_type == "EncounterTurnTaken"]
    assert "partial" in results  # the d20-inexpressible middle band actually happened
    await store.append_beat(main, events)

    # the partial's STANDING COST is canon: Ash carries the 'Exposed' condition (not a bool)
    ash = await store.get_sheet(main, "a:ash")
    assert ash is not None and "Exposed" in ash["conditions"]
    assert "hp" not in ash  # PbtA harm, never a leaked hp scalar
    bane = await store.get_sheet(main, "a:bane")
    assert bane is not None and bane["harm"] == 4  # the harm clock filled → out of the fight
    assert outcome.winner_team == "party" and "a:bane" in outcome.out_of_fight

    # ...and it forks like any other state — a sibling branch inherits the condition + harm clock
    head = await store.get_branch(main)
    fork = await store.fork_branch(world.world_id, head.head_commit, "aftermath")
    assert "Exposed" in (await store.get_sheet(fork.branch_id, "a:ash"))["conditions"]
    assert (await store.get_sheet(fork.branch_id, "a:bane"))["harm"] == 4


# --- 3. both rulesets coexist in one engine build (the game-agnosticism claim, concretely) ---


def test_d20_and_pbta_coexist_with_distinct_harm_shapes() -> None:
    d20_events, _ = run_encounter(
        D20,
        [
            Combatant(actor_id="a:h", team="party", sheet=_d20({"STR": 20, "CON": 20})),
            Combatant(actor_id="a:g", team="foes", sheet=_d20({"STR": 8, "CON": 6})),
        ],
        Rng(1),
        encounter_id="e",
    )
    pbta_events, _ = run_encounter(
        PBTA,
        [
            Combatant(actor_id="a:h", team="party", sheet=_pbta(3)),
            Combatant(actor_id="a:g", team="foes", sheet=_pbta(-3)),
        ],
        Rng(1),
        encounter_id="e",
    )
    d20_sheets = [e.payload["sheet"] for e in d20_events if e.event_type == "SheetUpdated"]
    pbta_sheets = [e.payload["sheet"] for e in pbta_events if e.event_type == "SheetUpdated"]
    # d20 harm = hp; PbtA harm = a clock — the SAME runner, two irreconcilable shapes, no leak
    assert d20_sheets and all("hp" in s and "harm" not in s for s in d20_sheets)
    assert pbta_sheets and all("harm" in s and "hp" not in s for s in pbta_sheets)


# --- 4. end-to-end pipeline: a registry-bound PbtA campaign plays a fight beat ---


class _Scripted:
    def __init__(self, *, plan_json: str, narration: str) -> None:
        self._plan = plan_json
        self._narration = narration

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield self._narration

    async def complete(self, req: CompletionRequest) -> str:
        return self._plan if req.stage_tag == "planner" else '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def test_pbta_fight_through_the_pipeline(store: PostgresEventStore) -> None:
    world = await store.create_world(
        f"Ember-{new_id()}", ruleset_id="uro-pbta", ruleset_version=">=0"
    )
    main = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        new_pc_name="Ash",
        new_pc_id="a:ash",
        pc_sheet=_pbta(5),  # a strong mover so the fight resolves quickly
        ruleset_id="uro-pbta",
        ruleset_version=">=0",
    )
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:bane", name="Bane", tier=2, role="thug"),
            sheet_updated(actor_id="a:bane", sheet=_pbta(-3), ruleset_id="uro-pbta"),
        ],
    )

    # the campaign's OWN ruleset is resolved via the registry (the 6.3 binding), not hard-coded
    ruleset = registry.resolve(campaign.ruleset_id, campaign.ruleset_version)
    assert isinstance(ruleset, UroPbtA)
    plan = (
        '{"intent_class":"action","triggers":["violence"],'
        '"mechanics":[{"affordance":"seize_by_force","actor":"a:ash","target":"a:bane"}]}'
    )
    engine = Engine(
        store,
        ProviderRouter(bindings={}, default=_Scripted(plan_json=plan, narration="Ash moves in.")),
        ruleset=ruleset,
    )
    result = await engine.run_beat(campaign, "p1", "I seize the Deep Vein from Bane")

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE commit_id = $1 ORDER BY seq",
            result.commit_id,
        )
    types = [r["event_type"] for r in rows]
    assert types.count("ModeChanged") == 2  # freeroam→encounter→freeroam
    assert "EncounterStarted" in types and "EncounterEnded" in types
    assert types[-1] == "BeatResolved"
    # every turn used the PbtA vocabulary, never a d20 hit/down
    turn_results = {r["payload"]["result"] for r in rows if r["event_type"] == "EncounterTurnTaken"}
    assert turn_results and turn_results <= {"miss", "partial", "full"}
    # harm reached the timeline as opaque PbtA sheets (clock/conditions), never a leaked hp
    fight_sheets = [
        r["payload"]["sheet"]
        for r in rows
        if r["event_type"] == "SheetUpdated" and r["payload"]["actor_id"] in ("a:ash", "a:bane")
    ]
    assert fight_sheets and all("harm" in s and "hp" not in s for s in fight_sheets)

    # the fight left persistent, ruleset-shaped state readable through the normal projection
    ash = PbtaSheet.model_validate(await store.get_sheet(main, "a:ash"))
    bane = PbtaSheet.model_validate(await store.get_sheet(main, "a:bane"))
    assert not bane.in_fight or not ash.in_fight  # a decisive result persisted
