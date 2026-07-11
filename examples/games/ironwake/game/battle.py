"""The IRONWAKE skirmish engine — TASK.md section B, deterministic under one seeded RNG.

All randomness flows from a single `random.Random(seed)` per battle; every roll is logged. The
AI is a fixed, deterministic policy (no randomness outside the dice): pick the nearest enemy
(Chebyshev distance, ties by uid), walk the BFS distance field toward attack range, attack.
Signatures per TASK B.2, with the AI policies documented at each decision point.

Board conventions:
- 10x10 grid authored as strings; '.' floor, '#' wall (impassable), '+' cover (ranged attackers
  take -4 against an occupant unless the shot is Aimed), 'o' the scenario objective (floor).
- Movement is 4-directional BFS over passable, unoccupied tiles; attacks use Chebyshev range
  (diagonal counts as adjacent) — documented simplification, no line-of-sight for shots.

Scenario end-conditions beyond mutual annihilation (both used by real contracts):
- hold_rounds R: a Defend battle ends at the end of round R if the company still holds the
  objective (a living company unit within 1 tile) — the surviving enemies FLEE the field
  (alive -> they are witnesses; this is how a routed raider carries your legend home).
- collapse_round R: a Desperate Stand site is coming down (the mill is ablaze); at the end of
  round R everything still on the board dies. This is the tuning TASK B.7 asks for so that a
  total, witnessless wipe is a live possibility.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field

from ironwake.game.units import BattleReport, Unit

FLOOR, WALL, COVER, OBJECTIVE = ".", "#", "+", "o"
_MAX_ROUNDS = 100  # hard safety cap; a capped battle is a "draw" (treated as a loss for pay)


@dataclass
class Board:
    rows: list[str]  # 10 strings of 10 chars each

    def __post_init__(self) -> None:
        assert len(self.rows) == 10 and all(len(r) == 10 for r in self.rows), "board must be 10x10"

    def tile(self, pos: tuple[int, int]) -> str:
        x, y = pos
        return self.rows[y][x]

    def in_bounds(self, pos: tuple[int, int]) -> bool:
        x, y = pos
        return 0 <= x < 10 and 0 <= y < 10

    def passable(self, pos: tuple[int, int]) -> bool:
        return self.in_bounds(pos) and self.tile(pos) != WALL

    def is_cover(self, pos: tuple[int, int]) -> bool:
        return self.tile(pos) == COVER


def chebyshev(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


# 4-neighborhood in a FIXED order (N, E, S, W) — the movement tie-break is this order.
_STEPS = ((0, -1), (1, 0), (0, 1), (-1, 0))


@dataclass
class Battle:
    board: Board
    units: list[Unit]
    seed: int
    objective: tuple[int, int] | None = None
    hold_rounds: int = 0  # Defend: company wins by holding the objective this many rounds
    collapse_round: int = 0  # Desperate Stand: the site kills everything still fighting
    log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._start_count = {
            "company": sum(1 for u in self.units if u.team == "company"),
            "enemy": sum(1 for u in self.units if u.team == "enemy"),
        }
        self._casualties: list[str] = []
        self._killing_blows: dict[str, str] = {}
        # initiative order is fixed at battle start: descending initiative, ties by uid
        self._order = sorted(self.units, key=lambda u: (-u.spec.initiative, u.uid))
        self.rounds = 0

    # --- helpers -------------------------------------------------------------------------

    def _roll(self, sides: int, label: str) -> int:
        value = self._rng.randint(1, sides)
        self.log.append(f"    [d{sides}={value}] {label}")
        return value

    def _on_board(self, team: str | None = None) -> list[Unit]:
        return [u for u in self.units if u.on_board and (team is None or u.team == team)]

    def _occupied(self, except_unit: Unit | None = None) -> set[tuple[int, int]]:
        return {u.pos for u in self.units if u.on_board and u is not except_unit}

    def _enemies_of(self, unit: Unit) -> list[Unit]:
        other = "enemy" if unit.team == "company" else "company"
        return self._on_board(other)

    def _nearest_enemy(self, unit: Unit) -> Unit | None:
        foes = self._enemies_of(unit)
        if not foes:
            return None
        return min(foes, key=lambda f: (chebyshev(unit.pos, f.pos), f.uid))

    def _distance_field(
        self, goals: set[tuple[int, int]], mover: Unit
    ) -> dict[tuple[int, int], int]:
        """Multi-source BFS distance-to-goal over passable, unoccupied tiles (the mover's own
        tile is free). Deterministic: fixed step order, queue-based BFS."""
        blocked = self._occupied(except_unit=mover)
        field_: dict[tuple[int, int], int] = {}
        queue: deque[tuple[int, int]] = deque()
        for g in sorted(goals):
            if self.board.passable(g) and (g not in blocked or g == mover.pos):
                field_[g] = 0
                queue.append(g)
        while queue:
            pos = queue.popleft()
            for dx, dy in _STEPS:
                nxt = (pos[0] + dx, pos[1] + dy)
                if nxt in field_ or not self.board.passable(nxt) or nxt in blocked:
                    continue
                field_[nxt] = field_[pos] + 1
                queue.append(nxt)
        return field_

    def _step_toward(self, unit: Unit, goals: set[tuple[int, int]]) -> None:
        """Walk up to `move` tiles down the BFS field toward the goal set. Ties break in the
        fixed N/E/S/W step order. Stops when no neighbor improves (blocked or arrived)."""
        field_ = self._distance_field(goals, unit)
        if unit.pos not in field_ and not any(
            self.board.in_bounds((unit.pos[0] + dx, unit.pos[1] + dy)) for dx, dy in _STEPS
        ):
            return
        blocked = self._occupied(except_unit=unit)
        start = unit.pos
        for _ in range(unit.spec.move):
            here = field_.get(unit.pos)
            best: tuple[int, int] | None = None
            best_d = here if here is not None else 10**6
            for dx, dy in _STEPS:
                nxt = (unit.pos[0] + dx, unit.pos[1] + dy)
                d = field_.get(nxt)
                if d is None or nxt in blocked:
                    continue
                if d < best_d:
                    best, best_d = nxt, d
            if best is None:
                break
            unit.pos = best
            if best_d == 0:
                break
        if unit.pos != start:
            self.log.append(f"  {unit.name} moves {start} -> {unit.pos}")

    # --- combat --------------------------------------------------------------------------

    def _attack(self, attacker: Unit, defender: Unit) -> None:
        dist = chebyshev(attacker.pos, defender.pos)
        is_ranged = dist > 1
        base = attacker.spec.ranged if is_ranged else attacker.spec.melee
        aimed = is_ranged and attacker.spec.cls == "Crossbow"  # signature: Aimed shot
        mods: list[str] = []
        bonus = base
        if attacker.rally:
            bonus += attacker.rally
            mods.append(f"rally+{attacker.rally}")
            attacker.rally = 0
        # signature: Flank — +3 when an ally stands adjacent to the target
        if attacker.spec.cls == "Skirmisher" and any(
            a is not attacker and a.team == attacker.team and chebyshev(a.pos, defender.pos) <= 1
            for a in self._on_board()
        ):
            bonus += 3
            mods.append("flank+3")
        if is_ranged and self.board.is_cover(defender.pos):
            if aimed:
                mods.append("aimed(ignores cover)")
            else:
                bonus -= 4
                mods.append("cover-4")
        dc = 10 + defender.spec.armor
        kind = "shoots" if is_ranged else "strikes at"
        roll = self._roll(
            20,
            f"{attacker.name} {kind} {defender.name} "
            f"(+{base}{' ' + ' '.join(mods) if mods else ''} vs DC {dc})",
        )
        if roll + bonus < dc:
            self.log.append(f"  {attacker.name} misses {defender.name}")
            return
        dmg = self._roll(attacker.spec.damage_die, f"damage (+{attacker.spec.damage_bonus})")
        dmg = max(1, dmg + attacker.spec.damage_bonus)
        defender.hp -= dmg
        self.log.append(f"  {attacker.name} hits {defender.name} for {dmg} ({defender.hp} hp left)")
        if defender.hp <= 0:
            self._kill(defender, attacker)

    def _kill(self, victim: Unit, killer: Unit | None) -> None:
        victim.alive = False
        victim.hp = 0
        self._casualties.append(victim.uid)
        self._killing_blows[victim.uid] = killer.uid if killer is not None else ""
        if killer is not None:
            killer.kills += 1
            self.log.append(f"  ** {victim.name} is slain by {killer.name} **")
        else:
            self.log.append(f"  ** {victim.name} perishes **")

    # --- morale (TASK B.5) -----------------------------------------------------------------

    def _team_broken(self, team: str) -> bool:
        return len(self._on_board(team)) < self._start_count[team] / 2

    def _hold_suppressed(self, unit: Unit) -> bool:
        """Signature: Hold — a living, unfled Bannerman keeps allies within 2 tiles (himself
        included) from fleeing. AI policy: the banner is always planted once morale is live."""
        return any(
            a.team == unit.team and a.spec.cls == "Bannerman" and chebyshev(a.pos, unit.pos) <= 2
            for a in self._on_board()
        )

    def _morale_check(self, unit: Unit) -> bool:
        """True -> the unit flees. Checked at the start of its turn once its team is below half
        of its STARTING strength. d20 + 5 >= 12 stands fast; Hold suppresses the check.
        Captain Vorlund never routs — a named officer dies on his feet (which is also what
        keeps the protection-ceiling contract demonstrable on every seed: he must FALL on the
        grid for the world to refuse the death)."""
        if unit.spec.cls == "Vorlund":
            return False
        if not self._team_broken(unit.team) or self._hold_suppressed(unit):
            return False
        roll = self._roll(20, f"{unit.name} morale (+5 vs 12)")
        if roll + 5 >= 12:
            return False
        unit.fled = True
        self.log.append(f"  << {unit.name} breaks and flees the field >>")
        return True

    # --- the per-unit turn -----------------------------------------------------------------

    def _act(self, unit: Unit) -> None:
        target = self._nearest_enemy(unit)
        if target is None:
            return
        spec = unit.spec

        # signature: Patch — the Sawbones heals the worst-hurt adjacent ally (< half hp) for
        # 1d8 instead of attacking. AI priority: patch before fighting.
        if spec.cls == "Sawbones":
            hurt = [
                a
                for a in self._on_board(unit.team)
                if a is not unit and chebyshev(a.pos, unit.pos) <= 1 and a.hp < a.spec.hp / 2
            ]
            if hurt:
                ally = min(hurt, key=lambda a: (a.hp, a.uid))
                heal = self._roll(8, f"{unit.name} patches {ally.name}")
                ally.hp = min(ally.spec.hp, ally.hp + heal)
                self.log.append(f"  {unit.name} patches {ally.name} (+{heal} -> {ally.hp} hp)")
                return

        # move into attack range if needed (goal: any tile within range of the chosen target)
        if chebyshev(unit.pos, target.pos) > spec.attack_range:
            goals = {
                (x, y)
                for x in range(10)
                for y in range(10)
                if self.board.passable((x, y))
                and chebyshev((x, y), target.pos) <= spec.attack_range
            }
            self._step_toward(unit, goals)

        target = self._nearest_enemy(unit)  # re-pick after moving (the nearest may have changed)
        if target is not None and chebyshev(unit.pos, target.pos) <= spec.attack_range:
            self._attack(unit, target)
            return

        # signature: Rally — a Sergeant who cannot reach a foe steadies an adjacent ally
        # (+2 to hit on that ally's next attack) instead of wasting the turn.
        if spec.cls == "Sergeant":
            allies = [
                a
                for a in self._on_board(unit.team)
                if a is not unit and chebyshev(a.pos, unit.pos) <= 1
            ]
            if allies:
                ally = min(allies, key=lambda a: a.uid)
                ally.rally = 2
                self.log.append(f"  {unit.name} rallies {ally.name} (+2 to hit next attack)")

    # --- battle loop -------------------------------------------------------------------------

    def _over(self) -> bool:
        return not self._on_board("company") or not self._on_board("enemy")

    def run(self) -> BattleReport:
        self.log.append(
            f"== battle begins (seed {self.seed}): "
            f"{self._start_count['company']} company vs {self._start_count['enemy']} enemy =="
        )
        outcome = ""
        for rnd in range(1, _MAX_ROUNDS + 1):
            self.rounds = rnd
            self.log.append(f"-- round {rnd} --")
            for unit in self._order:
                if not unit.on_board:
                    continue
                if self._morale_check(unit):
                    if self._over():  # the last of a side fleeing decides the field NOW
                        break
                    continue
                self._act(unit)
                if self._over():
                    break
            # a lone survivor of the company holding the line (feat input, TASK B.6)
            company = self._on_board("company")
            if len(company) == 1 and self._on_board("enemy"):
                company[0].alone_rounds += 1
            # Defend: the raiders break off once the objective has been held long enough
            if (
                self.hold_rounds
                and rnd >= self.hold_rounds
                and self.objective is not None
                and any(chebyshev(u.pos, self.objective) <= 1 for u in self._on_board("company"))
                and self._on_board("enemy")
            ):
                for foe in self._on_board("enemy"):
                    foe.fled = True
                self.log.append("  << the attackers break off and melt away — the site holds >>")
            # Desperate Stand: the site comes down on everyone still fighting
            if self.collapse_round and rnd >= self.collapse_round and not self._over():
                self.log.append("  ** the burning mill collapses on the melee **")
                for u in list(self._on_board()):
                    self._kill(u, None)
            if self._over():
                break
        company_left = self._on_board("company")
        enemy_left = self._on_board("enemy")
        if company_left and not enemy_left:
            outcome = "win"
        elif enemy_left and not company_left:
            outcome = "loss"
        elif not company_left and not enemy_left:
            outcome = "wipe"
        else:
            outcome = "draw"  # round-capped stalemate; pays like a loss
        self.log.append(f"== battle ends: {outcome} after {self.rounds} rounds ==")

        survivors = tuple(u.uid for u in self.units if u.alive)
        # B.6: "held the {feature}" credits a unit that DEFENDED the objective — in IRONWAKE the
        # company is always the defending/holding side, so only company units earn it (a raider
        # who happens to die next to the gate did not hold it).
        objective_holders = tuple(
            u.uid
            for u in self._on_board("company")
            if self.objective is not None and chebyshev(u.pos, self.objective) <= 1
        )
        return BattleReport(
            outcome=outcome,
            rounds=self.rounds,
            casualties=tuple(self._casualties),
            survivors=survivors,
            fled=tuple(u.uid for u in self.units if u.fled),
            kills={u.uid: u.kills for u in self.units if u.kills},
            killing_blows=dict(self._killing_blows),
            alone_rounds={u.uid: u.alone_rounds for u in self.units if u.alone_rounds},
            objective_holders=objective_holders,
            log=tuple(self.log),
        )
