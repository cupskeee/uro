"""Seeded, deterministic RNG for rulesets (docs/06, 05).

Rulesets receive an `Rng`, never wall-clock or global `random` — this is what makes rolls
reproducible for dry-run and recorded-response replay (docs/10). Wraps `random.Random`: for
a given integer seed and call order the draws are reproducible **within a CPython version** —
enough for same-version replay and dry-run. (CPython only guarantees the raw `random()`
method stable across versions; `randint`/`choice` build on `_randbelow`, which may change, so
a replay corpus is version-scoped, not eternal — pin the interpreter if you archive one.) The
pipeline derives one `Rng` per beat from the campaign seed + a beat counter (Phase 3.2+).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


class Rng:
    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._r = random.Random(seed)

    @property
    def seed(self) -> int:
        return self._seed

    def die(self, sides: int) -> int:
        """One die: a uniform int in [1, sides]."""
        return self._r.randint(1, sides)

    def d20(self) -> int:
        return self._r.randint(1, 20)

    def roll(self, n: int, sides: int) -> int:
        """Sum of `n` dice of `sides` faces (e.g. roll(2, 6) = 2d6)."""
        return sum(self._r.randint(1, sides) for _ in range(n))

    def choice(self, seq: Sequence[T]) -> T:
        return self._r.choice(seq)
