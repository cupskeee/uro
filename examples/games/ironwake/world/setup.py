"""Seed the Marches — the authored world IRONWAKE plays in (TASK inc 0, brief section 3.2).

Tier design is deliberate (the trust model bites exactly here):
- mercs are tier 1, NOT PCs -> they can really permadie via a Chronicler bundle;
- rank-and-file enemies are tier 0 -> they really die;
- Captain Vorlund (and Warlord Skane) are tier 2 -> the protection ceiling: a bundle can never
  kill or loot them, only make the world SAY they fell;
- the Quartermaster is the campaign PC (tier 2 + is_pc) -> narration avatar, never a combatant.

Distance IS the knows-graph (brief section 3.5). Belief propagation decays 0.9 -> 0.495 ->
0.272 and then hits the engine's 0.2 floor, so the chain below gives:
    surviving merc (eyewitness, 0.9 "is certain")
      -> Mira, Ironwake Hold   (1 hop, 0.495 "believes")        near: confident
      -> Corin, Duns-Ferry     (2 hops, 0.272 "has heard a rumor")  far: hedged
      -> Odo, Greywater        (3 hops, 0.149 < floor -> HEARS NOTHING)
That third town going silent is an engine finding, not a bug in this file — distill_outcome
hardcodes the decay parameters, so a game cannot tune its rumor horizon. Logged below.
"""

from __future__ import annotations

from dataclasses import dataclass

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    DomainEvent,
    actor_created,
    edge_added,
    faction_created,
    item_created,
    place_created,
    thread_created,
)
from uro_core.timeline.models import Campaign, World

from ironwake import frictionlog
from ironwake.game.company import Company, Merc
from ironwake.world.rules import IRONWAKE_RULE_PACK, THEATER

VORLUND = "a:vorlund"
VORLUNDS_BLADE = "i:vorlunds-blade"
SKANE = "a:warlord-skane"
MIRA = "a:npc-mira"  # tavern-keeper, Ironwake Hold (home — 1 hop from a witness)
CORIN = "a:npc-corin"  # crier, Duns-Ferry (2 hops)
ODO = "a:npc-odo"  # tavern drunk, Greywater (3 hops — beyond the engine's rumor horizon)

STARTING_MERCS: tuple[tuple[str, str, str], ...] = (
    ("a:merc-gerhardt", "Gerhardt", "Sergeant"),
    ("a:merc-elke", "Elke", "Crossbow"),
    ("a:merc-joss", "Joss", "Skirmisher"),
    ("a:merc-petra", "Petra", "Sawbones"),
)

TOWNS: tuple[tuple[str, str, str], ...] = (
    ("p:ironwake-hold", "Ironwake Hold", "home of the company; Mira's taproom"),
    ("p:duns-ferry", "Duns-Ferry", "the river crossing; Corin cries the news"),
    ("p:greywater", "Greywater", "the far fen town; Odo props up the bar"),
)

SITES: tuple[tuple[str, str], ...] = (
    ("p:west-road", "the West Road"),
    ("p:silent-mill", "the Silent Mill"),
    ("p:tollbridge", "the Tollbridge"),
    ("p:red-camp", "the Red Camp"),
)


@dataclass
class Marches:
    """Handles the rest of the game passes around."""

    world: World
    branch_id: str
    campaign: Campaign
    company: Company


def seed_events() -> list[DomainEvent]:
    """The authored genesis of the Marches (emitter S via create_world extra_events)."""
    events: list[DomainEvent] = []
    for place_id, name, description in TOWNS:
        events.append(
            place_created(place_id=place_id, name=name, kind="settlement", description=description)
        )
    for place_id, name in SITES:
        events.append(place_created(place_id=place_id, name=name, kind="site"))

    events.append(faction_created(faction_id="f:ironwake", name="The Ironwake Company"))
    events.append(faction_created(faction_id="f:red-band", name="The Red Band"))
    # The THEATER meta-faction exists ONLY to satisfy the Reaction-Layer scope fence: a rule
    # adding an at_war_with edge must contain BOTH factions in one scope, and faction scope =
    # member_of members. Scaffolding demanded by the grammar, not by the fiction (gap logged
    # in seed_world below).
    events.append(faction_created(faction_id=THEATER, name="The War in the Marches"))
    events.append(edge_added(src="f:ironwake", rel_type="member_of", dst=THEATER))
    events.append(edge_added(src="f:red-band", rel_type="member_of", dst=THEATER))

    events.append(
        thread_created(
            thread_id="t:red-band-war",
            stakes="The Red Band is bleeding the Marches white.",
            state="dormant",
        )
    )
    events.append(
        thread_created(
            thread_id="t:vorlund-bounty",
            stakes="Warlord Skane wants Captain Vorlund dead — proof, not stories.",
            state="offered",
        )
    )

    # --- the named, protected enemy officer (tier 2) and his lootable blade ---
    events.append(
        actor_created(
            actor_id=VORLUND,
            name="Captain Vorlund",
            tier=2,
            role="Red Band officer",
            aliases=["Vorlund", "the Red Captain"],
        )
    )
    events.append(edge_added(src=VORLUND, rel_type="member_of", dst="f:red-band"))
    events.append(
        item_created(item_id=VORLUNDS_BLADE, name="Vorlund's blade", owner_ref=VORLUND, kind="arm")
    )
    # --- the employer (tier 2 — protected canon, never a combatant) ---
    events.append(
        actor_created(
            actor_id=SKANE,
            name="Warlord Skane",
            tier=2,
            role="warlord of Ironwake Hold",
            aliases=["Skane", "the warlord"],
        )
    )
    events.append(edge_added(src=SKANE, rel_type="member_of", dst="f:ironwake"))
    events.append(edge_added(src=SKANE, rel_type="located_in", dst="p:ironwake-hold"))

    # --- the starting roster: tier 1, NOT PCs, so they can really permadie ---
    for actor_id, name, cls in STARTING_MERCS:
        events.append(
            actor_created(actor_id=actor_id, name=name, tier=1, role=f"Ironwake {cls.lower()}")
        )
        events.append(edge_added(src=actor_id, rel_type="member_of", dst="f:ironwake"))

    # --- town NPCs (tier 1 rumor carriers) + the knows DISTANCE-CHAIN (module docstring) ---
    for npc_id, name, role, town in (
        (MIRA, "Mira", "tavern-keeper of Ironwake Hold", "p:ironwake-hold"),
        (CORIN, "Corin", "crier of Duns-Ferry", "p:duns-ferry"),
        (ODO, "Odo", "tavern drunk of Greywater", "p:greywater"),
    ):
        events.append(actor_created(actor_id=npc_id, name=name, tier=1, role=role))
        events.append(edge_added(src=npc_id, rel_type="located_in", dst=town))
    events.append(edge_added(src=MIRA, rel_type="member_of", dst="f:ironwake"))
    for actor_id, _, _ in STARTING_MERCS:
        events.append(edge_added(src=actor_id, rel_type="knows", dst=MIRA))
    events.append(edge_added(src=MIRA, rel_type="knows", dst=CORIN))
    events.append(edge_added(src=CORIN, rel_type="knows", dst=ODO))
    return events


