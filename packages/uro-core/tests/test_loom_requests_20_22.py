"""Uro Loom requests 20-22 (D-113): things a character holds from the start, choices a ruleset
leaves for a player, and characters a world pack suggests. Deterministic; scripted model replies."""

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import (
    ChoiceError,
    answer_character_choice,
    chosen_holdings,
    join_campaign,
    own_character,
    rebuild_character,
    start_campaign,
)
from uro_core.domain.events import beat_resolved, sheet_updated
from uro_core.domain.ids import new_id
from uro_core.errors import PackError
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import CharSpecError, Holding
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.worldpack.backfill import backfill_gaps
from uro_core.worldpack.create import import_pack, ruleset_problem
from uro_core.worldpack.parse import parse_pack
from uro_server.app import create_app, engine_deps

ASHFALL = Path(__file__).parents[3] / "worlds" / "ashfall"


class _Narrator:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "Time moves."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def _beat(store: PostgresEventStore, branch: str) -> None:
    await store.append_beat(
        branch,
        [beat_resolved(beat_id=new_id(), participant_id="p", intent_text="go", narration=".")],
    )


# --- 20: things held from the start ---


async def test_a_character_starts_holding_what_the_player_chose(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Hold-{new_id()}", starting_gear=["lantern"])
    c = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p",
        new_pc_name="Ash",
        holdings=[{"name": "grandmother's compass", "data": {"keepsake": True}}, {"name": "rope"}],
    )
    held = {h["name"]: h for h in (await own_character(store, c, "p")).holdings}
    assert set(held) == {"lantern", "grandmother's compass", "rope"}
    assert held["grandmother's compass"]["data"] == {"keepsake": True}
    assert held["grandmother's compass"]["kind"] == "chosen" and held["lantern"]["kind"] == ""

    # a rebuild before the story starts replaces what was chosen; the world's gear stays
    view = await rebuild_character(store, c, "p", holdings=[{"name": "flint"}])
    assert sorted(h["name"] for h in view.holdings) == ["flint", "lantern"]
    await _beat(store, world.main_branch_id)
    from uro_core.characters import CharacterFrozen

    with pytest.raises(CharacterFrozen):
        await rebuild_character(store, c, "p", holdings=[])

    # a joiner chooses theirs too; malformed entries are located
    actor = await join_campaign(
        store, c, participant_id="q", new_pc_name="Bryn", holdings=[{"name": "map"}]
    )
    assert "map" in {h["name"] for h in await store.holdings(world.main_branch_id, actor)}
    with pytest.raises(CharSpecError) as err:
        await join_campaign(
            store, c, participant_id="r", new_pc_name="Cael", holdings=[{"name": ""}]
        )
    assert err.value.problems[0].loc == ["holdings", 0, "name"]
    with pytest.raises(CharSpecError, match="new character"):
        await join_campaign(
            store, c, participant_id="s", adopt_actor_id=actor, holdings=[{"name": "x"}]
        )


def test_a_ruleset_may_check_starting_holdings() -> None:
    class Strict(UroBasic):
        def check_holdings(self, holdings: list[Holding]) -> None:
            if len(holdings) > 1:
                raise ValueError("one keepsake only")

    assert chosen_holdings(Strict(), [{"name": "ring"}]) == [
        {"name": "ring", "data": {}, "kind": "chosen"}
    ]
    with pytest.raises(CharSpecError) as err:
        chosen_holdings(Strict(), [{"name": "ring"}, {"name": "coin"}])
    assert (
        err.value.problems[0].loc[0] == "holdings" and "keepsake" in err.value.problems[0].message
    )
    assert chosen_holdings(UroBasic(), [{"name": "a"}, {"name": "b"}])  # no hook: anything goes


# --- 21: a choice a ruleset leaves for a player ---


