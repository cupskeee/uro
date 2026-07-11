"""Unit stat tables — TASK.md section B.2, made unambiguous.

Choices the table left open (documented here, applied deterministically):
- initiative values (the table omitted them): Wolf 8, Skirmisher 7, Crossbow 6, Vorlund 6,
  Sergeant 5, Raider 5, Bannerman 4, Sawbones 3, Brute 2. Ties break by unit id, ascending.
- damage dice beyond the examples: Sawbones 1d6, Bannerman 1d8.
- attack ranges (Chebyshev tiles): melee = 1; Crossbow = 6; Sawbones = 3 (thrown knives, uses
  the ranged bonus). Everyone with ranged == 0 is melee-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UnitSpec:
    cls: str
    hp: int
    armor: int
    melee: int
    ranged: int
    move: int
    initiative: int
    damage_die: int  # the weapon die (d10 -> 10)
    damage_bonus: int = 0
    attack_range: int = 1  # Chebyshev; > 1 means the unit shoots at distance
    signature: str = ""


MERC_CLASSES: dict[str, UnitSpec] = {
    "Sergeant": UnitSpec(
        cls="Sergeant",
        hp=28,
        armor=6,
        melee=6,
        ranged=0,
        move=4,
        initiative=5,
        damage_die=10,
        signature="Rally",
    ),
    "Crossbow": UnitSpec(
        cls="Crossbow",
        hp=18,
        armor=2,
        melee=2,
        ranged=6,
        move=4,
        initiative=6,
        damage_die=8,
        attack_range=6,
        signature="Aimed shot",
    ),
    "Skirmisher": UnitSpec(
        cls="Skirmisher",
        hp=20,
        armor=3,
        melee=5,
        ranged=0,
        move=6,
        initiative=7,
        damage_die=6,
        damage_bonus=2,
        signature="Flank",
    ),
    "Sawbones": UnitSpec(
        cls="Sawbones",
        hp=16,
        armor=2,
        melee=2,
        ranged=2,
        move=4,
        initiative=3,
        damage_die=6,
        attack_range=3,
        signature="Patch",
    ),
    "Bannerman": UnitSpec(
        cls="Bannerman",
        hp=22,
        armor=4,
        melee=4,
        ranged=0,
        move=4,
        initiative=4,
        damage_die=8,
        signature="Hold",
    ),
}

ENEMY_TEMPLATES: dict[str, UnitSpec] = {
    "Raider": UnitSpec(
        cls="Raider",
        hp=14,
        armor=2,
        melee=4,
        ranged=0,
        move=4,
        initiative=5,
        damage_die=6,
    ),
    "Wolf": UnitSpec(
        cls="Wolf",
        hp=10,
        armor=0,
        melee=5,
        ranged=0,
        move=6,
        initiative=8,
        damage_die=4,
    ),
    "Brute": UnitSpec(
        cls="Brute",
        hp=26,
        armor=4,
        melee=6,
        ranged=0,
        move=3,
        initiative=2,
        damage_die=12,
    ),
    "Vorlund": UnitSpec(
        cls="Vorlund",
        hp=40,
        armor=6,
        melee=8,
        ranged=0,
        move=4,
        initiative=6,
        damage_die=12,
        damage_bonus=2,
        signature="the Red Captain",
    ),
}


@dataclass
class Unit:
    """A unit's live state on the board. `uid` IS the Uro actor id — the bundle refs are these."""

    uid: str
    name: str
    team: str  # "company" | "enemy"
    spec: UnitSpec
    pos: tuple[int, int]  # (x, y)
    hp: int = 0
    alive: bool = True
    fled: bool = False
    kills: int = 0
    rally: int = 0  # to-hit bonus granted by a Sergeant's Rally, consumed on the next attack
    alone_rounds: int = 0  # rounds spent as the last company unit standing (feat input)

    def __post_init__(self) -> None:
        if self.hp == 0:
            self.hp = self.spec.hp

    @property
    def on_board(self) -> bool:
        return self.alive and not self.fled


def make_unit(uid: str, name: str, team: str, spec: UnitSpec, pos: tuple[int, int]) -> Unit:
    return Unit(uid=uid, name=name, team=team, spec=spec, pos=pos)


@dataclass(frozen=True)
class BattleReport:
    """Everything world/chronicle.py needs to derive an OutcomeBundle (TASK B.6) — and a stable
    digest so tests can assert byte-identical replays."""

    outcome: str  # win | loss | wipe | draw
    rounds: int
    casualties: tuple[str, ...]  # uids in death order
    survivors: tuple[str, ...]  # every unit alive at the end (on board or fled), by uid
    fled: tuple[str, ...]
    kills: dict[str, int]  # uid -> kill count
    killing_blows: dict[str, str]  # victim uid -> killer uid ("" = scenario hazard)
    alone_rounds: dict[str, int]  # merc uid -> rounds stood as the last of the company
    objective_holders: tuple[str, ...]  # units adjacent to the objective at the end
    log: tuple[str, ...] = field(default=())

    def digest(self) -> str:
        """A compact, order-stable fingerprint of the whole fight (determinism assertions)."""
        return "|".join(
            [
                self.outcome,
                str(self.rounds),
                ",".join(self.casualties),
                ",".join(self.survivors),
                ",".join(self.fled),
                ",".join(f"{u}:{n}" for u, n in sorted(self.kills.items())),
                ",".join(f"{v}<{k}" for v, k in sorted(self.killing_blows.items())),
                ",".join(self.objective_holders),
            ]
        )