async def seed_world(store: PostgresEventStore, *, season_seed: int) -> Marches:
    """Create the world + campaign. The world name carries the seed so re-runs are separate
    worlds on one DB (Uro has no 'drop world' surface — a re-run simply seeds afresh)."""
    world = await store.create_world(
        f"The Marches (season {season_seed})",
        tone=["grim", "muddy", "mercenary"],
        rule_pack=IRONWAKE_RULE_PACK,
        extra_events=seed_events(),
    )
    branch = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        branch,
        participant_id="player-1",  # matches `uro serve`'s first-token participant (uro.py)
        new_pc_name="the Quartermaster",
    )
    frictionlog.gap(
        gap="author an at_war_with edge between two factions from a downtime agenda",
        happened=(
            "the rules gauntlet drops add_edge unless BOTH endpoints are inside the rule's one "
            "scope, and a faction scope only contains the faction + its member_of members "
            "(rules_gauntlet._scope_refs) — two belligerent factions share no natural scope"
        ),
        workaround=(
            "seeded a fictional meta-faction f:the-marches and made both belligerents member_of "
            "it, purely to satisfy the fence (world/setup.py seed_events)"
        ),
        severity="annoyance",
        needs=(
            "either multi-ref scopes ({factions: [a, b]}) or edge actions scoped by EITHER "
            "endpoint's jurisdiction"
        ),
        evidence="world/rules.py agenda-war-drums + world/setup.py THEATER seeding",
    )
    # TASK section C's "note in passing" targets, for the record: entity resolution
    # (canonical-name + alias only) WAS touched — Vorlund/Skane carry authored aliases and every
    # game ref is an exact actor id precisely so nothing depends on name matching; place-state-
    # in-narrator and auto-XP were NOT hit (sites never change state; mercs don't progress).
    frictionlog.gap(
        gap="(noted in passing) refer to actors loosely without authoring aliases",
        happened=(
            "entity resolution is canonical-name + alias only; 'the Red Captain'/'the warlord' "
            "resolve solely because setup.py authored them as aliases, and the game sidesteps "
            "resolution everywhere else by using exact actor ids in bundles and events"
        ),
        workaround="authored aliases at seed time; exact ids in every bundle ref",
        severity="cosmetic",
        needs="nothing for this game; the embedding entity_index remains correctly deferred",
        evidence="world/setup.py seed_events (aliases=); chronicle.py enemy_ids_for (exact ids)",
    )
    company = Company(roster=[Merc(actor_id=a, name=n, cls=c) for a, n, c in STARTING_MERCS])
    return Marches(world=world, branch_id=branch, campaign=campaign, company=company)


async def describe_world(store: PostgresEventStore, branch_id: str) -> str:
    """Inc-0's verification read-back: the world exists, tiers are right, chains are wired."""
    lines: list[str] = []
    places = await store.list_places(branch_id)
    lines.append(f"PLACES ({len(places)}):")
    lines.extend(f"  {p.place_id:18} {p.name} [{p.kind}]" for p in places)
    actors = await store.list_actors(branch_id)
    lines.append(f"ACTORS ({len(actors)}):")
    lines.extend(
        f"  {a.actor_id:20} {a.name:18} T{a.tier} {a.role}"
        + (f"  aka {a.aliases}" if a.aliases else "")
        for a in actors
    )
    knows = await store.list_edges(branch_id, "knows")
    lines.append(f"KNOWS-CHAIN ({len(knows)} edges — rumor distance):")
    lines.extend(f"  {e.src} -> {e.dst}" for e in knows)
    threads = await store.list_threads(branch_id)
    lines.append(f"THREADS ({len(threads)}):")
    lines.extend(f"  {t.thread_id:18} [{t.state}] {t.stakes}" for t in threads)
    return "\n".join(lines)
