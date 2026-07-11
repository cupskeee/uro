"""The Ironwake Company — roster, gold, and the SHADOW COUNTERS.

Permadeath is real here AND in Uro: a merc who dies is removed from this roster for the season
(game state) and committed as an Uro casualty (world canon) by world/chronicle.py.

THE SHADOW COUNTERS (stress goal 8): every numeric field on `Company` below the roster is state
IRONWAKE *wanted* the world to own — win streaks, kill tallies, escalation pressure — but Uro's
declarative Reaction Layer has no counters, no arithmetic, and no accumulating state, so the
grammar refused every rule that needed one. The exact wished-for rules are logged in
world/rules.py (WISHED_RULES) and printed with every season. Do not mistake these fields for a
design choice; they are the refusal log's receipts.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Merc:
    actor_id: str
    name: str
    cls: str
    alive: bool = True


# The recruit pool is a fixed, ordered list — deterministic roster turnover with no RNG at all.
RECRUIT_POOL: tuple[tuple[str, str], ...] = (
    ("Brandt", "Bannerman"),
    ("Hakon", "Skirmisher"),
    ("Wren", "Crossbow"),
    ("Aldo", "Sergeant"),
    ("Isolde", "Sawbones"),
    ("Kell", "Skirmisher"),
    ("Marta", "Crossbow"),
    ("Otho", "Bannerman"),
)

RECRUIT_COST = 15
STAND_WATCH_SIZE = 2  # the Mill Watch detachment (TASK inc 5)


@dataclass
class Company:
    roster: list[Merc] = field(default_factory=list)
    gold: int = 40
    # --- SHADOW COUNTERS (see module docstring; refusal receipts in world/rules.py) ---
    wins: int = 0
    losses: int = 0
    contracts_taken: int = 0
    total_kills: int = 0  # wanted: reputation tier from total kills (refused: accumulator)
    red_band_dead: int = 0  # wanted: escalate war every 5 dead raiders (refused: counter)
    bounty_failures: int = 0  # wanted: raise the bounty price per failure (refused: arithmetic)
    _recruited: int = 0

    def living(self) -> list[Merc]:
        return [m for m in self.roster if m.alive]

    def deploy(self, limit: int = 6) -> list[Merc]:
        return self.living()[:limit]

    def merc(self, actor_id: str) -> Merc | None:
        return next((m for m in self.roster if m.actor_id == actor_id), None)

    def mark_dead(self, actor_id: str) -> Merc | None:
        m = self.merc(actor_id)
        if m is not None:
            m.alive = False
        return m

    def next_recruit(self) -> tuple[str, str, str] | None:
        """The next sellsword waiting in Mira's taproom: (actor_id, name, class), or None when
        the pool runs dry. The caller pays gold and commits the ActorCreated to Uro."""
        if self._recruited >= len(RECRUIT_POOL):
            return None
        name, cls = RECRUIT_POOL[self._recruited]
        actor_id = f"a:merc-{name.lower()}"
        self._recruited += 1
        return actor_id, name, cls

    def hire(self, actor_id: str, name: str, cls: str) -> Merc:
        m = Merc(actor_id=actor_id, name=name, cls=cls)
        self.roster.append(m)
        self.gold -= RECRUIT_COST
        return m
