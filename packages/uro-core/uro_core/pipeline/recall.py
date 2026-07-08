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

from pydantic import BaseModel, Field

from uro_core.domain.events import BeatResolvedPayload
from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.ports.projections import EngineStore
from uro_core.providers.base import Message
from uro_core.timeline.models import ActorView, BeliefView, ClaimView


class RecallBundle(BaseModel):
    recent_beats: list[BeatResolvedPayload]
    actors: list[ActorView]  # on-stage figures
    claims: list[ClaimView]  # relevant claims, truth-annotated
    beliefs: list[BeliefView]  # beliefs held by on-stage figures
    # claims an on-stage figure BELIEVES but that aren't otherwise on-stage (e.g. a tavern
    # keeper's rumor about an absent hero) — carried only to render the belief, not as scene facts.
    belief_claims: list[ClaimView] = Field(default_factory=list)
    memories: list[str] = Field(default_factory=list)  # semantic recall of older beats (docs/04)


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
        if a.status != "dead"  # a dead actor is not a live present character (docs/02)
        and (_mentions(haystack, a.name) or any(_mentions(haystack, al) for al in a.aliases))
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

    # A belief about an off-stage subject (a rumor an on-stage NPC holds) still needs its claim
    # to be rendered — fetch those, separately, so they surface as beliefs and not scene facts.
    known_ids = {c.claim_id for c in claims}
    wanted = {b.claim_id for b in beliefs} - known_ids
    belief_claims = [c for c in all_claims if c.claim_id in wanted]

    return RecallBundle(
        recent_beats=recent,
        actors=on_stage,
        claims=claims,
        beliefs=beliefs,
        belief_claims=belief_claims,
    )


def _certainty(confidence: float) -> str:
    """Turn a belief's confidence into certainty phrasing — the rumor-distortion signal the
    narrator needs to hedge a garbled, third-hand belief (docs/02)."""
    if confidence >= 0.75:
        return "is certain"
    if confidence >= 0.45:
        return "believes"
    return "has heard a rumor"


def build_narrator_messages(
    recall: RecallBundle,
    intent_text: str,
    *,
    pc_actor_id: str = "",
    mechanics_traces: list[str] | None = None,
    directives: str = "",
    style: str = "",
    env: PromptEnv | None = None,
) -> list[Message]:
    facts = [c.statement for c in recall.claims if c.truth == "true"]
    rumors = [c.statement for c in recall.claims if c.truth != "true"]
    # belief_claims render on-stage NPCs' beliefs (below) but are NOT scene facts/rumors.
    claim_by_id = {c.claim_id: c for c in [*recall.claims, *recall.belief_claims]}
    name_by_id = {a.actor_id: a.name for a in recall.actors}
    # Who present believes what — joined to already-recalled claims, no extra queries. Confidence
    # is surfaced as certainty phrasing so the narrator can tell an eyewitness from a garbled,
    # third-hand rumor (a low-confidence belief → "has heard a rumor", not settled knowledge).
    belief_lines = [
        f"- {name_by_id[b.actor_id]} {_certainty(b.confidence)}: "
        f"{claim_by_id[b.claim_id].statement}"
        for b in recall.beliefs
        if b.actor_id in name_by_id and b.claim_id in claim_by_id
    ]

    context_lines = []
    # In a party (OQ-7), multiple PCs are on stage — tell the narrator WHOSE action the intent is,
    # so it doesn't attribute the swing to the wrong PC (cross-phase review P7xP1). Solo: harmless.
    acting_name = name_by_id.get(pc_actor_id)
    if acting_name:
        context_lines.append(
            f"ACTING CHARACTER: the player's intent below is {acting_name}'s action — "
            f"narrate it as {acting_name}."
        )
    if recall.memories:
        context_lines.append(
            "YOU RECALL (from earlier in the campaign):\n"
            + "\n".join(f"- {m}" for m in recall.memories)
        )
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
    # Mechanics results the ruleset produced this beat (docs/06): the narrator MUST honor the
    # outcome — a failed persuade check cannot be narrated as a success.
    if mechanics_traces:
        context_lines.append(
            "MECHANICS (weave these outcomes in — do not contradict them):\n"
            + "\n".join(f"- {t}" for t in mechanics_traces)
        )
    if directives:
        context_lines.append(f"DIRECTION: {directives}")

    system = (env or DEFAULT_ENV).render("narrator.system.j2", style=style)
    messages = [Message(role="system", content=system)]
    if context_lines:
        messages.append(Message(role="system", content="\n\n".join(context_lines)))
    for beat in recall.recent_beats:
        if not beat.intent_text or not beat.narration:
            continue
        messages.append(Message(role="user", content=beat.intent_text))
        messages.append(Message(role="assistant", content=beat.narration))
    messages.append(Message(role="user", content=intent_text))
    return messages
