"""AI backfill (docs/09): the *assisted* sufficiency policy. LLM-generated, opt-in, tagged.

When a pack grades `thin`/`insufficient`, the World service can offer to fill the declared gaps
with a generation pass — every generated element tagged `provenance="ai_backfill"` so authors
review it and platforms show what the machine invented. Silent invention is never the default:
backfill runs only when the operator asks. CI never calls a live model — a scripted provider
drives the deterministic test; the live pass is the operator's (docs/10 thesis split).

Every rubric dimension has a filler (D-108), run in an order where each can build on the last:
places, then who runs things, then people (placed and affiliated by name), then a conflict, the
tone, and a past. Each is one structured call (`worldsmith` role) and one ask→generate→tag step;
a reply that can't be used fills nothing and the gap stays reported. What it makes:

- geography: places (a region, a settlement in it, a site in that), each `PlaceSeed` tagged;
- power: one faction that runs things or contests who does (`FactionSeed`);
- population: people up to three, each placed and maybe affiliated (`ActorSeed`);
- conflict: one thread (`ThreadSeed`);
- tone: tone tags (recorded in `manifest.backfilled`: a tag has nowhere else to carry one);
- history: facts about the past, as claims that reach play (`ClaimSeed`, origin ai_backfill),
  and their overview in lore (`lore/ai_backfill/history.md`, which the grade reads).

`manifest.backfilled` lists every dimension filled, so the record survives where nothing else
carries a tag (tone). Names are invented only where the gap is the absence of named things (places,
people, a faction); elsewhere the model grounds its output in what exists. English (D-101).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from uro_core.domain.ids import new_id
from uro_core.providers.base import Message
from uro_core.providers.router import ProviderRouter
from uro_core.worldpack.models import (
    ActorSeed,
    ClaimSeed,
    FactionSeed,
    PlaceSeed,
    ThreadSeed,
    WorldPack,
)
from uro_core.worldpack.sufficiency import SufficiencyReport, check_sufficiency

BACKFILL_ROLE = "worldsmith"  # generation of world seeds (docs/04 role set is a living list)
TAG = "ai_backfill"
HISTORY_LORE = "ai_backfill/history.md"  # where a backfilled past's overview lives in lore
_LORE_EXCERPT = 2000  # chars of the pack's own lore shown to the model
_PEOPLE = 3  # the rubric's population floor

_SHARED = (
    "You are a world-builder filling a GAP in an author's setting. Fit the world's tone and what "
    "already exists. Everything you write becomes canon in the world's own voice: never mention "
    "players, their choices, the game, or these instructions. Write in English. Output ONLY JSON."
)

_PROMPTS = {
    "geography": (
        "The world has no places yet. Invent THREE places, largest to smallest: one `region`, one "
        "`settlement` inside it, one `site` inside that (use whatever these are in this setting: a "
        "station's decks, a ship's holds, a city's districts). Name each. "
        'JSON: {"places": [{"name": "…", "kind": "region|settlement|site", '
        '"description": "one sentence", "parent": "the name of the place it is inside, or null"}]}'
    ),
    "power": (
        "Nobody runs things yet. Invent ONE group that runs things, or contests who does, grounded "
        "in the places that exist. Name it. "
        'JSON: {"faction": {"name": "…", "kind": "faction|religion", '
        '"description": "one sentence"}}'
    ),
    "population": (
        "The world needs {n} more people to meet. Invent them: each with a name, a role (what "
        "others call them, ONE TO THREE WORDS in the setting's own words, like 'harbour-warden' "
        "or 'eel-catcher': a label, not a sentence), the place they're found (one of the PLACES, "
        "by name), and the group they belong to (one of the GROUPS, by name, or null). At most "
        "one may be a person of standing (tier 2); the rest are tier 1. "
        'JSON: {"people": [{"name": "…", "role": "…", "tier": 1, "place": "…", '
        '"group": "… or null"}]}'
    ),
    "conflict": (
        "Generate ONE conflict seed — a live tension in the world — grounded in the groups, people "
        "and places that exist; invent no new proper nouns. Write the stakes as a fact of the "
        "world. "
        'JSON: {"stakes": "one or two sentences", "state": "dormant|offered|active"}'
    ),
    "tone": (
        "The world has no tone yet. Give THREE or FOUR short tone tags (one or two words each) for "
        "how its story should sound, from what exists. "
        'JSON: {"tone": ["…", "…", "…"]}'
    ),
    "history": (
        "The world has no past to reference. Write FOUR facts about its past, each one or two "
        "sentences, that the places, groups and people now live with. Ground them in what exists "
        "(name existing places, groups and people); invent no other proper nouns. "
        'JSON: {"past": ["…", "…", "…", "…"]}'
    ),
}

_ORDER = ("geography", "power", "population", "conflict", "tone", "history")


async def backfill_gaps(
    pack: WorldPack,
    router: ProviderRouter,
    *,
    report: SufficiencyReport | None = None,
    play_creates_actors: bool = True,
) -> tuple[WorldPack, list[str]]:
    """Fill a pack's sufficiency gaps with tagged, AI-generated seeds (D-108). Returns the
    augmented pack + a human-readable line per thing added. The gaps are the report's (or the
    pack's own grade); a dimension the pack already covers is never touched."""
    report = report or check_sufficiency(pack, play_creates_actors=play_creates_actors)
    gaps = {d.name for d in report.dimensions if not d.ok}
    added: list[str] = []
    for dim in _ORDER:
        if dim not in gaps:
            continue
        pack, lines = await _FILLERS[dim](pack, router)
        if lines:
            added.extend(lines)
            manifest = pack.manifest.model_copy(
                update={"backfilled": [*pack.manifest.backfilled, dim]}
            )
            pack = pack.model_copy(update={"manifest": manifest})
    return pack, added


