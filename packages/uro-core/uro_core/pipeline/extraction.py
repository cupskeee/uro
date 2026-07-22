"""Extractor + validation gauntlet (docs/05, 12, 13).

The extractor turns generated prose into *proposed* world state; the gauntlet turns
proposals into committed events — or drops them.

Enforced by construction (structural — cannot be bypassed):
- Whitelist: the schema permits only actors, places, and claims, so the extractor is
  structurally incapable of proposing damage/death/loss events. A place is a benign
  named-location creation (kind='site'), never a state change to an existing place.
- Tier ceiling: the gauntlet always creates actors at T1.
- Player text isolation: the extractor is fed generated prose only, never the
  player's intent text (see engine._extract) — so a player cannot directly assert
  a claim into the extractor.

Enforced by policy (correct as far as the extractor classifies honestly):
- Provenance: narrator-asserted → `truth=true`; a character's speech → `truth=unknown`
  plus a belief for the speaker (an NPC can lie without corrupting world truth).
- Contradiction: a proposed `truth=true` claim the extractor flags as contradicting
  an existing `truth=true` claim is downgraded to `unknown`.
- Flavor filter (docs/05 promotion rules): the prompt tells the extractor to leave
  sensory/atmospheric description out entirely; a claim it marks `durable=false` (or
  any that slips through) is dropped by the gauntlet — flavor never becomes canon.
  Real models over-extract atmosphere as fact without this (found in the first live run).

NOT yet implemented (docs/13, deferred): evidence-span/consequence gating and the
"a truth=true claim must not merely restate a character's assertion" guard. So
`truth=true` currently rests on the extractor's self-declared provenance label; a
narrator that echoes a player's words is a residual surface, mitigated only by the
narrator being the trusted tier. Do not read this as a hard security boundary.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from uro_core.domain.events import (
    DomainEvent,
    Truth,
    actor_created,
    belief_changed,
    claim_recorded,
    place_created,
)
from uro_core.domain.ids import new_id
from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.pipeline.recall import RecallBundle
from uro_core.ports.projections import ProjectionQueries
from uro_core.providers.base import Message

_DEFAULT_BELIEF_CONFIDENCE = 0.8  # placeholder until belief-strength modeling (docs/11)


class ProposedActor(BaseModel):
    name: str
    role: str = ""


class ProposedPlace(BaseModel):
    name: str
    description: str = ""


class ProposedClaim(BaseModel):
    statement: str
    about: list[str] = Field(default_factory=list)
    provenance: Literal["narrator", "dialogue"] = "narrator"  # 'player' is not permitted
    speaker: str | None = None
    contradicts: list[str] = Field(default_factory=list)
    confidence: float | None = None
    durable: bool = True  # false = flavor/atmosphere; the gauntlet drops it (kept only as a
    #                       structural safety net — the prompt should keep flavor out entirely)


class Extraction(BaseModel):
    actors: list[ProposedActor] = Field(default_factory=list)
    places: list[ProposedPlace] = Field(default_factory=list)  # emergent locations (D-49)
    claims: list[ProposedClaim] = Field(default_factory=list)


# Entity-name canonicalization: fold case/whitespace and strip a leading article, so an actor
# extracted once as "the woman" and later mentioned as "woman" (or "The Duke"/"the Duke") resolves
# to ONE entity instead of splitting (found live). The `\s+` guards partial words ("another",
# "theater" are untouched). Mirrors the SQL in find_actor_by_name; kept safe/conservative —
# semantic matches ("hooded stranger" ≈ "stranger") are the embedding entity_index's job (OQ-3).
_ARTICLE_RE = re.compile(r"^(the|a|an)\s+")


def canonical_name(name: str) -> str:
    return _ARTICLE_RE.sub("", " ".join(name.strip().lower().split()))


def _name_token(name: str) -> str:
    return f"name:{canonical_name(name)}"


def build_extractor_messages(
    recall: RecallBundle, narration: str, *, env: PromptEnv | None = None
) -> list[Message]:
    known_actors = "\n".join(f"- {a.name} [{a.actor_id}]" for a in recall.actors) or "(none known)"
    known_claims = (
        "\n".join(f"- [{c.claim_id}] ({c.truth}) {c.statement}" for c in recall.claims) or "(none)"
    )
    user = (
        f"KNOWN ACTORS:\n{known_actors}\n\nKNOWN CLAIMS:\n{known_claims}\n\n"
        f"NARRATION:\n{narration}\n\n"
        'Return JSON: {"actors": [{"name", "role"}], '
        '"places": [{"name", "description"}], "claims": [{"statement", '
        '"about": [names], "provenance": "narrator"|"dialogue", '
        '"speaker": name (dialogue only), "contradicts": [known claim ids], '
        '"durable": true ONLY if still true a month from now / false for any passing moment, '
        'gesture, mood, or sensation, "confidence": 0..1}]}'
    )
    system = (env or DEFAULT_ENV).render("extractor.system.j2")
    return [
        Message(role="system", content=system),
        Message(role="user", content=user),
    ]


def parse_extraction(raw: str) -> Extraction | None:
    """Parse an extractor response into a validated Extraction, or None if unusable."""
    text = raw.strip()
    for candidate in (text, _slice_json_object(text)):
        if candidate is None:
            continue
        # Best-effort parse: a bad extraction must never crash the beat and lose the
        # narration (JSONDecodeError, ValidationError, RecursionError on deep nesting, …).
        try:
            return Extraction.model_validate(json.loads(candidate))
        except Exception:
            continue
    return None


def _slice_json_object(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


async def run_gauntlet(
    store: ProjectionQueries, branch_id: str, extraction: Extraction
) -> list[DomainEvent]:
    """Validate proposals into committable events (docs/13). Downgrade-or-drop only."""
    events: list[DomainEvent] = []
    name_to_ref: dict[str, str] = {}

    async def resolve(name: str, *, create: bool, role: str = "") -> str | None:
        key = canonical_name(name)  # "the woman" and "woman" dedup to one entity within a beat
        if not key:
            return None
        if key in name_to_ref:
            return name_to_ref[key]
        existing = await store.find_actor_by_name(branch_id, name)
        if existing is not None:  # entity resolution: link, never duplicate
            name_to_ref[key] = existing.actor_id
            return existing.actor_id
        if not create:
            return None
        ref = f"a:{new_id()}"
        events.append(actor_created(actor_id=ref, name=name.strip(), tier=1, role=role.strip()))
        name_to_ref[key] = ref
        return ref

    for pa in extraction.actors:
        await resolve(pa.name, create=True, role=pa.role)

    # Emergent places (D-49): a named, standing location the scene is set in or moves to becomes a
    # Place entity — like actors emerge from play. Dedup by canonical name against existing places
    # and within the beat, so a location revisited across beats resolves to ONE place, not a
    # duplicate. Places are independent of the actor/claim graph; kind defaults to 'site'.
    existing_places = {
        canonical_name(p.name): p.place_id for p in await store.list_places(branch_id)
    }
    seen_places: set[str] = set()
    for pp in extraction.places:
        pkey = canonical_name(pp.name)
        if not pkey or pkey in existing_places or pkey in seen_places:
            continue
        seen_places.add(pkey)
        events.append(
            place_created(
                place_id=f"p:{new_id()}", name=pp.name.strip(), description=pp.description.strip()
            )
        )

    # Pre-mint dialogue speakers so a claim *about* a speaker (resolved create=False
    # below) links to the same actor_id the belief uses, not a divergent name-token.
    for pc in extraction.claims:
        if pc.durable and pc.provenance == "dialogue" and pc.speaker and pc.speaker.strip():
            await resolve(pc.speaker, create=True)

    for pc in extraction.claims:
        if not pc.durable:
            continue  # flavor / atmosphere — not canon (docs/05 promotion rules)
        statement = pc.statement.strip()
        if not statement:
            continue
        subject_refs = [
            (await resolve(s, create=False)) or _name_token(s) for s in pc.about if s.strip()
        ]
        truth: Truth = "true" if pc.provenance == "narrator" else "unknown"
        if truth == "true":
            for cid in pc.contradicts:
                existing = await store.get_claim(branch_id, cid)
                if existing is not None and existing.truth == "true":
                    truth = "unknown"  # can't hold two contradictory truths
                    break

        claim_id = f"c:{new_id()}"
        events.append(
            claim_recorded(
                claim_id=claim_id,
                statement=statement,
                subject_refs=subject_refs,
                truth=truth,
                origin=pc.provenance,
            )
        )

        if pc.provenance == "dialogue" and pc.speaker and pc.speaker.strip():
            speaker_ref = await resolve(pc.speaker, create=True)
            if speaker_ref is not None:
                confidence = (
                    pc.confidence
                    if (pc.confidence is not None and 0.0 <= pc.confidence <= 1.0)
                    else _DEFAULT_BELIEF_CONFIDENCE
                )
                events.append(
                    belief_changed(actor_id=speaker_ref, claim_id=claim_id, confidence=confidence)
                )

    return events
