"""Phase 5 inc 5.2: export/import with hash-chain verification (docs/03, 07, 08).

The acceptance's leg (b): a world exported from one machine imports and continues on another —
here, exported from the store, verified, re-instantiated as a fresh world, and played on. A
bundle altered in transit is rejected before anything is written.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.engines.history import seed_history
from uro_core.errors import ExportError
from uro_core.export import (
    BundleBranch,
    BundleCommit,
    BundleEvent,
    WorldBundle,
    stamp_chain,
    verify_bundle,
)
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.rng import Rng
from uro_core.worldpack.importer import pack_to_events
from uro_core.worldpack.parse import parse_pack

WORLDS = Path(__file__).resolve().parents[3] / "worlds"


class _Stub:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "The pier groans under the grey tide."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


# --- pure: the hash chain detects tampering ---


def test_verify_bundle_detects_a_mutated_event() -> None:
    event = BundleEvent(
        event_id="e1",
        seq=0,
        event_type="WorldGenesis",
        caused_by={"kind": "system"},
        payload={"world_name": "X"},
    )
    bundle = WorldBundle(
        world_name="X",
        commits=[BundleCommit(commit_id="c1", parent_id=None, depth=0, events=[event])],
        branches=[BundleBranch(branch_id="b1", name="main", head_commit="c1")],
    )
    stamp_chain(bundle)
    verify_bundle(bundle)  # untampered → ok
    bundle.commits[0].events[0].payload = {"world_name": "HACKED"}
    with pytest.raises(ExportError):
        verify_bundle(bundle)


# --- round-trip against the store: export → verify → import → continue ---


async def test_export_import_roundtrip_and_continue(store: PostgresEventStore) -> None:
    pack = parse_pack(WORLDS / "ashfall")
    src = await store.create_world(
        pack.manifest.name, tone=pack.manifest.tone, extra_events=pack_to_events(pack)
    )
    await store.append_beat(src.main_branch_id, seed_history(pack.manifest, Rng(42)))

    bundle = await store.export_world(src.world_id)
    verify_bundle(bundle)  # the exported bundle is self-consistent
    assert len(bundle.commits) == 2  # genesis (import) + the seeding commit

    dst = await store.import_world(bundle)
    assert dst.world_id != src.world_id  # a fresh instance

    # the imported world carries the same authored geography, factions, and conflict seeds
    src_b, dst_b = src.main_branch_id, dst.main_branch_id
    assert {p.place_id for p in await store.list_places(dst_b)} == {
        p.place_id for p in await store.list_places(src_b)
    }
    assert {f.faction_id for f in await store.list_factions(dst_b)} == {
        f.faction_id for f in await store.list_factions(src_b)
    }
    assert {t.thread_id for t in await store.list_threads(dst_b)} == {
        t.thread_id for t in await store.list_threads(src_b)
    }

    # ...and it CONTINUES: a campaign starts on the imported world and a beat commits
    campaign = await store.start_campaign(
        dst.world_id, dst_b, participant_id="p1", new_pc_name="Nomad", new_pc_id="a:nomad"
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    result = await engine.run_beat(campaign, "p1", "I walk the drowned pier")
    assert result.commit_id


async def test_export_import_carries_semantic_memory(store: PostgresEventStore) -> None:
    # Cross-phase (P1 memory x P5 export): an import must keep long-range recall, like a fork does —
    # else the flagship P1 recall thesis silently breaks on the imported world.
    world = await store.create_world("MemWorld")
    campaign = await store.start_campaign(
        world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="A", new_pc_id="a:a"
    )
    engine = Engine(
        store, ProviderRouter(bindings={}, default=_Stub())
    )  # narrates "The pier groans…"
    await engine.run_beat(campaign, "p1", "I look around")  # populates memory_index + embeddings

    query = hashing_embedding("The pier groans under the grey tide.")
    assert await store.search(world.main_branch_id, query, 3)  # the source recalls it

    dst = await store.import_world(await store.export_world(world.world_id))
    hits = await store.search(dst.main_branch_id, query, 3)
    assert hits and any("pier" in h.text for h in hits)  # ...and so does the import


async def test_tampered_bundle_is_rejected_before_write(store: PostgresEventStore) -> None:
    pack = parse_pack(WORLDS / "ashfall")
    src = await store.create_world(pack.manifest.name, extra_events=pack_to_events(pack))
    bundle = await store.export_world(src.world_id)
    # forge a place name in the genesis commit
    genesis = bundle.commits[0]
    place_ev = next(e for e in genesis.events if e.event_type == "PlaceCreated")
    place_ev.payload["name"] = "Forgeton"
    with pytest.raises(ExportError):
        await store.import_world(bundle)  # verification fails → nothing written
