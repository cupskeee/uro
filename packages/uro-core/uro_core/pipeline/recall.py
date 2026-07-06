"""Structured recall (docs/04, 05).

Phase 1 recall is entity-triggered and deterministic: figure out which known actors
are on stage (their name/alias appears in the intent or recent narration), then pull
their beliefs and every claim about them or about a name-token mentioned in the beat.
The result is injected into the narrator prompt as established facts/rumors — which is
what lets a knowledgeable character contradict a lie the engine knows is false.

Semantic (embedding) recall is a later increment; this is the "what is *true* here?"
half that always beats semantic search when refs exist (docs/04).
"""

from __future__ import annotations

from pydantic import BaseModel

from uro_core.domain.events import BeatResolvedPayload
from uro_core.ports.projections import EngineStore
from uro_core.providers.base import Message
from uro_core.timeline.models import ActorView, BeliefView, ClaimView

_NARRATOR_SYSTEM = (
    "You are the narrator of a text RPG set in a tavern. Continue the scene in two to "
    "four sentences of vivid second-person prose. Never speak or decide for the player; "
    "narrate only what they perceive and how the world responds. You are given ESTABLISHED "
    "FACTS the world knows to be true and RUMORS that are unverified. If a character asserts "
    "something that contradicts an established fact, a knowledgeable character present may "
    "correct them; never present a rumor as settled truth."
)


class RecallBundle(BaseModel):
    recent_beats: list[BeatResolvedPayload]
    actors: list[ActorView]  # on-stage figures
    claims: list[ClaimView]  # relevant claims, truth-annotated
    beliefs: list[BeliefView]  # beliefs held by on-stage figures


def _name_token(name: str) -> str:
    return f"name:{name.strip().lower()}"


async def assemble_recall(
    store: EngineStore, branch_id: str, intent_text: str, recency: int
) -> RecallBundle:
    recent = await store.recent_beats(branch_id, recency)
    haystack = " ".join([intent_text, *(b.narration for b in recent[-2:])]).lower()

    actors_all = await store.list_actors(branch_id)
    on_stage = [
        a
        for a in actors_all
        if a.name.lower() in haystack or any(al.lower() in haystack for al in a.aliases)
    ]
    on_stage_ids = {a.actor_id for a in on_stage}

    def relevant(claim: ClaimView) -> bool:
        for ref in claim.subject_refs:
            if ref in on_stage_ids:
                return True
            if ref.startswith("name:") and ref[len("name:") :] in haystack:
                return True
        return False

    all_claims = await store.list_claims(branch_id)
    claims = [c for c in all_claims if relevant(c)]

    beliefs: list[BeliefView] = []
    for actor in on_stage:
        beliefs.extend(await store.beliefs_of(branch_id, actor.actor_id))

    return RecallBundle(recent_beats=recent, actors=on_stage, claims=claims, beliefs=beliefs)


def build_narrator_messages(recall: RecallBundle, intent_text: str) -> list[Message]:
    facts = [c.statement for c in recall.claims if c.truth == "true"]
    rumors = [c.statement for c in recall.claims if c.truth != "true"]
    context_lines = []
    if facts:
        context_lines.append("ESTABLISHED FACTS (true):\n" + "\n".join(f"- {f}" for f in facts))
    if rumors:
        context_lines.append("RUMORS (unverified):\n" + "\n".join(f"- {r}" for r in rumors))
    if recall.actors:
        present = ", ".join(f"{a.name} ({a.role})" if a.role else a.name for a in recall.actors)
        context_lines.append(f"PRESENT: {present}")

    messages = [Message(role="system", content=_NARRATOR_SYSTEM)]
    if context_lines:
        messages.append(Message(role="system", content="\n\n".join(context_lines)))
    for beat in recall.recent_beats:
        if not beat.intent_text or not beat.narration:
            continue
        messages.append(Message(role="user", content=beat.intent_text))
        messages.append(Message(role="assistant", content=beat.narration))
    messages.append(Message(role="user", content=intent_text))
    return messages
