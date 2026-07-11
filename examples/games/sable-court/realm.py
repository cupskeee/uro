"""The realm of Karsis — authored canon, seeded into Uro as `extra_events` at `create_world`.

Everything the world should own lives HERE as Uro events: the Houses (factions), the court
(actors with deliberately confusable names + authored aliases), the holdings (places, ownership
as `owns` edges), the plots (threads, mixed states), the gossip web (`knows` edges — the rails
belief propagation runs on), and the Reaction-Layer `RULE_PACK`.

The numeric ledger (strength/gold/tension) is NOT here — Uro structurally cannot own it; it
lives in `ledger.py`, and every field of it is a refusal-log entry.
"""

from __future__ import annotations

import frictionlog
from uro_core.domain.events import (
    DomainEvent,
    actor_created,
    claim_recorded,
    edge_added,
    faction_created,
    item_created,
    place_created,
    thread_created,
)

# --- ids (a: actor, p: place, f: faction, t: thread, i: item) ---

SPYMASTER = "a:spymaster"  # the PC; minted by start_campaign(new_pc_id=...), not seeded here

FACTIONS: list[tuple[str, str, str]] = [
    ("f:crown", "The Crown of Karsis", "the ailing King's own house and household"),
    ("f:vaelric", "House Vaelric", "martial, land-hungry, the border march's swords"),
    ("f:corvane", "House Corvane", "coastal salt-and-shipping wealth"),
    ("f:dellmoor", "House Dellmoor", "old blood, empty granaries, fading fast"),
    ("f:argent", "The Argent Ledger", "the bankers' guild; the Crown owes it everything"),
    ("f:ashen", "The Ashen Veil", "a hidden cult of the Old Flame"),
    # The umbrella faction is a WORKAROUND (logged in sable_court.py): a Reaction-Layer rule's
    # faction scope covers only that faction + its members, so a cross-House action (a war edge
    # between two Houses) is only in-jurisdiction if both Houses are members of ONE faction.
    ("f:court", "The Sable Court", "the whole court of Karsis — every great House at the Throne"),
]

# (actor_id, name, tier, role, aliases). Tier is 0..3 — TASK.md asks for a tier-4 King, but the
# engine caps tiers at 3 (ActorCreatedPayload: ge=0 le=3); logged as a gap at import time below.
ACTORS: list[tuple[str, str, int, str, list[str]]] = [
    ("a:halric", "King Halric", 3, "King of Karsis, dying", ["the King", "Halric"]),
    (
        "a:aldric-vaelric",
        "Aldric Vaelric",
        3,
        "Lord Marshal",
        ["the Marshal", "Lord Vaelric", "Aldric"],
    ),
    (
        "a:aldrice-corvane",
        "Aldrice Corvane",
        3,
        "Mistress of Salt",
        ["Lady Corvane", "Aldrice"],
    ),
    ("a:aldous-dellmoor", "Aldous Dellmoor", 2, "Lord of Oldkeep", ["Lord Dellmoor", "Aldous"]),
    ("a:queen-issolde", "Queen Issolde", 2, "the King's second wife", ["the Queen", "Issolde"]),
    ("a:prince-edric", "Prince Edric", 2, "sickly heir presumptive", ["the Prince", "Edric"]),
    ("a:maren-argent", "Maren Argent", 2, "Guildmistress of the Ledger", ["the Guildmistress"]),
    ("a:brother-sorrel", "Brother Sorrel", 2, "hierophant of the Veil", ["the Hierophant"]),
    (
        "a:aldric-younger",
        "Aldric the Younger",
        1,
        "the Marshal's nephew",
        ["the Younger"],
    ),
    ("a:ser-garret", "Ser Garret", 1, "knight sworn to Vaelric", ["Garret"]),
    ("a:ser-garrick", "Ser Garrick", 1, "knight sworn to Corvane", ["Garrick"]),
    ("a:ser-gareth", "Ser Gareth", 1, "knight sworn to Dellmoor", ["Gareth"]),
    ("a:lys", "Lys", 1, "the Spymaster's whisperer", ["the whisperer"]),
    ("a:captain-hurn", "Captain Hurn", 1, "captain of the border levies", ["Hurn"]),
    ("a:mother-vey", "Mother Vey", 1, "temple crone of the Veil", ["the crone"]),
    ("a:tobbin", "Tobbin", 0, "Vaelric levy retainer", []),
    ("a:willem", "Willem", 0, "Corvane caravan guard", []),
]

