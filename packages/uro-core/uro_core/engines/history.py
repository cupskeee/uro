"""History service — procedural seeding (docs/01, 03, 09). Deterministic — seeded RNG, no LLM.

Given a world's manifest + a seed, generate the backstory (power groups and their leaders,
conflicts between them, an era-defining event) as H-emitted events layered on top of the authored
geography, in the pack's own words (`[history.vocabulary]`, D-97). Same pack + a different seed →
a sibling world with a different past on identical geography (docs/03). Entity ids
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
from uro_core.errors import PackError
from uro_core.rulesets.rng import Rng
from uro_core.worldpack.models import WorldManifest


def seed_history(manifest: WorldManifest, rng: Rng) -> list[DomainEvent]:
    """Generate a world's seeded history. Pure: a function of (manifest, seed). Its words are the
    pack's own (`[history.vocabulary]`, D-97): the engine has none, so a pack without them can't
    be seeded."""
    vocab = manifest.history.vocabulary
    if vocab is None:
        raise PackError(
            f"{manifest.name!r} can't be seeded: its [history] has no vocabulary (groups, leaders, "
            "calamities). The engine ships no history of its own; the pack names what its past "
            "is made of."
        )
    seed = rng.seed
    cause = history_cause("seeding")
    era = f"the age of {manifest.history.seed_era}" if manifest.history.seed_era else "its past"
    events: list[DomainEvent] = [
        history_seeded(
            seed=seed,
            simulated_years=manifest.history.simulate_years,
            era_summary=era,
            caused_by=cause,
        )
    ]
    groups: list[str] = []
    for i in range(2 + rng.die(3)):  # 3..5 groups (varies by seed)
        fid = f"f:seed{seed}-d{i}"
        events.append(
            faction_created(
                faction_id=fid,
                name=vocab.group_name.replace("{name}", rng.choice(vocab.groups)),
                description=vocab.group_description.replace("{era}", era),
                caused_by=cause,
            )
        )
        rid = f"a:seed{seed}-r{i}"
        title = rng.choice(vocab.titles) if vocab.titles else ""
        leader = rng.choice(vocab.leaders)
        events.append(
            actor_created(
                actor_id=rid,
                name=f"{title} {leader}" if title else leader,
                tier=2,
                role=vocab.leader_role,
                caused_by=cause,
            )
        )
        events.append(edge_added(src=rid, rel_type="rules", dst=fid, caused_by=cause))
        groups.append(fid)
    for fid in groups:  # some groups fall into conflict (varies by seed)
        if len(groups) > 1 and rng.die(2) == 1:
            other = rng.choice([f for f in groups if f != fid])
            events.append(edge_added(src=fid, rel_type="at_war_with", dst=other, caused_by=cause))
    events.append(
        claim_recorded(
            claim_id=f"c:seed{seed}-era",
            statement=f"{era[0].upper()}{era[1:]} was marked by {rng.choice(vocab.calamities)}.",
            truth="true",
            origin="history",
            caused_by=cause,
        )
    )
    return events
