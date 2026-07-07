"""History service — procedural seeding (docs/01, 03, 09). Deterministic — seeded RNG, no LLM.

Given a world's manifest + a seed, generate the backstory (dynasties, wars, an era-defining
event) as H-emitted events layered on top of the authored geography. Same pack + a different
seed → a sibling world with different dynasties on identical geography (docs/03). Entity ids
are derived from the seed (not `new_id`) so a seeding replays byte-identically. The generated
NAMES/COUNT/WARS come from the seeded `Rng`, so they diverge across seeds.
"""

from __future__ import annotations

from uro_core.domain.events import (
    DomainEvent,
    actor_created,
    claim_recorded,
    edge_added,
    faction_created,
    history_cause,
    history_seeded,
)
from uro_core.rulesets.rng import Rng
from uro_core.worldpack.models import WorldManifest

_DYNASTIES = (
    "Corvane",
    "Ashmoor",
    "Dunhallow",
    "Verric",
    "Thorne",
    "Blackmere",
    "Halloway",
    "Estmark",
    "Grausel",
    "Vane",
)
_TITLES = ("King", "Queen", "Duke", "Warden", "High Lord", "Reeve")
_RULERS = ("Aldric", "Mira", "Sorel", "Ysolde", "Bram", "Cael", "Nessa", "Roderic")
_CALAMITIES = (
    "a great plague",
    "a border war",
    "a failed harvest",
    "a schism of faith",
    "a drowned fleet",
)


def seed_history(manifest: WorldManifest, rng: Rng) -> list[DomainEvent]:
    """Generate a world's seeded history. Pure: a function of (manifest, seed)."""
    seed = rng.seed
    cause = history_cause("seeding")
    era = manifest.history.seed_era or "shadows"
    events: list[DomainEvent] = [
        history_seeded(
            seed=seed,
            simulated_years=manifest.history.simulate_years,
            era_summary=f"the age of {era}",
            caused_by=cause,
        )
    ]
    dynasties: list[str] = []
    for i in range(2 + rng.die(3)):  # 3..5 dynasties (varies by seed)
        fid = f"f:seed{seed}-d{i}"
        events.append(
            faction_created(
                faction_id=fid,
                name=f"House {rng.choice(_DYNASTIES)}",
                description=f"A dynasty risen in the age of {era}.",
                caused_by=cause,
            )
        )
        rid = f"a:seed{seed}-r{i}"
        events.append(
            actor_created(
                actor_id=rid,
                name=f"{rng.choice(_TITLES)} {rng.choice(_RULERS)}",
                tier=2,
                role="ruler",
                caused_by=cause,
            )
        )
        events.append(edge_added(src=rid, rel_type="rules", dst=fid, caused_by=cause))
        dynasties.append(fid)
    for fid in dynasties:  # some dynasties fall to war (varies by seed)
        if len(dynasties) > 1 and rng.die(2) == 1:
            other = rng.choice([f for f in dynasties if f != fid])
            events.append(edge_added(src=fid, rel_type="at_war_with", dst=other, caused_by=cause))
    events.append(
        claim_recorded(
            claim_id=f"c:seed{seed}-era",
            statement=f"The age of {era} was marked by {rng.choice(_CALAMITIES)}.",
            truth="true",
            origin="history",
            caused_by=cause,
        )
    )
    return events
