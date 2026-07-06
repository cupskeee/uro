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

import re

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


def _mentions(haystack: str, term: str) -> bool:
    """Whole-word (phrase) match — avoids 'Ed' matching 'medal' or 'Al' matching 'also'."""
    term = term.strip().lower()
    if not term:
        return False
    return re.search(rf"\b{re.escape(term)}\b", haystack) is not None


async def assemble_recall(
    store: EngineStore, branch_id: str, intent_text: str, recency: int
) -> RecallBundle:
    recent = await store.recent_beats(branch_id, recency)
    # Scan the intent plus the recent window's intent AND narration, so an actor active
    # in an ongoing exchange (referred to by pronoun this beat) stays on stage.
    haystack = " ".join(
        [intent_text, *(b.intent_text for b in recent), *(b.narration for b in recent)]
    ).lower()

    actors_all = await store.list_actors(branch_id)
    on_stage = [
        a
        for a in actors_all
        if _mentions(haystack, a.name) or any(_mentions(haystack, al) for al in a.aliases)
    ]
    on_stage_ids = {a.actor_id for a in on_stage}

    def relevant(claim: ClaimView) -> bool:
        for ref in claim.subject_refs:
            if ref in on_stage_ids:
                return True
            if ref.startswith("name:") and _mentions(haystack, ref[len("name:") :]):
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
    claim_by_id = {c.claim_id: c for c in recall.claims}
    name_by_id = {a.actor_id: a.name for a in recall.actors}
    # Who present believes what — joined to already-recalled claims, no extra queries.
    belief_lines = [
        f"- {name_by_id[b.actor_id]} believes: {claim_by_id[b.claim_id].statement}"
        for b in recall.beliefs
        if b.actor_id in name_by_id and b.claim_id in claim_by_id
    ]

    context_lines = []
    if facts:
        context_lines.append("ESTABLISHED FACTS (true):\n" + "\n".join(f"- {f}" for f in facts))
    if rumors:
        context_lines.append("RUMORS (unverified):\n" + "\n".join(f"- {r}" for r in rumors))
    if belief_lines:
        context_lines.append(
            "PRESENT CHARACTERS' BELIEFS (may be false):\n" + "\n".join(belief_lines)
        )
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