# (place_id, name, kind, description)
PLACES: list[tuple[str, str, str, str]] = [
    ("p:capital", "Karsis City", "settlement", "the capital, coiled around the Sable Throne"),
    ("p:border-march", "The Border March", "region", "Vaelric's hard country of levies and keeps"),
    ("p:saltport", "Saltport", "settlement", "Corvane's harbor of salt-pans and quiet ledgers"),
    ("p:oldkeep", "Oldkeep", "site", "Dellmoor's crumbling seat above empty granaries"),
    ("p:temple-district", "The Temple District", "site", "incense, bells, and the Veil beneath"),
    ("p:silvermines", "The Silvermines", "site", "the Argent Ledger's collateral made rock"),
    ("p:greyfen", "The Greyfen", "region", "Corvane's fog-bound marsh road to the interior"),
]

# faction → owned places (ownership encoded as `owns` edges, readable via list_edges)
OWNERSHIP: dict[str, list[str]] = {
    "f:crown": ["p:capital"],
    "f:vaelric": ["p:border-march"],
    "f:corvane": ["p:saltport", "p:greyfen"],
    "f:dellmoor": ["p:oldkeep"],
    "f:argent": ["p:silvermines"],
    "f:ashen": ["p:temple-district"],
}

MEMBERSHIP: dict[str, list[str]] = {
    "f:crown": ["a:halric", "a:queen-issolde", "a:prince-edric", "a:lys", "a:captain-hurn"],
    "f:vaelric": ["a:aldric-vaelric", "a:aldric-younger", "a:ser-garret", "a:tobbin"],
    "f:corvane": ["a:aldrice-corvane", "a:ser-garrick", "a:willem"],
    "f:dellmoor": ["a:aldous-dellmoor", "a:ser-gareth"],
    "f:argent": ["a:maren-argent"],
    "f:ashen": ["a:brother-sorrel", "a:mother-vey"],
    # every House sits at the Sable Court (the umbrella-scope workaround — see FACTIONS)
    "f:court": ["f:crown", "f:vaelric", "f:corvane", "f:dellmoor", "f:argent", "f:ashen"],
}

# The gossip web: `knows` edges are directed rails for belief propagation (src tells dst).
KNOWS: list[tuple[str, str]] = [
    ("a:willem", "a:ser-garrick"),  # a caravan guard reports to his knight
    ("a:ser-garrick", "a:aldrice-corvane"),  # the knight reports to his lady
    ("a:aldrice-corvane", "a:maren-argent"),  # salt money talks to guild money
    ("a:maren-argent", "a:halric"),  # the Guildmistress has the King's ear
    ("a:tobbin", "a:ser-garret"),
    ("a:ser-garret", "a:aldric-vaelric"),
    ("a:aldric-vaelric", "a:halric"),
    ("a:ser-gareth", "a:aldous-dellmoor"),
    ("a:aldous-dellmoor", "a:maren-argent"),
    ("a:brother-sorrel", "a:mother-vey"),
    ("a:mother-vey", "a:queen-issolde"),  # the cult's finger inside the palace
    ("a:queen-issolde", "a:halric"),
    ("a:lys", SPYMASTER),  # the whisperer reports to the player
]

# (thread_id, stakes, initial state) — ≥12 plots, mixed states, designed to grow to dozens.
THREADS: list[tuple[str, str, str]] = [
    ("t:succession", "The King is dying without a clear heir.", "dormant"),
    ("t:vaelric-corvane-feud", "Blood and salt: Vaelric and Corvane circle each other.", "dormant"),
    ("t:ashen-heresy", "The Ashen Veil whispers the old rites beneath the temple.", "active"),
    ("t:argent-debt", "The Crown owes the Argent Ledger more than it can repay.", "active"),
    ("t:border-war", "The border levies mass along the march.", "dormant"),
    ("t:royal-betrothal", "A marriage could bind two Houses to the Throne.", "offered"),
    ("t:poison-plot", "Someone salts the King's cup, slowly.", "dormant"),
    ("t:saltport-smuggling", "Contraband moves through Saltport under Corvane seals.", "active"),
    ("t:crown-illness", "The King's strength fails by the week.", "active"),
    ("t:dellmoor-decline", "Oldkeep's granaries empty; House Dellmoor fades.", "active"),
    ("t:cult-infiltration", "The Veil has a finger in every House.", "dormant"),
    ("t:tax-revolt", "The river towns refuse the levy.", "dormant"),
]

