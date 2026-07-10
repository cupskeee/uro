"""Structured recall: word-boundary matching, the recency window, belief injection."""

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, beat_resolved, claim_recorded
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
