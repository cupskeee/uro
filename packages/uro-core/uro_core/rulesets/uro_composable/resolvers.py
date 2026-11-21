"""The resolvers: how a step and a difficulty become dice, and dice a band (D-114).

Code, never data: a definition picks one and may tune it. Each reads only the two neutral scales
(a step, a difficulty word) and answers in the third (a band), so any vocabulary plays under any
of them. The defaults put the three close together at `normal` for an ordinary rating
(`odds` computes the exact chances; a test holds them to it):

    step        0     1     2     3     4     5      chance of anything but `fail`, at normal
    d20        .45   .55   .65   .75   .85   .95
    2d6        .42   .58   .72   .83   .92   .92
    pool       .50   .50   .75   .88   .94   .97
"""

from __future__ import annotations

from fractions import Fraction
from itertools import product
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_composable.definition import DIFFICULTIES, Band, Cost, Difficulty

_STRICT = ConfigDict(extra="forbid")


class Roll(BaseModel):
    band: Band
    trace: str  # the working, for the narrator
    view: dict[str, Any]  # the working a player may see: their own dice and what they met
    pushed: bool = False


def _table(easy: int, normal: int, hard: int, extreme: int) -> dict[Difficulty, int]:
    return {"easy": easy, "normal": normal, "hard": hard, "extreme": extreme}


def _full(table: dict[Difficulty, int], name: str) -> None:
    missing = [d for d in DIFFICULTIES if d not in table]
    if missing:
        raise ValueError(f"{name}: a value for each of {', '.join(DIFFICULTIES)}")


class D20(BaseModel):
    """d20 + `per_step` times step against the difficulty's target: `fail` below it, `success`
    at it, `great` at target + `great_margin` or on a natural 20. No `cost` band."""

    model_config = _STRICT

    kind: Literal["d20"] = "d20"
    per_step: int = Field(2, ge=0, le=5)
    targets: dict[Difficulty, int] = Field(default_factory=lambda: _table(8, 12, 16, 20))
    great_margin: int = Field(10, ge=1, le=40)

    @model_validator(mode="after")
    def _check(self) -> D20:
        _full(self.targets, "d20.targets")
        return self

    @property
    def bands(self) -> tuple[Band, ...]:
        return ("fail", "success", "great")

    def judge(
        self, dice: list[int], step: int, difficulty: Difficulty
    ) -> tuple[Band, str, dict[str, Any]]:
        die, mod, target = dice[0], self.per_step * step, self.targets[difficulty]
        total = die + mod
        band: Band = (
            "great"
            if die == 20 or total >= target + self.great_margin
            else "success"
            if total >= target
            else "fail"
        )
        return (
            band,
            f"d20 ({die}) {mod:+d} = {total} vs {target}",
            {"dice": [die], "modifier": mod, "total": total, "against": target},
        )

    def shape(self, step: int) -> list[int]:
        return [20]


class TwoD6(BaseModel):
    """2d6 + (step - 1, held to -1..+3) + the difficulty's shift: `fail` on 6 or less, `cost` on
    7 to 9, `success` on 10 or more; `great` on 12 or more only when `great` is on."""

    model_config = _STRICT

    kind: Literal["2d6"] = "2d6"
    shifts: dict[Difficulty, int] = Field(default_factory=lambda: _table(1, 0, -1, -2))
    great: bool = False

    @model_validator(mode="after")
    def _check(self) -> TwoD6:
        _full(self.shifts, "2d6.shifts")
        return self

    @property
    def bands(self) -> tuple[Band, ...]:
        return ("fail", "cost", "success", "great") if self.great else ("fail", "cost", "success")

    def judge(
        self, dice: list[int], step: int, difficulty: Difficulty
    ) -> tuple[Band, str, dict[str, Any]]:
        mod = max(-1, min(3, step - 1)) + self.shifts[difficulty]
        total = sum(dice) + mod
        band: Band = (
            "great"
            if self.great and total >= 12
            else "success"
            if total >= 10
            else "cost"
            if total >= 7
            else "fail"
        )
        return (
            band,
            f"2d6 ({dice[0]}, {dice[1]}) {mod:+d} = {total}",
            {"dice": list(dice), "modifier": mod, "total": total},
        )

    def shape(self, step: int) -> list[int]:
        return [6, 6]


class Pool(BaseModel):
    """max(step, 1) d6, each die making it at the difficulty's face or higher: `fail` when none
    does, `great` at `great_marks` or more, else `success`. No `cost` band. `push` rerolls a
    failed pool once, paid on a track."""

    model_config = _STRICT

    kind: Literal["pool"] = "pool"
    faces: dict[Difficulty, int] = Field(default_factory=lambda: _table(3, 4, 5, 6))
    great_marks: int = Field(3, ge=2, le=20)
    push: Cost | None = None

    @model_validator(mode="after")
    def _check(self) -> Pool:
        _full(self.faces, "pool.faces")
        if any(not 2 <= f <= 6 for f in self.faces.values()):
            raise ValueError("pool.faces: each is from 2 to 6")
        return self

    @property
    def bands(self) -> tuple[Band, ...]:
        return ("fail", "success", "great")

    def judge(
        self, dice: list[int], step: int, difficulty: Difficulty
    ) -> tuple[Band, str, dict[str, Any]]:
        face = self.faces[difficulty]
        marks = sum(1 for d in dice if d >= face)
        band: Band = "fail" if marks == 0 else "great" if marks >= self.great_marks else "success"
        shown = ", ".join(str(d) for d in sorted(dice))
        return (
            band,
            f"{len(dice)}d6 ({shown}) at {face}+: {marks}",
            {"dice": sorted(dice), "against": face, "marks": marks},
        )

    def shape(self, step: int) -> list[int]:
        return [6] * max(step, 1)


Resolver = Annotated[D20 | TwoD6 | Pool, Field(discriminator="kind")]


def roll(
    resolver: D20 | TwoD6 | Pool,
    step: int,
    difficulty: Difficulty,
    rng: Rng,
    *,
    may_push: bool = False,
) -> Roll:
    """One check. `may_push` says the roller can pay a pool's push (the caller charges it)."""
    dice = [rng.die(sides) for sides in resolver.shape(step)]
    band, trace, view = resolver.judge(dice, step, difficulty)
    pushed = False
    if band == "fail" and may_push and isinstance(resolver, Pool) and resolver.push is not None:
        dice = [rng.die(sides) for sides in resolver.shape(step)]
        band, again, view = resolver.judge(dice, step, difficulty)
        trace, pushed = f"{trace}, pushed: {again}", True
    return Roll(band=band, trace=trace, view={**view, "pushed": pushed}, pushed=pushed)


def shift(resolver: D20 | TwoD6 | Pool, band: Band, by: int) -> Band:
    """A band moved `by` places along the bands this resolver produces."""
    bands = resolver.bands
    if not by or band not in bands:
        return band
    return bands[max(0, min(len(bands) - 1, bands.index(band) + by))]


def odds(resolver: D20 | TwoD6 | Pool, step: int, difficulty: Difficulty) -> dict[Band, Fraction]:
    """The exact chance of each band for one unpushed roll, by enumeration."""
    shape = resolver.shape(step)
    out: dict[Band, Fraction] = {}
    each = Fraction(1, 1)
    for sides in shape:
        each /= sides
    for dice in product(*(range(1, s + 1) for s in shape)):
        band = resolver.judge(list(dice), step, difficulty)[0]
        out[band] = out.get(band, Fraction(0)) + each
    return out
