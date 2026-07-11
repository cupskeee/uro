"""THE SEVENTH VAULT — the authored world: geography, cast, crew, threads, the prize.

Everything canonical lives in Uro from genesis: the five vault layers, the House Guard, the
Warden (tier 3, protected canon), the tier-0/1 guards the Chronicler CAN kill, the alarm and
score threads, and the Heart of the Seventh Vault. The crew are AUTHORED actors adopted as PCs
(`adopt_actor_id`) so their ids are stable across runs — the rule pack and the manifest can name
them.

THE STATE PUN (gap G: closed ThreadState vocabulary). The heist wants
    t:alarm  calm -> suspicious -> alerted -> lockdown
    t:score  pending -> prize-taken -> escaped | betrayed
but the Reaction-Layer grammar only speaks Literal["dormant","offered","active","resolved",
"dead"] (domain/events.py:797, pinned by worldpack/rules.py:45,115) — a rule cannot SET or
TEST any other word (pydantic literal_error, captured). Authored `thread_created` would happily
carry state="calm" (domain/events.py:890 takes `state: str`), but then no rule could ever touch
it. So the alarm/score live in the five-state lifecycle vocabulary and the GAME carries the
translation table below. That table is itself friction evidence: the semantics of "offered"
(= suspicious) exist only in game code.
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
from uro_core.rulesets import registry
from uro_core.rulesets.base import CharSpec
from uro_core.rulesets.rng import Rng

DSN_DEFAULT = "postgresql://uro:uro@localhost:5433/uro"

# --- the five layers, in infiltration order -------------------------------------------------
LAYERS = [
    ("p:outer-gate", "The Outer Gate"),
    ("p:gallery", "The Gallery"),
    ("p:security-hub", "The Security Hub"),
    ("p:antechamber", "The Antechamber"),
    ("p:seventh-vault", "The Seventh Vault"),
]

# --- the state pun: heist vocabulary <-> the grammar's closed ThreadState vocabulary ---------
ALARM_THREAD = "t:alarm"
SCORE_THREAD = "t:score"

ALARM_STATES = {  # heist word -> the only vocabulary rules may speak
    "calm": "dormant",
    "suspicious": "offered",
    "alerted": "active",
    "lockdown": "dead",
}
# NOTE: TASK Section 3 names the fourth score state "caught"; it ships as "betrayed" because
# the TASK's own Stage 7 / DoD mandate exactly two endings — clean escape and double-cross —
# and in neither is anyone captured. No committed state records guard-capture (disclosed in
# GAP_REPORT.md's Summary); the abandoned crew's fate lives in the committed legend rumor.
SCORE_STATES = {
    "pending": "dormant",
    "prize-taken": "active",
    "escaped": "resolved",
    "betrayed": "dead",
}
ALARM_WORDS = {v: k for k, v in ALARM_STATES.items()}
SCORE_WORDS = {v: k for k, v in SCORE_STATES.items()}

# --- the crew: (role, server token, authored actor id, name, ability emphasis) ---------------
# NOTE the participant_id is NOT the token: `uro serve` maps tokens POSITIONALLY to
# player-1..player-N (uro_cli/main.py:486) — a gap logged in host.py. Order here IS the token
# order passed to `uro serve` AND the WS join order, so participant player-(i+1) == CREW[i].
CREW = [
    ("cracksman", "crew-cracksman", "a:vesna", "Vesna", {"DEX": 16, "INT": 16}),
    ("face", "crew-face", "a:doran", "Doran", {"CHA": 16, "WIS": 12}),
    ("ghost", "crew-ghost", "a:sable", "Sable", {"DEX": 17, "WIS": 14}),
    ("muscle", "crew-muscle", "a:brakk", "Brakk", {"STR": 16, "CON": 16}),
]
CREW_ACTORS = [c[2] for c in CREW]
CREW_FACTION = "f:crew"
GUARD_FACTION = "f:house-guard"

WARDEN = "a:warden"
GUARD_FALLEN = "a:guard-7"  # tier 0 — the Chronicler may kill this one
GUARD_WITNESS = "a:guard-9"  # tier 1 — survives the skirmish and carries the rumor
GUARD_CELLAR = "a:guard-11"  # tier 0 — dies unwitnessed in the betrayal ending
TAPSTER = "a:tapster"  # tier 0 — one `knows` hop from the witness: rumor decay proof

PRIZE = "i:prize"
KEYRING = "i:keyring"

RULESET_ID = "uro-basic"


def crew_sheet(emphasis: dict[str, int]) -> dict[str, Any]:
    """A deterministic uro-basic d20 sheet (abilities/hp/ac) built THROUGH the ruleset."""
    ruleset = registry.resolve(RULESET_ID, "")
    return dict(ruleset.new_character(CharSpec(data={"abilities": emphasis}), Rng(0)))


def genesis_events() -> list[Any]:
    """The authored world, committed at WorldGenesis via create_world(extra_events=...)."""
    events: list[Any] = []
    for place_id, name in LAYERS:
        events.append(place_created(place_id=place_id, name=name))
    events.append(faction_created(faction_id=GUARD_FACTION, name="The House Guard"))
    events.append(faction_created(faction_id=CREW_FACTION, name="The Crew of the Seventh Vault"))
    events.append(
        actor_created(
            actor_id=WARDEN,
            name="Warden Kessler",
            tier=3,
            role="vault-warden",
            aliases=["the Warden", "Kessler"],
        )
    )
    events.append(
        actor_created(actor_id=GUARD_FALLEN, name="Guardsman Ott", tier=0, role="house-guard")
    )
    events.append(
        actor_created(actor_id=GUARD_WITNESS, name="Guardswoman Reyla", tier=1, role="house-guard")
    )
    events.append(
        actor_created(actor_id=GUARD_CELLAR, name="Cellar-watch Umble", tier=0, role="house-guard")
    )
    events.append(
        actor_created(actor_id=TAPSTER, name="Tapster Hyle", tier=0, role="tavern-keeper")
    )
    for guard in (WARDEN, GUARD_FALLEN, GUARD_WITNESS, GUARD_CELLAR):
        events.append(edge_added(src=guard, rel_type="member_of", dst=GUARD_FACTION))
    # the rumor lattice: the surviving witness gossips to the tapster (per-hop decay proof)
    events.append(edge_added(src=GUARD_WITNESS, rel_type="knows", dst=TAPSTER))
    # the crew — authored so ids are stable; adopted as PCs by the host
    for _role, _token, actor_id, name, _emph in CREW:
        events.append(actor_created(actor_id=actor_id, name=name, tier=2, role="thief", aliases=[]))
        events.append(edge_added(src=actor_id, rel_type="member_of", dst=CREW_FACTION))
    events.append(
        thread_created(
            thread_id=ALARM_THREAD,
            stakes="How hot the job is: calm/suspicious/alerted/lockdown "
            "(punned onto dormant/offered/active/dead — see world.py).",
            state=ALARM_STATES["calm"],
        )
    )
    events.append(
        thread_created(
            thread_id=SCORE_THREAD,
            stakes="Has the crew taken the prize, and who walked out with it: "
            "pending/prize-taken/escaped|betrayed (punned onto dormant/active/resolved/dead).",
            state=SCORE_STATES["pending"],
        )
    )
    events.append(
        item_created(
            item_id=PRIZE,
            name="The Heart of the Seventh Vault",
            owner_ref="p:seventh-vault",
            kind="treasure",
        )
    )
    events.append(
        item_created(item_id=KEYRING, name="the Warden's brass keyring", owner_ref=GUARD_FALLEN)
    )
    return events