# --- the fillers: (pack, router) -> (augmented pack, lines added) ---


async def _geography(pack: WorldPack, router: ProviderRouter) -> tuple[WorldPack, list[str]]:
    data = await _ask(router, "geography", pack)
    rows = [r for r in _list(data, "places") if _text(r, "name")][:3]
    by_name = {p.name.casefold(): p.id for p in pack.places}
    made: list[PlaceSeed] = []
    for row in rows:
        kind = _text(row, "kind")
        place = PlaceSeed(
            id=f"p:{new_id()}",
            name=_text(row, "name"),
            kind=kind if kind in ("region", "settlement", "site") else "site",  # type: ignore[arg-type]
            description=_text(row, "description"),
            provenance=TAG,
        )
        made.append(place)
        by_name.setdefault(place.name.casefold(), place.id)
    # parents by name, once every new place has an id; one naming itself or nothing known has none
    for place, row in zip(made, rows, strict=True):
        parent = by_name.get(_text(row, "parent").casefold())
        if parent and parent != place.id:
            place.parent = parent
    return (
        pack.model_copy(update={"places": [*pack.places, *made]}),
        [f"place ({TAG}): {p.name} — {p.kind}" for p in made],
    )


async def _power(pack: WorldPack, router: ProviderRouter) -> tuple[WorldPack, list[str]]:
    data = await _ask(router, "power", pack)
    row = data.get("faction") if isinstance(data, dict) else None
    if not isinstance(row, dict) or not _text(row, "name"):
        return pack, []
    kind = _text(row, "kind")
    faction = FactionSeed(
        id=f"f:{new_id()}",
        name=_text(row, "name"),
        kind=kind if kind in ("faction", "religion") else "faction",
        description=_text(row, "description"),
        provenance=TAG,
    )
    return (
        pack.model_copy(update={"factions": [*pack.factions, faction]}),
        [f"faction ({TAG}): {faction.name}"],
    )


async def _population(pack: WorldPack, router: ProviderRouter) -> tuple[WorldPack, list[str]]:
    need = max(_PEOPLE - len(pack.actors), 1)
    data = await _ask(router, "population", pack, n=need)
    places = {p.name.casefold(): p.id for p in pack.places}
    groups = {f.name.casefold(): f.id for f in pack.factions}
    taken = {a.name.casefold() for a in pack.actors}
    made: list[ActorSeed] = []
    standing = any(a.tier >= 2 for a in pack.actors)
    for row in _list(data, "people"):
        name = _text(row, "name")
        if not name or name.casefold() in taken or len(made) >= need:
            continue
        tier = 2 if row.get("tier") == 2 and not standing else 1
        standing = standing or tier == 2
        made.append(
            ActorSeed(
                id=f"a:{new_id()}",
                name=name,
                tier=tier,
                role=_text(row, "role"),
                location=places.get(_text(row, "place").casefold()),
                faction=groups.get(_text(row, "group").casefold()),
                provenance=TAG,
            )
        )
        taken.add(name.casefold())
    return (
        pack.model_copy(update={"actors": [*pack.actors, *made]}),
        [f"person ({TAG}): {a.name}, {a.role or 'no role'}" for a in made],
    )


