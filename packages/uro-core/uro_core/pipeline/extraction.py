"""Extractor + validation gauntlet (docs/05, 12, 13).

The extractor turns generated prose into *proposed* world state; the gauntlet turns
proposals into committed events — or drops them.

Enforced by construction (structural — cannot be bypassed):
- Whitelist: the schema permits only actors and claims, so the extractor is
  structurally incapable of proposing damage/death/terrain events.
- Tier ceiling: the gauntlet always creates actors at T1.
- Player text isolation: the extractor is fed generated prose only, never the
  player's intent text (see engine._extract) — so a player cannot directly assert
  a claim into the extractor.

Enforced by policy (correct as far as the extractor classifies honestly):
- Provenance: narrator-asserted → `truth=true`; a character's speech → `truth=unknown`
  plus a belief for the speaker (an NPC can lie without corrupting world truth).
- Contradiction: a proposed `truth=true` claim the extractor flags as contradicting
  an existing `truth=true` claim is downgraded to `unknown`.

NOT yet implemented (docs/13, deferred): evidence-span/consequence gating and the
"a truth=true claim must not merely restate a character's assertion" guard. So
`truth=true` currently rests on the extractor's self-declared provenance label; a
narrator that echoes a player's words is a residual surface, mitigated only by the
narrator being the trusted tier. Do not read this as a hard security boundary.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from uro_core.domain.events import (
    DomainEvent,
    Truth,
    actor_created,
    belief_changed,
    claim_recorded,
)
from uro_core.domain.ids import new_id
from uro_core.pipeline.recall import RecallBundle
from uro_core.ports.projections import ProjectionQueries
from uro_core.providers.base import Message

_DEFAULT_BELIEF_CONFIDENCE = 0.8  # placeholder until belief-strength modeling (docs/11)

EXTRACTOR_SYSTEM = (
    "You extract world state from RPG narration. Report only what the prose states — never "
    "invent. A fact the NARRATOR asserts as real has provenance 'narrator'. Something a "
    "CHARACTER says (in quotes or reported speech) has provenance 'dialogue' with that "
    "character as 'speaker' — it may be a lie. Name a new actor only if the prose explicitly "
    "names them. When a statement conflicts with a KNOWN CLAIM, list that claim's id in "
    "'contradicts'. Keep statements terse and self-contained. Output ONLY a JSON object."
)


class ProposedActor(BaseModel):
    name: str
    role: str = ""


class ProposedClaim(BaseModel):
    statement: str
    about: list[str] = Field(default_factory=list)
    provenance: Literal["narrator", "dialogue"] = "narrator"  # 'player' is not permitted
    speaker: str | None = None
    contradicts: list[str] = Field(default_factory=list)
    confidence: float | None = None


class Extraction(BaseModel):
    actors: list[ProposedActor] = Field(default_factory=list)
    claims: list[ProposedClaim] = Field(default_factory=list)


def _name_token(name: str) -> str:
    return f"name:{name.strip().lower()}"


def build_extractor_messages(recall: RecallBundle, narration: str) -> list[Message]:
    known_actors = "\n".join(f"- {a.name} [{a.actor_id}]" for a in recall.actors) or "(none known)"
    known_claims = (
        "\n".join(f"- [{c.claim_id}] ({c.truth}) {c.statement}" for c in recall.claims) or "(none)"
    )
    user = (
        f"KNOWN ACTORS:\n{known_actors}\n\nKNOWN CLAIMS:\n{known_claims}\n\n"
        f"NARRATION:\n{narration}\n\n"
        'Return JSON: {"actors": [{"name", "role"}], "claims": [{"statement", '
        '"about": [names], "provenance": "narrator"|"dialogue", '
        '"speaker": name (dialogue only), "contradicts": [known claim ids], '
        '"confidence": 0..1}]}'
    )
    return [
        Message(role="system", content=EXTRACTOR_SYSTEM),
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
        key = name.strip().lower()
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

    # Pre-mint dialogue speakers so a claim *about* a speaker (resolved create=False
    # below) links to the same actor_id the belief uses, not a divergent name-token.
    for pc in extraction.claims:
        if pc.provenance == "dialogue" and pc.speaker and pc.speaker.strip():
            await resolve(pc.speaker, create=True)

    for pc in extraction.claims:
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