# (item_id, name, owner) — real, owned items so Chronicler loot has something true to move
# (and something protected to be refused: the Marshal's letters).
ITEMS: list[tuple[str, str, str]] = [
    ("i:vaelric-warbanner", "the Vaelric war-banner", "a:tobbin"),
    ("i:salt-ledger", "the Saltport smuggling ledger", "a:willem"),
    ("i:marshals-letters", "the Marshal's private letters", "a:aldric-vaelric"),
]

# A seeded claim the R3 agenda can `spread_belief` about (spread_belief refuses a nonexistent
# claim, so the heresy must already be on record as testimony).
HERESY_CLAIM_ID = "c:seed-heresy"


def seed_events() -> list[DomainEvent]:
    """The authored realm as a flat list of seed events for `create_world(extra_events=...)`."""
    events: list[DomainEvent] = []
    for fid, name, desc in FACTIONS:
        events.append(faction_created(faction_id=fid, name=name, description=desc))
    for aid, name, tier, role, aliases in ACTORS:
        events.append(actor_created(actor_id=aid, name=name, tier=tier, role=role, aliases=aliases))
    for pid, name, kind, desc in PLACES:
        events.append(
            place_created(place_id=pid, name=name, kind=kind, description=desc)  # type: ignore[arg-type]
        )
    for faction, members in MEMBERSHIP.items():
        for member in members:
            events.append(edge_added(src=member, rel_type="member_of", dst=faction))
    for faction, places in OWNERSHIP.items():
        for place in places:
            events.append(edge_added(src=faction, rel_type="owns", dst=place))
    for src, dst in KNOWS:
        events.append(edge_added(src=src, rel_type="knows", dst=dst))
    for tid, stakes, state in THREADS:
        events.append(thread_created(thread_id=tid, stakes=stakes, state=state))
    for iid, name, owner in ITEMS:
        events.append(item_created(item_id=iid, name=name, owner_ref=owner))
    events.append(
        claim_recorded(
            claim_id=HERESY_CLAIM_ID,
            statement="The Old Flame never died; it waits beneath the temple ash.",
            subject_refs=["a:brother-sorrel"],
            truth="unknown",
            origin="author",
        )
    )
    return events


# TASK.md §2.1 names King Halric "tier 4"; the engine's tier scale is 0..3 (T3 = agent), so a
# fourth tier does not exist. Logged once, at realm-definition time.
frictionlog.gap(
    gap="Seed King Halric at tier 4 (TASK.md's scale for a monarch)",
    happened="ActorCreatedPayload validates tier with ge=0, le=3 — tier 4 raises ValidationError",
    workaround="seeded at tier 3 (same protection ceiling as any T2+ actor)",
    severity="cosmetic",
    needs="nothing (docs mismatch) — or a wider authored-tier scale if games need finer canon rank",
    evidence="uro_core/domain/events.py:153 (tier: ge=0 le=3) vs realm.py ACTORS a:halric",
)

# Found while authoring r4a below: a faction-scoped rule can NEVER create_thread — the new
# thread id is not among the faction's members, so the gauntlet drops it. The only way to mint
# a thread from a rule is a thread-scope naming the not-yet-existing id (self-scoping).
frictionlog.gap(
    gap="A faction's rule should be able to spawn a plot about that faction "
    "(create_thread under faction scope)",
    happened="the gauntlet checks the new thread id against the scope's allowed refs; a "
    "faction scope allows only the faction + its members, so the create is silently dropped — "
    "create_thread works ONLY with scope {thread: <the-new-id>}, which reads as a rule "
    "scoped to a thing that does not exist yet",
    workaround="r4a self-scopes to the thread it creates (works, reads oddly)",
    severity="annoyance",
    needs="allow create_thread under faction/place scope (the thread ABOUT that jurisdiction), "
    "or document self-scoping as the pattern",
    evidence="uro_core/engines/rules_gauntlet.py:73 (a.thread not in allowed); realm.py r4a",
)


