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

import logging
import re

from pydantic import BaseModel, Field

from uro_core.domain.events import BeatResolvedPayload
from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.ports.projections import EngineStore
from uro_core.providers.base import Message
from uro_core.timeline.models import (
    ActorView,
    BeliefView,
    ClaimView,
    ParticipantNote,
    PlaceView,
    ThreadView,
)

logger = logging.getLogger(__name__)


class RecallBundle(BaseModel):
    recent_beats: list[BeatResolvedPayload]
    actors: list[ActorView]  # on-stage figures
    claims: list[ClaimView]  # relevant claims, truth-annotated
    beliefs: list[BeliefView]  # beliefs held by on-stage figures
    # claims an on-stage figure BELIEVES but that aren't otherwise on-stage (e.g. a tavern
    # keeper's rumor about an absent hero) — carried only to render the belief, not as scene facts.
    belief_claims: list[ClaimView] = Field(default_factory=list)
    memories: list[str] = Field(default_factory=list)  # semantic recall of older beats (docs/04)
    # LIVE plots (state active/offered) — campaign-wide context so the narrator can weave the
    # ongoing stakes, and so a Reaction-Layer thread-state change (D-33) actually reaches the
    # story instead of only mutating proj_threads invisibly. Not scene-scoped (threads have no
    # entity refs); dormant/resolved/dead are excluded (not in play / concluded).
    active_threads: list[ThreadView] = Field(default_factory=list)
    # PLACES the beat mentions (name-matched, like on-stage actors) — so the narrator sees a place's
    # current state (docs/04 gap B4: a place changing hands or being DESTROYED was invisible; the
    # meteor's crater, a holding that changed owner). Closes the last structured-recall deferral.
    places: list[PlaceView] = Field(default_factory=list)
    # The acting player's out-of-world notes (B8) — knowledge that survives a fork (time-loop/NG+),
    # rendered as the player's private recollection ONLY; never canon, never an NPC belief.
    participant_notes: list[ParticipantNote] = Field(default_factory=list)


def _name_token(name: str) -> str:
    return f"name:{name.strip().lower()}"


def _mentions(haystack: str, term: str) -> bool:
    """Whole-word (phrase) match — avoids 'Ed' matching 'medal' or 'Al' matching 'also'."""
    term = term.strip().lower()
    if not term:
        return False
    return re.search(rf"\b{re.escape(term)}\b", haystack) is not None


async def assemble_recall(
    store: EngineStore,
    branch_id: str,
    intent_text: str,
    recency: int,
    *,
    participant_id: str = "",
    world_ref: str = "",
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

    # Places/factions the beat MENTIONS (by name), like on-stage actors. Computed here (not just for
    # the narrator's place-state block below) so a claim ABOUT one surfaces too: a module rumor
    # carries pack refs like "f:red-band"/"p:vault" in subject_refs (never a name: token, since the
    # extractor only mints those for unresolved actors) — so without this, a rumor about an on-stage
    # faction/place was invisible even while its name was on stage (docs/04 B4).
    places = [p for p in await store.list_places(branch_id) if _mentions(haystack, p.name)]
    factions = [f for f in await store.list_factions(branch_id) if _mentions(haystack, f.name)]
    on_stage_entity_ids = (
        on_stage_ids | {p.place_id for p in places} | {f.faction_id for f in factions}
    )

    def relevant(claim: ClaimView) -> bool:
        for ref in claim.subject_refs:
            if ref in on_stage_entity_ids:
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

    # Live plots (campaign-wide): the stakes the narrator should keep in play. Ordered by id for
    # a deterministic prompt (list_threads is already branch-scoped + ordered).
    active_threads = [
        t for t in await store.list_threads(branch_id) if t.state in ("active", "offered")
    ]

    # (on-stage `places`/`factions` were computed above, before `relevant`, so a claim about one
    # surfaces; the narrator's place-state block below reuses `places`.)

    # The acting player's out-of-world notes (B8): pinned OR entity-triggered (surfaced this beat).
    # Best-effort like semantic recall — a failure here must not sink the beat. Fetched only for the
    # ACTING participant (party isolation), scoped to the world (survives a within-world fork).
    participant_notes: list[ParticipantNote] = []
    if participant_id and world_ref:
        all_notes: list[ParticipantNote] = []
        try:  # narrow: only the STORE call is best-effort — a filter bug below should surface
            all_notes = await store.participant_notes(participant_id, world_ref)
        except Exception:  # degrade to no notes, but LOG it (e.g. an unmigrated DB)
            logger.warning(
                "participant recall failed for %s in world %s", participant_id, world_ref
            )
        participant_notes = [
            n
            for n in all_notes
            # a ref may be prefixed ("name:vault") or bare ("vault") — match the bare term
            if n.pinned or any(_mentions(haystack, ref.split(":", 1)[-1]) for ref in n.entity_refs)
        ]

    return RecallBundle(
        recent_beats=recent,
        actors=on_stage,
        claims=claims,
        beliefs=beliefs,
        belief_claims=belief_claims,
        active_threads=active_threads,
        places=places,
        participant_notes=participant_notes,
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
    # The player's out-of-world notes (B8): the player's PRIVATE edge across loops/lives. Addressed
    # to the player, explicitly walled off from canon — the narrator may let it inform the player's
    # choices but must NOT state it as public fact or put it in an NPC's mouth.
    if recall.participant_notes:
        context_lines.append(
            "WHAT YOU (THE PLAYER) REMEMBER FROM A PREVIOUS LIFE/LOOP "
            "(known ONLY to you — the world and its people do NOT know this; it is your private "
            "edge, never state it as public fact or through another character):\n"
            + "\n".join(f"- {n.text}" for n in recall.participant_notes)
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
    # Ongoing plots (docs/17): the live stakes the narrator should keep in motion — this is how a
    # Reaction-Layer thread-state change (a dormant plot the engine just woke) reaches the story.
    if recall.active_threads:
        context_lines.append(
            "ACTIVE THREADS (ongoing plots — keep them in motion):\n"
            + "\n".join(f"- {t.stakes}" for t in recall.active_threads)
        )
    # Places in the scene + their CURRENT state (docs/04 B4): so the narrator honors a place that
    # was destroyed or changed — never describe a crater as the town it used to be.
    if recall.places:
        place_lines = []
        for p in recall.places:
            state = "" if p.status == "active" else f" [{p.status.upper()}]"
            desc = f" — {p.description}" if p.description else ""
            place_lines.append(f"- {p.name}{state}{desc}")
        context_lines.append("PLACES (current state — honor it):\n" + "\n".join(place_lines))
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
