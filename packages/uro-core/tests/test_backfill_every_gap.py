"""D-108 (Uro Loom's request 3): backfill fills every rubric dimension, and everything it invents
is tagged `provenance=ai_backfill` where it's committed. A scripted worldsmith stands in for the
model (CI never calls a live one); it answers each dimension's ask by what the ask says."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.worldpack.backfill import HISTORY_LORE, backfill_gaps
from uro_core.worldpack.create import import_pack
from uro_core.worldpack.parse import parse_pack
from uro_core.worldpack.sufficiency import check_sufficiency

_REPLIES = {
    "no places yet": {
        "places": [
            {"name": "The Saltmarsh", "kind": "region", "description": "Flat, wet, wide."},
            {
                "name": "Reedholm",
                "kind": "settlement",
                "description": "Stilt houses.",
                "parent": "The Saltmarsh",
            },
            {
                "name": "The Drowned Bell",
                "kind": "site",
                "description": "A sunken chapel.",
                "parent": "Reedholm",
            },
        ]
    },
    "Nobody runs things": {
        "faction": {
            "name": "The Dyke Wardens",
            "kind": "faction",
            "description": "Keep the sea out.",
        }
    },
    "more people": {
        "people": [
            {
                "name": "Ada Fenn",
                "role": "warden-captain",
                "tier": 2,
                "place": "Reedholm",
                "group": "The Dyke Wardens",
            },
            {
                "name": "Tam Reed",
                "role": "eel-catcher",
                "tier": 2,
                "place": "The Saltmarsh",
                "group": None,
            },
            {
                "name": "Old Sile",
                "role": "bell-keeper",
                "tier": 1,
                "place": "Nowhere Known",
                "group": "Nobody",
            },
        ]
    },
    "conflict seed": {
        "stakes": "The Dyke Wardens mean to flood the outer marsh to save Reedholm.",
        "state": "active",
    },
    "no tone yet": {"tone": ["damp", "stubborn", "quiet"]},
    "no past": {
        "past": [
            "Reedholm was built on stilts after the great flood took the old town.",
            "The Dyke Wardens were sworn the year the Drowned Bell first rang under water.",
            "Ada Fenn's mother held the dyke alone for a night and was never seen again.",
            "Nobody in the Saltmarsh will say who opened the sluices that year.",
        ]
    },
}


class _Worldsmith:
    def __init__(self, replies: dict[str, object] | None = None) -> None:
        self.replies = _REPLIES if replies is None else replies
        self.asked: list[str] = []

    async def complete(self, req: CompletionRequest) -> str:
        system = req.messages[0].content
        for cue, reply in self.replies.items():
            if cue in system:
                self.asked.append(cue)
                return json.dumps(reply)
        return "not json"

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


def _bare_pack(tmp_path: Path, name: str) -> Path:
    pack = tmp_path / "bare"
    pack.mkdir()
    (pack / "world.toml").write_text(f'[world]\nname = "{name}"\n')
    return pack


async def test_a_pack_with_only_a_name_is_grown_into_a_runnable_one(tmp_path: Path) -> None:
    pack = parse_pack(_bare_pack(tmp_path, "Saltmarsh"))
    before = check_sufficiency(pack)
    assert before.grade == "insufficient" and len(before.gaps) == 6
    smith = _Worldsmith()
    grown, added = await backfill_gaps(pack, ProviderRouter(bindings={}, default=smith))
    assert check_sufficiency(grown).grade == "runnable"
    assert grown.manifest.backfilled == [
        "geography",
        "power",
        "population",
        "conflict",
        "tone",
        "history",
    ]
    # every invention is tagged, and only inventions
    assert {p.provenance for p in grown.places} == {"ai_backfill"}
    assert {a.provenance for a in grown.actors} == {"ai_backfill"}
    assert grown.factions[0].provenance == "ai_backfill"
    assert grown.threads[0].provenance == "ai_backfill"
    assert {c.provenance for c in grown.claims} == {"ai_backfill"} and len(grown.claims) == 4
    # places nest by name; people are placed and affiliated by name, unknown names left out
    region, town, chapel = grown.places
    assert (region.parent, town.parent, chapel.parent) == (None, region.id, town.id)
    ada, _tam, sile = grown.actors
    assert (ada.location, ada.faction) == (town.id, grown.factions[0].id)
    assert (sile.location, sile.faction) == (None, None)
    # at most one person of standing among those invented
    assert [a.tier for a in grown.actors] == [2, 1, 1]
    # the past is claims (they reach play), named subjects linked, and its overview is lore
    assert set(grown.claims[2].subject_refs) == {ada.id}
    assert HISTORY_LORE in grown.lore and "stilts" in grown.lore[HISTORY_LORE]
    assert grown.manifest.tone == ["damp", "stubborn", "quiet"]
    assert len(added) == 3 + 1 + 3 + 1 + 1 + 4


async def test_a_dimension_the_pack_covers_is_left_alone(tmp_path: Path) -> None:
    thornwood = parse_pack(Path(__file__).resolve().parents[3] / "worlds" / "thornwood")
    smith = _Worldsmith()
    grown, _ = await backfill_gaps(thornwood, ProviderRouter(bindings={}, default=smith))
    assert smith.asked == ["conflict seed"]  # thornwood lacks only a conflict
    assert grown.places == thornwood.places and grown.actors == thornwood.actors
    assert grown.manifest.backfilled == ["conflict"]


async def test_a_reply_that_cannot_be_used_fills_nothing(tmp_path: Path) -> None:
    pack = parse_pack(_bare_pack(tmp_path, "Nowhere"))
    smith = _Worldsmith(replies={})  # every answer is "not json"
    grown, added = await backfill_gaps(pack, ProviderRouter(bindings={}, default=smith))
    assert added == [] and grown.manifest.backfilled == []
    assert check_sufficiency(grown).grade == "insufficient"


async def test_backfilled_seeds_are_committed_with_their_tags(
    tmp_path: Path, store: PostgresEventStore
) -> None:
    pack = parse_pack(_bare_pack(tmp_path, f"Saltmarsh-{new_id()}"))
    router = ProviderRouter(bindings={}, default=_Worldsmith())
    made = await import_pack(store, pack, backfill_router=router)  # insufficient, then grown
    assert made.grade == "runnable" and made.backfilled
    branch = made.world.main_branch_id
    state = (await store.query_across([branch], ["places", "actors", "factions", "claims"]))[branch]
    assert {r["provenance"] for r in state["places"]} == {"ai_backfill"}
    assert {r["provenance"] for r in state["actors"]} == {"ai_backfill"}
    assert {r["provenance"] for r in state["factions"]} == {"ai_backfill"}
    assert {r["origin"] for r in state["claims"]} == {"ai_backfill"}
    threads = await store.query_across([branch], ["threads"])
    assert {r["provenance"] for r in threads[branch]["threads"]} == {"ai_backfill"}
    # the record of what was filled is kept with the world (D-104)
    assert (await store.world_manifest(made.world.world_id))["backfilled"][0] == "geography"
    # an authored pack's entities say so
    authored = parse_pack(Path(__file__).resolve().parents[3] / "worlds" / "thornwood")
    authored.manifest.name = f"Thornwood-{new_id()}"
    world = (await import_pack(store, authored)).world
    rows = (await store.query_across([world.main_branch_id], ["places"]))[world.main_branch_id]
    assert {r["provenance"] for r in rows["places"]} == {"author"}
