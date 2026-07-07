"""Turn a parsed pack into the seed events committed at import (docs/09). No LLM.

The authored geography/actors/factions/relations/threads become `PlaceCreated`/`FactionCreated`/
`ActorCreated`/`EdgeAdded`/`ClaimRecorded`/`ThreadCreated` (emitter S) in the WorldGenesis commit —
so they exist as timeline state BEFORE any History seeding, and survive identically across seeds
(docs/03: identical geography, different dynasties). AI-backfilled conflict seeds ride the same
path, tagged `provenance=ai_backfill` on the committed `ThreadCreated` — so the machine's
inventions are reviewable committed state, not a discarded in-memory model.
"""

from __future__ import annotations

from uro_core.domain.events import (
    DomainEvent,
    actor_created,
    claim_recorded,
    edge_added,
    faction_created,
    place_created,
    thread_created,
)
from uro_core.worldpack.models import PlaceKind, WorldPack


def pack_to_events(pack: WorldPack) -> list[DomainEvent]:
    """The seed events (emitter S) for a pack's authored entities + cross-linked relations."""
    events: list[DomainEvent] = []
    for p in pack.places:
        kind: PlaceKind = p.kind
        events.append(
            place_created(place_id=p.id, name=p.name, kind=kind, description=p.description)
        )
        if p.parent:
            events.append(edge_added(src=p.id, rel_type="located_in", dst=p.parent))
    for f in pack.factions:
        events.append(
            faction_created(faction_id=f.id, name=f.name, kind=f.kind, description=f.description)
        )
    for f in pack.factions:  # wars after every faction exists (dst must resolve)
        for other in f.at_war_with:
            events.append(edge_added(src=f.id, rel_type="at_war_with", dst=other))
    for a in pack.actors:
        events.append(
            actor_created(actor_id=a.id, name=a.name, tier=a.tier, role=a.role, aliases=a.aliases)
        )
        if a.faction:
            events.append(edge_added(src=a.id, rel_type="member_of", dst=a.faction))
        if a.location:
            events.append(edge_added(src=a.id, rel_type="located_in", dst=a.location))
    for c in pack.claims:
        events.append(
            claim_recorded(
                claim_id=c.id,
                statement=c.statement,
                subject_refs=c.subject_refs,
                truth=c.truth,
                origin="worldpack",
            )
        )
    for t in pack.threads:
        events.append(
            thread_created(thread_id=t.id, stakes=t.stakes, state=t.state, provenance=t.provenance)
        )
    return events
