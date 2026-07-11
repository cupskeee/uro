"""HOLLOWLOOP — the Vale of Mourn: the authored world, the schedule, the clues, the rule pack.

Everything canonical lives in Uro from genesis: the seven places, the five villagers, the three
items, and the doom thread. ONE world, ONE origin marker; every loop is a real
`fork_branch(world_id, "origin", "loop-NNNN")` off that marker, so the world resets and the
Loopwalker does not.

TWO PUNS THE ENGINE FORCED (both logged as gaps, see GAP_REPORT):

1. THE DOOM VOCABULARY. The brief's own ladder — `looming -> gathering -> imminent` (and
   `warded` for the win) — is REJECTED by the Reaction-Layer grammar: `ThreadState` is the
   closed literal ["dormant","offered","active","resolved","dead"] (domain/events.py:797,
   pinned by worldpack/rules.py:45,114). A rule cannot SET or TEST any other word. Worse, a
   bad pack does not raise — `Engine.react`/`agenda_tick` swallow the ValidationError into a
   warning and the WHOLE pack silently goes dark (engine.py:388-389, 420-421). So the dread
   ladder is punned onto the five words the grammar speaks, and DOOM_WORDS translates back.

2. SEGMENTS ARE DAYS. `world_day` is day-granular (`current_world_time -> int`); there is no
   sub-day clock. The 7 segments of the doomed day are therefore 7 `world_day`s, advanced with
   `engine.agenda_tick(branch, 1)` per beat. Because every loop forks from an origin commit at
   day 0, absolute `world_day` == segment, which is the only reason the rule pack's `world_day`
   conditions (which compare ABSOLUTE days, engines/rules.py:147) can express "as the day wears
   on" at all.
"""

from __future__ import annotations

from typing import Any

from uro_core.domain.events import (
    actor_created,
    edge_added,
    faction_created,
    item_created,
    place_created,
    thread_created,
)

DSN_DEFAULT = "postgresql://uro:uro@localhost:5433/uro"
ORIGIN_REF = "origin"  # the fixed marker every loop forks from (a NAME, not a hash)
PC_ID = "a:pc"
PARTICIPANT = "player-1"

# --- the loop clock: 7 segments == world_day 0..6 --------------------------------------------
SEGMENTS = [
    "dawn",
    "morning",
    "noon",
    "afternoon",
    "dusk",
    "last light",
    "the Fall",
]
DOOM_SEGMENT = 6

# --- the Vale ---------------------------------------------------------------------------------
VALE = "p:vale"
PLACES = [
    (VALE, "The Vale of Mourn", "region"),
    ("p:square", "The Square", "site"),
    ("p:chapel", "The Chapel", "site"),
    ("p:forge", "The Forge", "site"),
    ("p:well", "The Old Well", "site"),
    ("p:manor", "The Manor", "site"),
    ("p:tower", "The Bell Tower", "site"),
]
PLACE_NAMES = {pid: name for pid, name, _kind in PLACES}
VISITABLE = [pid for pid, _n, _k in PLACES if pid != VALE]

# --- the cast: (id, name, tier, role, aliases) -------------------------------------------------
CAST = [
    ("a:aldis", "Elder Aldis", 2, "elder", ["Aldis", "the Elder"]),
    ("a:sela", "Chaplain Sela", 1, "chaplain", ["Sela", "the Chaplain"]),
    ("a:wren", "Wren", 0, "child", ["the child", "the girl at the well"]),
    ("a:bryn", "Bryn the Smith", 1, "smith", ["Bryn", "the Smith"]),
    ("a:harrow", "Harrow the Stranger", 1, "harbinger", ["Harrow", "the Stranger"]),
]
NPC_NAMES = {a: n for a, n, _t, _r, _al in CAST}
VALE_FOLK = "f:vale"

# --- the schedule: actor -> {segment: place} ---------------------------------------------------
# (a NPC is talkable only where and when the schedule puts them; "hidden" = nowhere)
SCHEDULE: dict[str, dict[int, str]] = {
    "a:aldis": {
        0: "p:chapel",
        1: "p:chapel",
        2: "p:chapel",
        3: "p:manor",
        4: "p:manor",
        5: "p:manor",
        6: "p:manor",
    },
    "a:sela": dict.fromkeys(range(7), "p:chapel"),
    "a:wren": {
        0: "p:square",
        1: "p:square",
        2: "p:square",
        3: "p:well",
        4: "p:well",
        5: "p:square",
    },  # seg 6: hidden
    "a:bryn": dict.fromkeys(range(7), "p:forge"),
    "a:harrow": {
        0: "p:square",
        1: "p:well",
        2: "p:well",
        3: "p:tower",
        4: "p:tower",
        5: "p:tower",
        6: "p:tower",
    },
}


def who_is_at(place: str, segment: int) -> list[str]:
    """The NPCs the schedule puts at `place` in `segment` — the game's own read of its own
    authored schedule (Uro has no scheduling concept; see GAP_REPORT)."""
    return sorted(a for a, sched in SCHEDULE.items() if sched.get(segment) == place)


# --- the items --------------------------------------------------------------------------------
TOWER_KEY = "i:tower-key"
ITEMS = [
    (TOWER_KEY, "the tower key", "a:wren"),
    ("i:bell-hammer", "the bell hammer", "a:bryn"),
    ("i:star-chart", "the star-chart", "a:aldis"),
]

# --- the doom thread, punned onto the closed ThreadState literal -------------------------------
DOOM = "t:doom"
DOOM_STATES = {  # the fiction's word -> the only vocabulary the grammar speaks
    "looming": "dormant",  # dawn: unseen. NB dormant threads do NOT reach the narrator.
    "gathering": "offered",  # afternoon: the sky wrongs. `offered` DOES reach the narrator.
    "imminent": "active",  # last light: the air hums. `active` DOES reach the narrator.
    "fallen": "dead",  # the Fall struck. Terminal.
    "warded": "resolved",  # the Sky-Bell rang. The win. Terminal.
}
DOOM_WORDS = {v: k for k, v in DOOM_STATES.items()}

