"""Proposing a character from a description (D-112, Uro Loom request 14): one model call, given
the ruleset's spec schema and the world's tone, whose reply is checked like a preview and saved
nowhere. Scripted providers; no live call."""

import json
from collections.abc import AsyncIterator

import httpx
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import PROPOSAL_ROLE, propose_character, start_campaign
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_server.app import create_app, engine_deps


class _Scripted:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: list[CompletionRequest] = []

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "x"

    async def complete(self, req: CompletionRequest) -> str:
        self.seen.append(req)
        return self.reply

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def _world(store: PostgresEventStore, **kw):  # type: ignore[no-untyped-def]
    return await store.create_world(f"Prop-{new_id()}", tone=["grim", "salt-stained"], **kw)


async def test_a_buildable_proposal(store: PostgresEventStore) -> None:
    world = await _world(store, ruleset_id="uro-pool")
    model = _Scripted(json.dumps({"name": "Maren", "spec": {"traits": {"might": 4, "wits": 1}}}))
    before = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
    p = await propose_character(
        store,
        ProviderRouter(bindings={}, default=model),
        branch_id=world.main_branch_id,
        description="a huge dockhand, strong as an ox and not much for thinking",
    )
    assert p.ok and p.name == "Maren" and p.ruleset == "uro-pool"
    assert p.sheet is not None and p.sheet["traits"]["might"] == 4
    req = model.seen[0]
    assert req.stage_tag == PROPOSAL_ROLE and req.json_mode
    assert "traits" in req.messages[0].content and "grim" in req.messages[0].content
    spec_schema = req.json_schema["properties"]["spec"]  # type: ignore[index]
    assert "traits" in spec_schema["properties"]  # the ruleset's own schema rides along
    after = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
    assert before == after  # nothing saved


async def test_an_unbuildable_or_empty_reply_is_a_proposal_with_problems(
    store: PostgresEventStore,
) -> None:
    world = await _world(store)
    c = await start_campaign(
        store,
        world.world_id,
        world.main_branch_id,
        participant_id="p",
        new_pc_name="Ash",
        ruleset_id="uro-pool",
        ruleset_config={"trait_cap": 3},
    )
    over = _Scripted(json.dumps({"spec": {"traits": {"might": 5}}}))
    p = await propose_character(
        store,
        ProviderRouter(bindings={}, default=over),
        branch_id=c.branch_id,
        campaign=c,
        description="the strongest person alive",
    )
    assert not p.ok and p.spec == {"traits": {"might": 5}}  # the player can fix it
    assert p.problems[0].loc == ["traits", "might"]  # the campaign's own cap
    junk = _Scripted("I'd rather not.")
    p = await propose_character(
        store,
        ProviderRouter(bindings={}, default=junk),
        branch_id=c.branch_id,
        campaign=c,
        description="anyone",
    )
    assert not p.ok and p.spec == {} and "no build" in p.problems[0].message


async def test_proposals_are_operator_only_over_rest(store: PostgresEventStore) -> None:
    world = await _world(store)
    model = _Scripted(json.dumps({"name": "Kit", "spec": {"level": 2}}))
    engine = Engine(store, ProviderRouter(bindings={}, default=model))
    deps = engine_deps(store, engine, {"op": "alice", "pl": "bob"}, admin_tokens={"op"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(deps)), base_url="http://t"
    ) as client:
        path = f"/worlds/{world.world_id}/character-proposal"
        r = await client.post(f"{path}?token=pl", json={"description": "a scout"})
        assert r.status_code == 403
        r = await client.post(f"{path}?token=op", json={"description": "   "})
        assert r.status_code == 400
        r = await client.post(f"{path}?token=op", json={"description": "a scout"})
        assert r.status_code == 200 and r.json()["ok"] and r.json()["spec"] == {"level": 2}
