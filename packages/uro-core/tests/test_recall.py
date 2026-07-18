"""Structured recall: word-boundary matching, the recency window, belief injection."""

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    actor_created,
    beat_resolved,
    claim_recorded,
    faction_created,
    place_created,
)
from uro_core.domain.ids import new_id
from uro_core.pipeline.recall import RecallBundle, assemble_recall, build_narrator_messages
from uro_core.timeline.models import ActorView, BeliefView, ClaimView, ThreadView


async def _branch(store: PostgresEventStore) -> str:
    world = await store.create_world(f"test-{new_id()}")
    return world.main_branch_id


async def test_recall_uses_word_boundaries(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:ed", name="Ed", tier=2),
            claim_recorded(
                claim_id="c:ed", statement="Ed owes a debt.", subject_refs=["a:ed"], truth="true"
            ),
        ],
    )
    # 'red' and 'medal' contain 'ed' but must NOT drag Ed on stage.
    quiet = await assemble_recall(store, branch, "I admire the red medal", 8)
    assert quiet.actors == [] and quiet.claims == []
    # A whole-word 'Ed' does.
    loud = await assemble_recall(store, branch, "I greet Ed warmly", 8)
    assert [a.actor_id for a in loud.actors] == ["a:ed"]
    assert [c.claim_id for c in loud.claims] == ["c:ed"]


async def test_recall_keeps_recently_mentioned_actor_on_stage(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(branch, [actor_created(actor_id="a:flora", name="Flora", tier=2)])
    await store.append_beat(
        branch,
        [
            beat_resolved(
                beat_id="b1",
                participant_id="p",
                intent_text="I greet Flora",
                narration="Flora smiles behind the bar.",
            )
        ],
    )
    # Current intent uses a pronoun; Flora is still on stage from the last beat's window.
    recall = await assemble_recall(store, branch, "I ask her about the ale", 8)
    assert any(a.actor_id == "a:flora" for a in recall.actors)


async def test_recall_surfaces_a_claim_about_a_mentioned_faction(
    store: PostgresEventStore,
) -> None:
    """A module rumor carries a pack ref like "f:redband"/"p:vault" in subject_refs (never a name:
    token - the extractor only mints those for unresolved actors). So a claim ABOUT a faction OR a
    place must surface when that entity is on stage by name (docs/04 B4), like an actor's claims do,
    and stay hidden otherwise. Covers BOTH the faction and the place id-match arms of the union."""
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            faction_created(
                faction_id="f:redband",
                name="Red Band",
                kind="faction",
                description="a mercenary company",
            ),
            place_created(
                place_id="p:vault",
                name="the Vault",
                kind="site",
                description="a sealed treasury",
            ),
            claim_recorded(
                claim_id="c:rb",
                statement="The Red Band broke the truce.",
                subject_refs=["f:redband"],
                truth="unknown",
                origin="module",
            ),
            claim_recorded(
                claim_id="c:vault",
                statement="The Vault was already looted.",
                subject_refs=["p:vault"],
                truth="unknown",
                origin="module",
            ),
        ],
    )
    hidden = await assemble_recall(store, branch, "I count my coins", 8)
    assert hidden.claims == []  # neither entity mentioned -> both claims stay off-stage
    faction = await assemble_recall(store, branch, "What of the Red Band?", 8)
    assert [c.claim_id for c in faction.claims] == ["c:rb"]  # faction arm
    place = await assemble_recall(store, branch, "I search the Vault", 8)
    assert [c.claim_id for c in place.claims] == ["c:vault"]  # place arm


def test_narrator_prompt_surfaces_present_beliefs() -> None:
    recall = RecallBundle(
        recent_beats=[],
        actors=[ActorView(actor_id="a:flora", name="Flora", tier=2, role="innkeeper", aliases=[])],
        claims=[
            ClaimView(
                claim_id="c:war",
                statement="The Duke plans war.",
                subject_refs=["name:duke"],
                truth="unknown",
                origin="dialogue",
            )
        ],
        beliefs=[
            BeliefView(actor_id="a:flora", claim_id="c:war", confidence=0.9, learned_from=None)
        ],
    )
    blob = "\n".join(m.content for m in build_narrator_messages(recall, "I ask about the Duke"))
    # beliefs are live, and confidence surfaces as certainty phrasing (0.9 → "is certain")
    assert "Flora is certain: The Duke plans war." in blob


def test_narrator_prompt_surfaces_recalled_memories() -> None:
    recall = RecallBundle(
        recent_beats=[],
        actors=[],
        claims=[],
        beliefs=[],
        memories=["The oracle warned of a great flood."],
    )
    blob = "\n".join(m.content for m in build_narrator_messages(recall, "what now?"))
    assert "The oracle warned of a great flood." in blob


def test_narrator_prompt_surfaces_active_threads() -> None:
    # Active/offered plots reach the narrator so it can keep them in motion (docs/17); a Reaction-
    # Layer thread-state change is otherwise invisible to the story.
    recall = RecallBundle(
        recent_beats=[],
        actors=[],
        claims=[],
        beliefs=[],
        active_threads=[
            ThreadView(
                thread_id="t:ritual",
                stakes="The Saltborn will drown Vel.",
                state="active",
                provenance="author",
            ),
        ],
    )
    blob = "\n".join(m.content for m in build_narrator_messages(recall, "I look around"))
    assert "ACTIVE THREADS" in blob and "The Saltborn will drown Vel." in blob


async def test_assemble_recall_surfaces_only_live_threads(store: PostgresEventStore) -> None:
    from uro_core.domain.events import thread_created, thread_state_changed

    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            thread_created(thread_id="t:live", stakes="a war brews", state="active"),
            thread_created(thread_id="t:offer", stakes="a bargain offered", state="offered"),
            thread_created(thread_id="t:sleep", stakes="a dormant plot", state="dormant"),
            thread_created(thread_id="t:done", stakes="a settled matter", state="active"),
            thread_state_changed(thread_id="t:done", to_state="resolved"),
        ],
    )
    recall = await assemble_recall(store, branch, "what is happening?", 8)
    live = {t.thread_id for t in recall.active_threads}
    assert live == {"t:live", "t:offer"}  # active + offered only; dormant/resolved excluded


def test_narrator_prompt_surfaces_place_state() -> None:
    from uro_core.timeline.models import PlaceView

    recall = RecallBundle(
        recent_beats=[],
        actors=[],
        claims=[],
        beliefs=[],
        places=[
            PlaceView(
                place_id="p:vel",
                name="Vel",
                kind="settlement",
                status="destroyed",
                description="a smoking crater where the city stood",
            ),
        ],
    )
    blob = "\n".join(m.content for m in build_narrator_messages(recall, "I ride toward Vel"))
    assert "PLACES" in blob and "Vel [DESTROYED]" in blob and "smoking crater" in blob


async def test_assemble_recall_surfaces_a_mentioned_place(store: PostgresEventStore) -> None:
    from uro_core.domain.events import place_created, place_destroyed

    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            place_created(place_id="p:vel", name="Vel", kind="settlement", description="a city"),
            place_created(place_id="p:far", name="Faroff", kind="settlement", description="far"),
            place_destroyed(place_id="p:vel", cause="the meteor"),
        ],
    )
    recall = await assemble_recall(store, branch, "I approach the ruins of Vel", 8)
    names = {p.name: p.status for p in recall.places}
    assert names.get("Vel") == "destroyed"  # the mentioned place + its current state
    assert "Faroff" not in names  # not mentioned → not surfaced