# --- the four keystone clues -------------------------------------------------------------------
# A clue is DISCOVERED when the beat's extractor commits its claim (truth=true, origin=narrator)
# on that loop's branch, AND the game records the clue key in the Codex.
#
# GAP (see GAP_REPORT): the extractor MINTS the claim id (`c:{ulid}`, extraction.py:185) — a
# game cannot choose `c:nature`, and `ProposedClaim` has no id field. So a clue's identity on a
# branch is its exact STATEMENT PROSE, and "does this loop know K1?" is a string match. The
# `key` below is the game's own id; `statement` is the only thing Uro can be asked about.
CLUES: dict[str, dict[str, Any]] = {
    "K1": {
        "id": "c:nature",  # what we WANTED the claim id to be (Uro mints its own)
        "title": "the nature of the Fall",
        "statement": (
            "The Fall is a falling star, not an omen, and it strikes the Vale at last light."
        ),
        "about": ["Elder Aldis"],
        "requires": [],
    },
    "K2": {
        "id": "c:ward",
        "title": "the ward",
        "statement": (
            "The Sky-Bell can ward the Fall if it is rung at the very moment the star strikes."
        ),
        "about": ["Chaplain Sela"],
        "requires": ["K1"],
    },
    "K3": {
        "id": "c:key",
        "title": "the hidden key",
        "statement": "Wren hid the tower key in the old well.",
        "about": ["Wren"],
        "requires": [],
    },
    "K4": {
        "id": "c:timing",
        "title": "the hour of the star",
        "statement": (
            "The star falls at nightfall, and the bell must ring at that hour and no other."
        ),
        "about": ["Harrow the Stranger"],
        "requires": ["K1"],
    },
}
KEYSTONES = ("K1", "K2", "K3", "K4")
CLUE_BY_STATEMENT = {c["statement"]: k for k, c in CLUES.items()}


# --- the Reaction Layer: rising dread, declarative only ----------------------------------------
# Agendas fire on `engine.agenda_tick(branch, 1)` — one per segment. They escalate t:doom and
# spread the villagers' unease. They CANNOT commit the Fall (the action union structurally
# cannot destroy a place, D-33's trust fence) — the Fall is host-authored (loop.py).
RULE_PACK: dict[str, Any] = {
    "rules_api_version": 1,
    "agendas": [
        {
            "id": "dread-gathers",
            "every_days": 1,
            "when": {
                "kind": "all",
                "all": [
                    {"kind": "world_day", "op": ">=", "value": 3},
                    {"kind": "thread_state", "thread": DOOM, "state": DOOM_STATES["looming"]},
                ],
            },
            "then": [{"do": "set_thread_state", "thread": DOOM, "to": DOOM_STATES["gathering"]}],
            "scope": {"thread": DOOM},
        },
        {
            "id": "dread-imminent",
            "every_days": 1,
            "when": {
                "kind": "all",
                "all": [
                    {"kind": "world_day", "op": ">=", "value": 5},
                    {"kind": "thread_state", "thread": DOOM, "state": DOOM_STATES["gathering"]},
                ],
            },
            "then": [{"do": "set_thread_state", "thread": DOOM, "to": DOOM_STATES["imminent"]}],
            "scope": {"thread": DOOM},
        },
        # The villagers' unease. A rumor claim about an on-stage villager is the ONLY channel
        # that carries escalating dread into the narrator's prose: a thread's `stakes` text is
        # fixed at creation and its STATE never reaches the prompt (recall.py:172-176), so
        # gathering-vs-imminent is invisible to the narrator. Rumors are not.
        {
            "id": "unease-spreads",
            "every_days": 1,
            "when": {"kind": "world_day", "op": ">=", "value": 3},
            "then": [
                {
                    "do": "record_rumor",
                    "text": "The villagers say the sky feels wrong today, and the birds have "
                    "gone quiet over the Vale.",
                    "subjects": ["a:sela", "a:bryn"],
                }
            ],
            "scope": {"faction": VALE_FOLK},
        },
        {
            "id": "dread-at-last-light",
            "every_days": 1,
            "when": {"kind": "world_day", "op": ">=", "value": 5},
            "then": [
                {
                    "do": "record_rumor",
                    "text": "The air over the Vale hums, and the well-water shivers in the bucket.",
                    "subjects": ["a:sela", "a:bryn"],
                }
            ],
            "scope": {"faction": VALE_FOLK},
        },
    ],
}


def genesis_events() -> list[Any]:
    """The authored Vale, committed at WorldGenesis via create_world(extra_events=...)."""
    events: list[Any] = []
    for place_id, name, kind in PLACES:
        events.append(place_created(place_id=place_id, name=name, kind=kind))
    events.append(faction_created(faction_id=VALE_FOLK, name="The folk of the Vale"))
    for actor_id, name, tier, role, aliases in CAST:
        events.append(
            actor_created(actor_id=actor_id, name=name, tier=tier, role=role, aliases=list(aliases))
        )
        # membership is what puts a villager inside the rumor rules' faction scope
        events.append(edge_added(src=actor_id, rel_type="member_of", dst=VALE_FOLK))
    for item_id, name, owner in ITEMS:
        events.append(item_created(item_id=item_id, name=name, owner_ref=owner))
    events.append(
        thread_created(
            thread_id=DOOM,
            stakes="The Fall will end the Vale of Mourn at last light.",
            state=DOOM_STATES["looming"],
        )
    )
    return events
