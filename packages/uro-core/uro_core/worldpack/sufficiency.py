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


def _population_note(pack: WorldPack, play_creates_actors: bool) -> str:
    if not pack.manifest.generate_population:
        return ""
    if play_creates_actors:
        return " + generate_population (the people are made in play)"
    return (
        " + generate_population, but this instance's extraction policy keeps play from creating"
        " actors"
    )


def check_sufficiency(pack: WorldPack, *, play_creates_actors: bool = True) -> SufficiencyReport:
    """Grade a pack against the runtime rubric (docs/09). `generate_population = true` means the
    world's people are made in play — the narrator names them and the extractor records them
    (D-49/D-50); nothing is generated up front. So it stands in for seeded actors only while play
    may create actors (`play_creates_actors`: the instance's extraction policy, when the caller has
    it; a pure parse assumes the default, on)."""
    kinds = {p.kind for p in pack.places}
    n_actors = len(pack.actors)
    # Power, in any genre (D-97): a faction, or a person of standing (tier 2+: a duke, a station
    # director, a crime boss). No role words: the engine doesn't know what a ruler is called.
    n_standing = sum(1 for a in pack.actors if a.tier >= 2)
    # simulated years only count as a past when the pack can be seeded (D-97: its vocabulary)
    history = pack.manifest.history
    seedable = history.simulate_years > 0 and history.vocabulary is not None
    has_conflict = bool(pack.threads) or any(f.at_war_with for f in pack.factions)
    lore_chars = sum(len(t) for t in pack.lore.values())

    dims = [
        _dim(  # one place is a world (a ship, a mansion, a station): no shape is required (D-97)
            "geography",
            bool(pack.places),
            f"{len(pack.places)} places: {sorted(kinds)}",
            "no places seeded — nowhere to be",
        ),
        _dim(
            "population",
            n_actors >= 3 or (pack.manifest.generate_population and play_creates_actors),
            f"{n_actors} seeded actors" + _population_note(pack, play_creates_actors),
            f"{n_actors} seeded actors{_population_note(pack, play_creates_actors)}: needs ≥3, or "
            "generate_population=true with play allowed to create actors — no one to meet",
        ),
        _dim(
            "power",
            bool(pack.factions) or n_standing > 0,
            f"{len(pack.factions)} factions, {n_standing} people of standing (tier 2+)",
            "no faction and no one of standing (tier 2+) — no one runs things",
        ),
        _dim(
            "conflict",
            has_conflict,
            f"{len(pack.threads)} conflict seeds",
            "no conflict seeds found — campaigns will open aimless",
        ),
        _dim(
            "tone",
            bool(pack.manifest.tone)
            or "narrator.system.j2" in pack.prompts
            or "narrator.system.append.j2" in pack.prompts,  # D-106: a style note added
            f"tone: {pack.manifest.tone}" if pack.manifest.tone else "narrator style template",
            "no tone tags or narrator style template — the world has no voice",
        ),
        _dim(
            "history",
            lore_chars >= _HISTORY_LORE_THRESHOLD or seedable,
            f"{lore_chars} chars of lore"
            + (f", simulate_years={pack.manifest.history.simulate_years}" if seedable else ""),
            "little lore and nothing to seed a past from (history.simulate_years with a "
            "[history.vocabulary]) — no past to reference",
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