# --- The Reaction Layer rule pack (docs/17) — everything the grammar CAN express, shipped. ---
#
# R1/R2/R3/R4/R5/R6 from TASK.md §2.4. Notes at the wrinkles:
# - r4 must be TWO rules (create_thread needs thread-scope naming the new id; the rumor needs
#   faction-scope) — the §3 target-2 scope split, demonstrated live.
# - r5 ships twice on purpose: the naive faction-scoped form (silently dropped by the gauntlet —
#   its subject is outside f:vaelric's jurisdiction) and the umbrella-scoped form that lands.
# - r6 is authored and correct but CANNOT FIRE at runtime: the only runtime ActorDied path is the
#   Chronicler, whose trust ceiling refuses to kill a T3 King — the death collision, asserted.
RULE_PACK: dict[str, object] = {
    "rules_api_version": 1,
    "rules": [
        {
            "id": "r1-feud-wakes-on-death",
            "trigger": {"event": "ActorDied"},
            "when": {
                "kind": "thread_state",
                "thread": "t:vaelric-corvane-feud",
                "state": "dormant",
            },
            "then": [
                {"do": "set_thread_state", "thread": "t:vaelric-corvane-feud", "to": "active"}
            ],
            "scope": {"thread": "t:vaelric-corvane-feud"},
        },
        {
            "id": "r4a-alliance-spawns-counterplot",
            "trigger": {"event": "EdgeAdded", "where": {"rel_type": "allied_with"}},
            "then": [
                {
                    "do": "create_thread",
                    "thread": "t:counter-pact",
                    "stakes": "A counter-pact forms in the shadows against the new alliance.",
                }
            ],
            "scope": {"thread": "t:counter-pact"},
        },
        {
            "id": "r4b-alliance-stirs-the-guild",
            "trigger": {"event": "EdgeAdded", "where": {"rel_type": "allied_with"}},
            "then": [
                {
                    "do": "record_rumor",
                    "text": "The court murmurs of a pact sealed behind the Sable Throne, "
                    "and the Ledger reprices every debt by dawn.",
                    "subjects": ["a:maren-argent"],
                }
            ],
            "scope": {"faction": "f:argent"},
        },
        {
            "id": "r6-succession-opens-on-kings-death",
            "trigger": {"event": "ActorDied", "where": {"actor_id": "a:halric"}},
            "when": {"kind": "thread_state", "thread": "t:succession", "state": "dormant"},
            "then": [{"do": "set_thread_state", "thread": "t:succession", "to": "active"}],
            "scope": {"thread": "t:succession"},
        },
    ],
    "agendas": [
        {
            "id": "r2-war-breeds-rumor",
            "every_days": 20,
            "when": {
                "kind": "edge_exists",
                "src": "f:vaelric",
                "rel": "at_war_with",
                "dst": "f:corvane",
            },
            "then": [
                {
                    "do": "record_rumor",
                    "text": "They say Vaelric riders burned the Corvane granaries "
                    "on the salt road.",
                    "subjects": ["a:aldrice-corvane"],
                }
            ],
            "scope": {"faction": "f:corvane"},
        },
        {
            "id": "r3-heresy-spreads",
            "every_days": 30,
            "when": {"kind": "thread_state", "thread": "t:ashen-heresy", "state": "active"},
            "then": [
                {
                    "do": "spread_belief",
                    "claim": HERESY_CLAIM_ID,
                    "witnesses": ["a:brother-sorrel", "a:mother-vey"],
                }
            ],
            "scope": {"faction": "f:ashen"},
        },
        {
            "id": "r5a-border-war-umbrella",
            "every_days": 30,
            "when": {"kind": "world_day", "op": ">", "value": 90},
            "then": [
                {"do": "add_edge", "src": "f:vaelric", "rel": "at_war_with", "dst": "f:dellmoor"},
                {"do": "add_edge", "src": "f:dellmoor", "rel": "at_war_with", "dst": "f:vaelric"},
            ],
            # scoped to the umbrella faction so BOTH Houses are in jurisdiction (the workaround)
            "scope": {"faction": "f:court"},
        },
        {
            "id": "r5b-border-war-naive",
            "every_days": 30,
            "when": {"kind": "world_day", "op": ">", "value": 90},
            "then": [
                {
                    "do": "record_rumor",
                    "text": "Dellmoor's walls will not hold a season against the march levies.",
                    "subjects": ["a:aldous-dellmoor"],
                }
            ],
            # DELIBERATELY mis-scoped the way a naive author would: a:aldous-dellmoor is not a
            # member of f:vaelric, so the gauntlet drops this SILENTLY. Asserted absent.
            "scope": {"faction": "f:vaelric"},
        },
    ],
}