async def _choosing_campaign(store: PostgresEventStore):  # type: ignore[no-untyped-def]
    world = await store.create_world(
        f"Grow-{new_id()}", ruleset_id="uro-basic", ruleset_config={"ability_choice_on_level": True}
    )
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", new_pc_name="Ash"
    )
    actor = await store.pc_for_participant(c.campaign_id, "p")
    assert actor is not None
    rs = UroBasic({"ability_choice_on_level": True})
    from uro_core.rulesets.base import Award

    levelled = rs.progression(await store.get_sheet(c.branch_id, actor), Award(data={"levels": 1}))  # type: ignore[arg-type]
    await store.append_beat(
        c.branch_id, [sheet_updated(actor_id=actor, sheet=levelled, ruleset_id=rs.id)]
    )
    return c, actor


async def test_a_pending_choice_is_read_answered_once_and_kept_in_the_sheet(
    store: PostgresEventStore,
) -> None:
    c, actor = await _choosing_campaign(store)
    view = await own_character(store, c, "p")
    assert [ch.id for ch in view.choices] == ["raise-ability"]
    before = view.sheet
    with pytest.raises(CharSpecError) as err:
        await answer_character_choice(store, c, "p", "raise-ability", {"ability": "LUCK"})
    assert err.value.problems[0].loc[0] == "answer"
    view = await answer_character_choice(store, c, "p", "raise-ability", {"ability": "CON"})
    assert view.sheet["abilities"]["CON"] == before["abilities"]["CON"] + 1  # type: ignore[index]
    assert view.choices == []
    with pytest.raises(CharSpecError, match="no such pending choice"):
        await answer_character_choice(store, c, "p", "raise-ability", {"ability": "STR"})
    # an answer computed from a sheet that has since changed is refused, not double-spent
    with pytest.raises(ValueError, match="changed meanwhile"):
        await store.amend_sheet(c.branch_id, actor, before, [])  # type: ignore[arg-type]
    assert ChoiceError  # (the characters-level wrapper of that refusal)


async def test_a_beat_tells_its_player_about_their_pending_choices(
    store: PostgresEventStore,
) -> None:
    c, _ = await _choosing_campaign(store)
    engine = Engine(
        store,
        ProviderRouter(bindings={}, default=_Narrator()),
        ruleset=UroBasic({"ability_choice_on_level": True}),
    )
    result = await engine.run_beat(c, "p", "I rest by the fire")
    assert [ch.id for ch in result.choices] == ["raise-ability"]  # play goes on; the choice waits


async def test_choices_over_rest(store: PostgresEventStore) -> None:
    c, _ = await _choosing_campaign(store)
    engine = Engine(store, ProviderRouter(bindings={}, default=_Narrator()))
    deps = engine_deps(store, engine, {"op": "alice", "pt": "p"}, admin_tokens={"op"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(deps)), base_url="http://t"
    ) as client:
        mine = (await client.get(f"/campaigns/{c.campaign_id}/character?token=pt")).json()
        assert mine["choices"][0]["id"] == "raise-ability"
        path = f"/campaigns/{c.campaign_id}/character/choices/raise-ability"
        r = await client.post(f"{path}?token=pt", json={"answer": {"ability": "nope"}})
        assert r.status_code == 400 and r.json()["detail"]["problems"]
        r = await client.post(f"{path}?token=pt", json={"answer": {"ability": "WIS"}})
        assert r.status_code == 200 and r.json()["choices"] == []
        r = await client.post(f"{path}?token=pt", json={"answer": {"ability": "WIS"}})
        assert r.status_code == 400


# --- 22: suggested characters in a world pack ---


def _pack_with_characters(tmp_path: Path, characters: list[dict[str, Any]]) -> Path:
    root = tmp_path / "pack"
    shutil.copytree(ASHFALL, root)
    (root / "entities" / "characters.yaml").write_text(json.dumps(characters))
    return root


