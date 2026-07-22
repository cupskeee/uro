"""Extractor + validation gauntlet (docs/05, 12, 13).

The extractor turns generated prose into *proposed* world state; the gauntlet turns
proposals into committed events — or drops them.

Enforced by construction (structural — cannot be bypassed):
- Whitelist: the schema permits only actors, places, factions, threads, claims — and, via
  affiliations, `member_of`/`located_in` edges between them (D-50). The extractor is structurally
  incapable of proposing damage/death/loss/state-change events; every emergent entity is a benign
  CREATION, never a mutation of an existing one.
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
    edge_added,
    faction_created,
    place_created,
    thread_created,
)
from uro_core.domain.extraction_policy import ExtractionPolicy
from uro_core.domain.ids import new_id
from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.pipeline.recall import RecallBundle
from uro_core.ports.projections import ProjectionQueries
from uro_core.providers.base import Message

_DEFAULT_BELIEF_CONFIDENCE = 0.8  # placeholder until belief-strength modeling (docs/11)


class ProposedActor(BaseModel):
    name: str
    role: str = ""
    member_of: str = ""  # a faction this actor belongs to (D-50; cascade-created if it's new)
    located_in: str = ""  # a place this actor is at (D-50; cascade-created if it's new)


class ProposedPlace(BaseModel):
    name: str
    description: str = ""
    parent: str = ""  # a larger place this sits inside (D-50; cascade-created if it's new)


class ProposedFaction(BaseModel):
    name: str
    description: str = ""


class ProposedThread(BaseModel):
    stakes: str  # the plot/conflict at stake — SOFT (never deduped; two similar plots may coexist)


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
    factions: list[ProposedFaction] = Field(default_factory=list)  # emergent factions (D-50)
    threads: list[ProposedThread] = Field(default_factory=list)  # emergent plots (D-50; soft)
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
    known_places = "\n".join(f"- {p.name} [{p.place_id}]" for p in recall.places) or "(none)"
    known_claims = (
        "\n".join(f"- [{c.claim_id}] ({c.truth}) {c.statement}" for c in recall.claims) or "(none)"
    )
    user = (
        f"KNOWN ACTORS:\n{known_actors}\n\nKNOWN PLACES:\n{known_places}\n\n"
        f"KNOWN CLAIMS:\n{known_claims}\n\nNARRATION:\n{narration}\n\n"
        "Return JSON: {"
        '"actors": [{"name", "role", "member_of": faction name (optional), '
        '"located_in": place name (optional)}], '
        '"places": [{"name", "description", "parent": larger place name (optional)}], '
        '"factions": [{"name", "description"}], "threads": [{"stakes"}], '
        '"claims": [{"statement", "about": [names], "provenance": "narrator"|"dialogue", '
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
    store: ProjectionQueries,
    branch_id: str,
    extraction: Extraction,
    *,
    policy: ExtractionPolicy | None = None,
) -> list[DomainEvent]:
    """Validate proposals into committable events (docs/13). Downgrade-or-drop only.

    `policy` gates which EMERGENT categories may be created (D-49) — a disabled category is silently
    skipped (structural: the extractor may propose it, but nothing commits).

    Emergent world is RELATIONAL (D-50): an actor carries its affiliations (a faction it's
    `member_of`, a place it's `located_in`). Resolving an actor CASCADE-creates the faction/place if
    they don't exist and wires the `member_of`/`located_in` edges; a place carries a `parent`
    (`located_in`) the same way. Identity is (name + affiliation), so two same-named actors of
    DIFFERENT houses stay DISTINCT — a soft, relationship-aware dedup, not a blunt name-merge (an
    affiliation the proposal states but an existing same-name actor lacks just ENRICHES that actor).
    Factions dedup by name; threads are NEVER deduped (two similar plots may legitimately coexist).
    """
    policy = policy or ExtractionPolicy()
    events: list[DomainEvent] = []

    # Pre-load existing entities once (branches are small) for cross-beat resolution + dedup.
    places_by_key = {canonical_name(p.name): p.place_id for p in await store.list_places(branch_id)}
    factions_by_key = {
        canonical_name(f.name): f.faction_id for f in await store.list_factions(branch_id)
    }
    actors_by_key: dict[str, list[str]] = {}  # canonical name OR alias → actor ids (may be several)
    for a in await store.list_actors(branch_id):
        # Index by name AND aliases — a pack-authored "Mera" [alias "the barkeep"] must resolve when
        # later mentioned only by the alias, or she'd split into a duplicate (review HIGH; the
        # relational rewrite dropped find_actor_by_name, which matched aliases).
        for k in {canonical_name(a.name), *(canonical_name(al) for al in a.aliases)}:
            if k:
                actors_by_key.setdefault(k, []).append(a.actor_id)
    aff: dict[str, dict[str, str]] = {}  # actor_id → {"member_of"|"located_in": dst id}
    for e in await store.list_edges(branch_id):
        if e.rel_type in ("member_of", "located_in"):
            aff.setdefault(e.src, {})[e.rel_type] = e.dst
    name_to_ref: dict[str, str] = {}  # canonical actor name (this beat) → id, for claim subjects

    def resolve_faction(name: str, *, description: str = "") -> str | None:
        key = canonical_name(name)
        if not key:
            return None
        if key in factions_by_key:
            return factions_by_key[key]
        if not policy.extract_factions:
            return None
        fid = f"f:{new_id()}"
        events.append(
            faction_created(faction_id=fid, name=name.strip(), description=description.strip())
        )
        factions_by_key[key] = fid
        return fid

    def resolve_place(name: str, *, description: str = "", parent: str = "") -> str | None:
        key = canonical_name(name)
        if not key:
            return None
        if key in places_by_key:
            return places_by_key[key]
        if not policy.extract_places:
            return None
        pid = f"p:{new_id()}"
        events.append(
            place_created(place_id=pid, name=name.strip(), description=description.strip())
        )
        places_by_key[key] = pid
        if parent.strip():  # cascade the containing place + a located_in edge
            parent_id = resolve_place(parent)
            if parent_id is not None and parent_id != pid:
                events.append(edge_added(src=pid, rel_type="located_in", dst=parent_id))
        return pid

    def _enrich(actor_id: str, rel: str, dst: str | None) -> None:
        # Attach an affiliation the proposal states but the (linked) actor lacks — graph enrichment.
        if dst is not None and aff.get(actor_id, {}).get(rel) is None:
            events.append(edge_added(src=actor_id, rel_type=rel, dst=dst))
            aff.setdefault(actor_id, {})[rel] = dst

    def resolve_actor(
        name: str, *, role: str = "", member_of: str = "", located_in: str = ""
    ) -> str | None:
        key = canonical_name(name)
        if not key:
            return None
        # Gate affiliation resolution by the target policy AT THE CALL SITE, so a disabled
        # category wires NO edge even to a PRE-EXISTING faction/place (resolve_* returns an existing
        # id before its own gate; without this, factions-off still member_of'd an authored — C1).
        fid = (
            resolve_faction(member_of) if (member_of.strip() and policy.extract_factions) else None
        )
        pid = resolve_place(located_in) if (located_in.strip() and policy.extract_places) else None
        # Relationship-aware dedup: link to a same-name actor whose affiliations DON'T CONFLICT
        # (None = unknown = compatible); a same-name actor of a different house/place is DISTINCT.
        for cand in actors_by_key.get(key, []):
            ca = aff.get(cand, {})
            if (fid is None or ca.get("member_of") in (None, fid)) and (
                pid is None or ca.get("located_in") in (None, pid)
            ):
                _enrich(cand, "member_of", fid)
                _enrich(cand, "located_in", pid)
                name_to_ref[key] = cand
                return cand
        if not policy.extract_actors:
            return None
        aid = f"a:{new_id()}"
        events.append(actor_created(actor_id=aid, name=name.strip(), tier=1, role=role.strip()))
        actors_by_key.setdefault(key, []).append(aid)
        name_to_ref[key] = aid
        _enrich(aid, "member_of", fid)
        _enrich(aid, "located_in", pid)
        return aid

    def link_actor(name: str) -> str | None:
        # Resolve a claim SUBJECT to an existing actor (this beat or prior) — never creates.
        key = canonical_name(name)
        if not key:
            return None
        if key in name_to_ref:
            return name_to_ref[key]
        ids = actors_by_key.get(key)
        return ids[0] if ids else None

    for pa in extraction.actors:
        resolve_actor(pa.name, role=pa.role, member_of=pa.member_of, located_in=pa.located_in)
    for pp in extraction.places:
        resolve_place(pp.name, description=pp.description, parent=pp.parent)
    for pf in extraction.factions:
        resolve_faction(pf.name, description=pf.description)
    if policy.extract_threads:
        for pt in extraction.threads:
            stakes = pt.stakes.strip()
            if stakes:
                events.append(
                    thread_created(
                        thread_id=f"t:{new_id()}",
                        stakes=stakes,
                        state="active",
                        provenance="emergent",
                    )
                )

    if not policy.extract_claims:
        return (
            events  # claims/beliefs disabled → the entity graph only (the engine disclaims recall)
        )

    # Pre-mint dialogue speakers so a claim *about* a speaker links to the same actor_id the belief
    # uses, not a divergent name-token.
    for pc in extraction.claims:
        if pc.durable and pc.provenance == "dialogue" and pc.speaker and pc.speaker.strip():
            resolve_actor(pc.speaker)

    for pc in extraction.claims:
        if not pc.durable:
            continue  # flavor / atmosphere — not canon (docs/05 promotion rules)
        statement = pc.statement.strip()
        if not statement:
            continue
        subject_refs = [(link_actor(s) or _name_token(s)) for s in pc.about if s.strip()]
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
            speaker_ref = resolve_actor(pc.speaker)
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
