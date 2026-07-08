"""Phase 4 inc 4.2: world import + procedural History seeding (docs/09, 03). Deterministic.

Import commits the authored geography/factions/actors/relations (emitter S) at WorldGenesis;
History seeding layers seed-dependent dynasties/wars (emitter H) on top — so seed 42 and seed
43 produce different histories on IDENTICAL geography (the Phase-4 acceptance).
"""

from collections.abc import AsyncIterator
from pathlib import Path

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.engines.history import seed_history
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.rng import Rng
from uro_core.worldpack.importer import pack_to_events
from uro_core.worldpack.parse import parse_pack

WORLDS = Path(__file__).resolve().parents[3] / "worlds"


def _faction_names(events: list) -> list[str]:
    return [e.payload["name"] for e in events if e.event_type == "FactionCreated"]


def test_seed_history_reproducible_and_seed_varying() -> None:
    manifest = parse_pack(WORLDS / "ashfall").manifest
    a, a2 = seed_history(manifest, Rng(42)), seed_history(manifest, Rng(42))
    assert [e.payload for e in a] == [e.payload for e in a2]  # same seed → byte-identical
    assert a[0].event_type == "HistorySeeded" and a[0].payload["seed"] == 42
    # real RNG divergence (not just seed-stamped ids): the generated house names differ
    assert _faction_names(a) != _faction_names(seed_history(manifest, Rng(43)))


async def test_import_commits_authored_entities(store: PostgresEventStore) -> None:
    pack = parse_pack(WORLDS / "ashfall")
    world = await store.create_world(pack.manifest.name, extra_events=pack_to_events(pack))
    main = world.main_branch_id
    assert {p.kind for p in await store.list_places(main)} == {"region", "settlement", "site"}
    assert {f.faction_id for f in await store.list_factions(main)} == {"f:duchy", "f:saltborn"}
    # cross-linked relations became edges
    wars = await store.list_edges(main, "at_war_with")
    assert any(e.src == "f:duchy" and e.dst == "f:saltborn" for e in wars)
    members = await store.list_edges(main, "member_of")
    assert any(e.src == "a:halbrecht" and e.dst == "f:duchy" for e in members)
    located = await store.list_edges(main, "located_in")
    assert any(e.src == "p:vel" and e.dst == "p:vael" for e in located)


async def test_seed_42_vs_43_differ_on_identical_geography(store: PostgresEventStore) -> None:
    pack = parse_pack(WORLDS / "ashfall")
    # fresh event objects per import (each DomainEvent carries a unique event_id)
    wa = await store.create_world(pack.manifest.name, extra_events=pack_to_events(pack))
    wb = await store.create_world(pack.manifest.name, extra_events=pack_to_events(pack))
    await store.append_beat(wa.main_branch_id, seed_history(pack.manifest, Rng(42)))
    await store.append_beat(wb.main_branch_id, seed_history(pack.manifest, Rng(43)))

    def places(branch: str) -> list[tuple[str, str, str]]:
        return sorted((p.place_id, p.name, p.kind) for p in _places[branch])

    _places = {
        wa.main_branch_id: await store.list_places(wa.main_branch_id),
        wb.main_branch_id: await store.list_places(wb.main_branch_id),
    }
    # IDENTICAL geography — the authored places are seed-independent
    assert places(wa.main_branch_id) == places(wb.main_branch_id)

    def dynasties(fs: list) -> list[str]:
        return sorted(f.name for f in fs if f.faction_id.startswith("f:seed"))

    fa = await store.list_factions(wa.main_branch_id)
    fb = await store.list_factions(wb.main_branch_id)
    assert dynasties(fa) != dynasties(fb)  # DIFFERENT history (seeded dynasties diverge)
    # the authored factions survive identically in both siblings
    authored = {f.faction_id for f in fa if not f.faction_id.startswith("f:seed")}
    assert authored == {"f:duchy", "f:saltborn"}
    assert authored == {f.faction_id for f in fb if not f.faction_id.startswith("f:seed")}