async def test_a_pack_suggests_characters_a_campaign_can_begin_as(
    store: PostgresEventStore, tmp_path: Path
) -> None:
    root = _pack_with_characters(
        tmp_path,
        [
            {"id": "pc:wren", "name": "Wren", "line": "an ash-scout", "spec": {"level": 2}},
            {"id": "pc:oda", "name": "Oda", "portrait": "art/oda.png"},
        ],
    )
    pack = parse_pack(root)
    assert [c.id for c in pack.characters] == ["pc:wren", "pc:oda"]
    world = (await import_pack(store, pack)).world
    suggested = await store.world_characters(world.main_branch_id)
    assert [c["name"] for c in suggested] == ["Wren", "Oda"] and suggested[1]["portrait"]
    # nothing exists until a campaign begins as one
    assert await store.find_actor_by_name(world.main_branch_id, "Wren") is None
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p", character="pc:wren"
    )
    view = await own_character(store, c, "p")
    assert view.name == "Wren" and view.sheet["level"] == 2  # type: ignore[index]
    with pytest.raises(CharSpecError, match="no such suggested character"):
        await join_campaign(store, c, participant_id="q", character="pc:nobody")
    # a player may edit the build: a given spec wins over the suggestion's
    actor = await join_campaign(
        store, c, participant_id="q", character="pc:wren", spec={"level": 1}
    )
    assert (await store.get_sheet(world.main_branch_id, actor))["level"] == 1  # type: ignore[index]
    # export → import keeps the suggestions
    again = await store.import_world(await store.export_world(world.world_id))
    assert [c["id"] for c in await store.world_characters(again.main_branch_id)] == [
        "pc:wren",
        "pc:oda",
    ]


def test_a_suggested_characters_spec_must_build_under_the_packs_ruleset(tmp_path: Path) -> None:
    pack = parse_pack(
        _pack_with_characters(tmp_path, [{"id": "x", "name": "X", "spec": {"mana": 9}}])
    )
    problem = ruleset_problem(pack)
    assert problem is not None and "'x'" in problem
    with pytest.raises(PackError, match="twice"):
        parse_pack(
            _pack_with_characters(
                tmp_path / "2", [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}]
            )
        )


async def test_suggested_characters_over_rest_and_post_worlds(store: PostgresEventStore) -> None:
    engine = Engine(store, ProviderRouter(bindings={}, default=_Narrator()))
    deps = engine_deps(store, engine, {"op": "alice"}, admin_tokens={"op"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(deps)), base_url="http://t"
    ) as client:
        r = await client.post(
            "/worlds?token=op",
            json={
                "name": f"Sugg-{new_id()}",
                "characters": [{"id": "k", "name": "Kit", "spec": {"level": 9}}],
            },
        )
        assert r.status_code == 400 and "'k'" in r.json()["detail"]
        r = await client.post(
            "/worlds?token=op",
            json={
                "name": f"Sugg-{new_id()}",
                "characters": [{"id": "k", "name": "Kit", "line": "a fixer"}],
            },
        )
        assert r.status_code == 200, r.text
        w = r.json()["world_id"]
        listed = (await client.get(f"/worlds/{w}/characters?token=op")).json()["characters"]
        assert listed == [
            {
                "id": "k",
                "name": "Kit",
                "line": "a fixer",
                "portrait": "",
                "spec": {},
                "provenance": "author",
            }
        ]
        r = await client.post(
            f"/worlds/{w}/campaigns?token=op", json={"participant": "alice", "character": "k"}
        )
        assert r.status_code == 200, r.text


async def test_backfill_suggests_characters_when_a_pack_has_none() -> None:
    pack = parse_pack(ASHFALL)
    assert not pack.characters
    seen: list[str] = []

    class Worldsmith:
        async def complete(self, req: CompletionRequest) -> str:
            seen.append(req.messages[0].content)
            return json.dumps(
                {
                    "characters": [
                        {"name": "Tamsin", "line": "a reed-cutter", "spec": {"level": 2}},
                        {"name": "Hob", "line": "a bellows-boy", "spec": {"mana": 3}},
                    ]
                }
            )

    filled, added = await backfill_gaps(pack, ProviderRouter(bindings={}, default=Worldsmith()))
    assert [c.name for c in filled.characters] == ["Tamsin", "Hob"]
    assert all(c.provenance == "ai_backfill" for c in filled.characters)
    assert filled.characters[1].spec == {}  # a build the ruleset refuses is kept as no build
    assert "characters" in filled.manifest.backfilled and len(added) == 2
    assert '"level"' in seen[-1]  # the ruleset's spec schema rode along