async def _conflict(pack: WorldPack, router: ProviderRouter) -> tuple[WorldPack, list[str]]:
    data = await _ask(router, "conflict", pack)
    stakes = _text(data, "stakes") if isinstance(data, dict) else ""
    if not stakes:
        return pack, []
    state = _text(data, "state")
    seed = ThreadSeed(
        id=f"t:{new_id()}",
        stakes=stakes,
        state=state if state in ("dormant", "offered", "active") else "dormant",  # type: ignore[arg-type]
        provenance=TAG,
    )
    return (
        pack.model_copy(update={"threads": [*pack.threads, seed]}),
        [f"conflict seed ({TAG}): {seed.stakes}"],
    )


async def _tone(pack: WorldPack, router: ProviderRouter) -> tuple[WorldPack, list[str]]:
    data = await _ask(router, "tone", pack)
    tags = [t.strip() for t in _list(data, "tone", strings=True) if t.strip()][:4]
    if not tags:
        return pack, []
    manifest = pack.manifest.model_copy(update={"tone": tags})
    return pack.model_copy(update={"manifest": manifest}), [f"tone ({TAG}): {', '.join(tags)}"]


async def _history(pack: WorldPack, router: ProviderRouter) -> tuple[WorldPack, list[str]]:
    data = await _ask(router, "history", pack)
    facts = [f.strip() for f in _list(data, "past", strings=True) if f.strip()][:6]
    if not facts:
        return pack, []
    names = [
        (n.casefold(), ref)
        for n, ref in [
            *((p.name, p.id) for p in pack.places),
            *((f.name, f.id) for f in pack.factions),
            *((a.name, a.id) for a in pack.actors),
        ]
    ]
    claims = [
        ClaimSeed(
            id=f"c:{new_id()}",
            statement=fact,
            truth="true",
            subject_refs=[ref for name, ref in names if _names(fact, name)],
            provenance=TAG,
        )
        for fact in facts
    ]
    overview = "# A past (AI backfill)\n\n" + "\n\n".join(facts) + "\n"
    return (
        pack.model_copy(
            update={
                "claims": [*pack.claims, *claims],
                "lore": {**pack.lore, HISTORY_LORE: overview},
            }
        ),
        [f"fact about the past ({TAG}): {c.statement}" for c in claims],
    )


_FILLERS: dict[str, Callable[[WorldPack, ProviderRouter], Awaitable[tuple[WorldPack, list[str]]]]]
_FILLERS = {
    "geography": _geography,
    "power": _power,
    "population": _population,
    "conflict": _conflict,
    "tone": _tone,
    "history": _history,
}


# --- the ask, and reading the reply ---


async def _ask(router: ProviderRouter, dim: str, pack: WorldPack, **fill: Any) -> Any:
    task = _PROMPTS[dim].replace("{n}", str(fill["n"])) if "n" in fill else _PROMPTS[dim]
    messages = [
        Message(role="system", content=f"{_SHARED}\n\n{task}"),
        Message(role="user", content=_world_brief(pack)),
    ]
    raw = await router.complete(BACKFILL_ROLE, messages, json_mode=True, temperature=0.7)
    try:
        return json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def _world_brief(pack: WorldPack) -> str:
    """What the model is shown: the world as it stands, the pack's own words first."""
    m = pack.manifest
    lore = "\n\n".join(t for path, t in sorted(pack.lore.items()) if not path.startswith(TAG))
    lines = [
        f"WORLD: {m.name}",
        f"TONE: {', '.join(m.tone) or '(none yet)'}",
        f"DESCRIPTION: {m.description or '(none)'}",
        "PLACES: " + ("; ".join(f"{p.name} ({p.kind})" for p in pack.places) or "(none yet)"),
        "GROUPS: "
        + ("; ".join(f"{f.name}: {f.description}" for f in pack.factions) or "(none yet)"),
        "PEOPLE: "
        + ("; ".join(f"{a.name} ({a.role or 'no role'})" for a in pack.actors) or "(none yet)"),
        "CONFLICTS: " + ("; ".join(t.stakes for t in pack.threads) or "(none yet)"),
    ]
    if lore:
        lines.append(f"LORE (the author's own):\n{lore[:_LORE_EXCERPT]}")
    return "\n".join(lines)


def _list(data: Any, key: str, *, strings: bool = False) -> list[Any]:
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    want = str if strings else dict
    return [i for i in items if isinstance(i, want)]


def _text(row: Any, key: str) -> str:
    value = row.get(key) if isinstance(row, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _names(text: str, name: str) -> bool:
    """A name appears in the text as whole words, in any case."""
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) is not None