async def test_world_style_stored_on_import(store: PostgresEventStore) -> None:
    pack = parse_pack(WORLDS / "ashfall")
    world = await store.create_world(
        pack.manifest.name,
        tone=pack.manifest.tone,
        prompt_overrides=pack.prompts,
        extra_events=pack_to_events(pack),
    )
    style, overrides = await store.world_style(world.main_branch_id)
    assert style == "grim, low-magic, political"
    assert overrides == {}
    # a world created WITHOUT a pack carries no tone
    plain = await store.create_world("Plain")
    assert await store.world_style(plain.main_branch_id) == ("", {})


async def test_imported_world_plays_in_authored_tone(store: PostgresEventStore) -> None:
    # The acceptance's last leg: a campaign on an imported world narrates in the pack's tone —
    # the manifest tone reaches the narrator system prompt end-to-end.
    pack = parse_pack(WORLDS / "ashfall")
    world = await store.create_world(
        pack.manifest.name,
        tone=pack.manifest.tone,
        prompt_overrides=pack.prompts,
        extra_events=pack_to_events(pack),
    )
    campaign = await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Rho",
        new_pc_id="a:rho",
    )
    captured: dict[str, str] = {}

    class _Spy:
        async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
            captured["narrator"] = "\n".join(m.content for m in req.messages if m.role == "system")
            yield "You step onto Vel's rotting pier."

        async def complete(self, req: CompletionRequest) -> str:
            return '{"actors": [], "claims": []}'

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [hashing_embedding(t) for t in texts]

    engine = Engine(store, ProviderRouter(bindings={}, default=_Spy()))  # no ruleset → free-roam
    await engine.run_beat(campaign, "p1", "I look around Vel")
    assert "grim" in captured["narrator"] and "political" in captured["narrator"]


async def test_authored_threads_import_as_provenance_tagged_state(
    store: PostgresEventStore,
) -> None:
    pack = parse_pack(WORLDS / "ashfall")
    world = await store.create_world(pack.manifest.name, extra_events=pack_to_events(pack))
    threads = await store.list_threads(world.main_branch_id)
    assert any(t.thread_id == "t:saltborn-ritual" and t.provenance == "author" for t in threads)


async def test_backfilled_seed_commits_as_ai_backfill_state(store: PostgresEventStore) -> None:
    # The acceptance's real leg: backfill → import commits a ThreadCreated tagged ai_backfill —
    # queryable committed state, not a discarded in-memory model.
    from uro_core.worldpack.backfill import backfill_gaps

    class _Worldsmith:
        async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
            yield ""

        async def complete(self, req: CompletionRequest) -> str:
            return '{"stakes": "The Council hides a blight in the orchard.", "state": "dormant"}'

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [hashing_embedding(t) for t in texts]

    pack = parse_pack(WORLDS / "thornwood")
    augmented, added = await backfill_gaps(pack, ProviderRouter(bindings={}, default=_Worldsmith()))
    assert added  # a seed was generated
    world = await store.create_world(
        augmented.manifest.name, extra_events=pack_to_events(augmented)
    )
    backfilled = [
        t for t in await store.list_threads(world.main_branch_id) if t.provenance == "ai_backfill"
    ]
    assert len(backfilled) == 1 and "blight" in backfilled[0].stakes.lower()


async def test_authored_aliases_resolve_colloquial_references(store: PostgresEventStore) -> None:
    # Live-run finding (2026-07-09): emberfell's "Cass Holloway" gained a "Cass" alias so a
    # colloquial reference resolves to the authored NPC instead of the extractor minting a
    # duplicate (the OQ-3 fragmentation the PbtA live run hit). find_actor_by_name matches aliases.
    pack = parse_pack(WORLDS / "emberfell")
    cass = next(a for a in pack.actors if a.id == "a:cass")
    assert "Cass" in cass.aliases  # the pack now carries the colloquial alias
    world = await store.create_world(pack.manifest.name, extra_events=pack_to_events(pack))
    hit = await store.find_actor_by_name(world.main_branch_id, "Cass")
    assert hit is not None and hit.actor_id == "a:cass"  # "Cass" → the authored NPC, no duplicate
