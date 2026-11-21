"""What the D-111..D-113 live pass found (2026-11-21, Sonnet 5.5): a person is usually named
before they are described, so a danger word arriving a beat later was dropped; and a strict schema
makes a model write null for the spec fields it leaves out."""

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import _without_nulls, start_campaign
from uro_core.domain.ids import new_id
from uro_core.pipeline.extraction import Extraction, ProposedActor, run_gauntlet


async def test_a_known_untagged_person_takes_the_first_danger_word_shown(
    store: PostgresEventStore,
) -> None:
    world = await store.create_world(f"Hint-{new_id()}")
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", new_pc_name="Hale"
    )
    branch = c.branch_id

    async def play(actor: ProposedActor) -> list[str]:
        events = await run_gauntlet(store, branch, Extraction(actors=[actor]))
        await store.append_beat(branch, events)
        return [e.event_type for e in events]

    assert await play(ProposedActor(name="Halvard Brenn")) == ["ActorCreated"]  # named, bare
    assert await play(ProposedActor(name="Halvard Brenn", threat="Strong")) == ["ActorThreatNoted"]
    actor = await store.find_actor_by_name(branch, "Halvard Brenn")
    assert actor is not None
    assert (await store.actor_spec(branch, actor.actor_id))[1] == "strong"
    # the first word stands; another word, a non-word, or a player character changes nothing
    assert await play(ProposedActor(name="Halvard Brenn", threat="deadly")) == []
    assert await play(ProposedActor(name="Hale", threat="deadly")) == []
    assert await play(ProposedActor(name="Pim")) == ["ActorCreated"]
    assert await play(ProposedActor(name="Pim", threat="terrifying")) == []
    # ...and an unsheeted foe's first sheet is then built from it
    assert (await store.actor_spec(branch, actor.actor_id))[1] == "strong"


def test_a_proposed_spec_loses_its_nulls() -> None:
    spec = {"level": None, "abilities": {"STR": 8, "INT": None}, "extras": {"a": None}, "tier": 1}
    assert _without_nulls(spec) == {"abilities": {"STR": 8}, "tier": 1}
