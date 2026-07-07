"""The sufficiency check (docs/09). Deterministic — no LLM.

Scores an assembled pack against the coverage rubric the pipeline needs at runtime and grades
it `runnable | thin | insufficient` with specific gaps. The owner requirement: if the lore is
too minimal to run a world, the author must be TOLD (not silently fail at play). The AI backfill
pass (4.4) consumes these gaps to offer fixes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from uro_core.worldpack.models import WorldPack

Grade = Literal["runnable", "thin", "insufficient"]
_HISTORY_LORE_THRESHOLD = 200  # chars of overview lore that count as "a past to reference"
_RULER_ROLES = ("ruler", "king", "queen", "lord", "lady", "chief", "duke", "duchess", "warlord")

# Structural dimensions: without these there is literally nothing to play → insufficient.
# The rest are playability dimensions: present-but-shallow → thin (still runnable).
_STRUCTURAL = frozenset({"geography", "population"})


class SufficiencyDimension(BaseModel):
    name: str
    ok: bool
    detail: str  # what's present, or the specific gap


class SufficiencyReport(BaseModel):
    grade: Grade
    dimensions: list[SufficiencyDimension]

    @property
    def gaps(self) -> list[str]:
        return [d.detail for d in self.dimensions if not d.ok]


def check_sufficiency(pack: WorldPack) -> SufficiencyReport:
    kinds = {p.kind for p in pack.places}
    n_actors = len(pack.actors)
    has_ruler = any(any(r in a.role.lower() for r in _RULER_ROLES) for a in pack.actors)
    has_conflict = bool(pack.threads) or any(f.at_war_with for f in pack.factions)
    lore_chars = sum(len(t) for t in pack.lore.values())

    dims = [
        _dim(
            "geography",
            {"region", "settlement", "site"} <= kinds,
            f"places: {sorted(kinds)}" if kinds else "no places seeded",
            "needs ≥1 region, ≥1 settlement, and ≥1 site — nowhere to be",
        ),
        _dim(
            "population",
            n_actors >= 3 or pack.manifest.generate_population,
            f"{n_actors} seeded actors"
            + (" + generate_population" if pack.manifest.generate_population else ""),
            "needs ≥3 seeded actors or generate_population=true — no one to meet",
        ),
        _dim(
            "power",
            bool(pack.factions) or has_ruler,
            f"{len(pack.factions)} factions" + (", a ruler" if has_ruler else ""),
            "no faction or ruler — no one runs things",
        ),
        _dim(
            "conflict",
            has_conflict,
            f"{len(pack.threads)} conflict seeds",
            "no conflict seeds found — campaigns will open aimless",
        ),
        _dim(
            "tone",
            bool(pack.manifest.tone) or "narrator.style.j2" in pack.prompts,
            f"tone: {pack.manifest.tone}" if pack.manifest.tone else "narrator style template",
            "no tone tags or narrator style template — the world has no voice",
        ),
        _dim(
            "history",
            lore_chars >= _HISTORY_LORE_THRESHOLD or pack.manifest.history.simulate_years > 0,
            f"{lore_chars} chars of lore"
            + (
                f", simulate_years={pack.manifest.history.simulate_years}"
                if pack.manifest.history.simulate_years
                else ""
            ),
            "little lore and no history.simulate_years — no past to reference",
        ),
    ]

    if any(not d.ok for d in dims if d.name in _STRUCTURAL):
        grade: Grade = "insufficient"
    elif any(not d.ok for d in dims):
        grade = "thin"
    else:
        grade = "runnable"
    return SufficiencyReport(grade=grade, dimensions=dims)


def _dim(name: str, ok: bool, present: str, gap: str) -> SufficiencyDimension:
    return SufficiencyDimension(name=name, ok=ok, detail=present if ok else gap)
