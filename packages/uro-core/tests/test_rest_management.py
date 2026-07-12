"""B3 — the REST management surface (docs/18): the CRUD/read endpoints a non-Python / non-co-located
consumer needs. Driven here (not in uro-server tests) because the endpoints run against the REAL
`EngineStore` (the DB `store` fixture), which lives in uro-core; the uro-server tests exercise the
transport (auth/broadcast) with fake deps. Deterministic — no LLM (management ops don't run a beat).
"""

from collections.abc import AsyncIterator

import httpx
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_server.app import ServerDeps, create_app, engine_deps

_TOK = "tok-mgmt"


class _Stub:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "x"

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _client(store: PostgresEventStore) -> httpx.AsyncClient:
    """An ASGI-transport client over the real engine-backed app (usable inside an async test —
    TestClient would deadlock against the already-running event loop)."""
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    app = create_app(engine_deps(store, engine, {_TOK: "alice"}))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _q(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}token={_TOK}"


async def test_management_surface_world_campaign_join_reads(store: PostgresEventStore) -> None:
    async with _client(store) as client:
        # create a world
        r = await client.post(_q("/worlds"), json={"name": "Managed"})
        assert r.status_code == 200, r.text
        world_id = r.json()["world_id"]

        # it shows in the list
        listed = (await client.get(_q("/worlds"))).json()
        assert world_id in [w["world_id"] for w in listed]

        # start a campaign (fresh PC) under it
        r = await client.post(
            _q(f"/worlds/{world_id}/campaigns"),
            json={"participant": "alice", "new_pc_name": "Ash"},
        )
        assert r.status_code == 200, r.text
        campaign_id = r.json()["campaign_id"]

        # a second participant joins on their own PC
        r = await client.post(
            _q(f"/campaigns/{campaign_id}/join"),
            json={"participant": "bob", "new_pc_name": "Bane"},
        )
        assert r.status_code == 200, r.text

        # the roster now has two distinct PCs
        roster = (await client.get(_q(f"/campaigns/{campaign_id}/roster"))).json()["pcs"]
        assert len(roster) == 2

        # the campaign shows in the list + a single-fetch
        listed_c = (await client.get(_q("/campaigns"))).json()
        assert campaign_id in [c["campaign_id"] for c in listed_c]
        one = (await client.get(_q(f"/campaigns/{campaign_id}"))).json()
        assert one["campaign_id"] == campaign_id

        # state read: actors section is populated (the two PCs at least)
        state = (await client.get(_q(f"/campaigns/{campaign_id}/state?sections=actors"))).json()
        assert len(state["state"]["actors"]) >= 2

        # a time-skip advances world time via the engine-backed agenda tick
        r = await client.post(_q(f"/campaigns/{campaign_id}/time-skip"), json={"days": 5})
        assert r.status_code == 200, r.text
        assert r.json()["world_day"] == 5

        # chronicle read returns a (possibly empty) beat list, not an error
        chron = (await client.get(_q(f"/campaigns/{campaign_id}/chronicle"))).json()
        assert isinstance(chron["beats"], list)


async def test_rest_campaign_pins_ruleset_and_sheets_the_pc(store: PostgresEventStore) -> None:
    # holistic-review fix (B3 x D-30 + Phase-3): a REST-created campaign must pin the world's
    # resolved ruleset (not "") and sheet its PC, matching the CLI path — else mechanics never
    # engage and the WS cross-ruleset guard is bypassed by an empty pin.
    async with _client(store) as client:
        world_id = (await client.post(_q("/worlds"), json={"name": "Pinned"})).json()["world_id"]
        c = (
            await client.post(
                _q(f"/worlds/{world_id}/campaigns"),
                json={"participant": "alice", "new_pc_name": "Ash"},
            )
        ).json()["campaign_id"]
        # the campaign pins the resolved default ruleset (non-empty) — D-30
        assert (await client.get(_q(f"/campaigns/{c}"))).json()["ruleset_id"] == "uro-basic"
        # the fresh PC has a character sheet — Phase-3 (visible in the sheets projection)
        sheets = (await client.get(_q(f"/campaigns/{c}/state?sections=sheets"))).json()
        assert len(sheets["state"]["sheets"]) >= 1
        # a joining participant is likewise sheeted
        await client.post(
            _q(f"/campaigns/{c}/join"), json={"participant": "bob", "new_pc_name": "Bane"}
        )
        sheets2 = (await client.get(_q(f"/campaigns/{c}/state?sections=sheets"))).json()
        assert len(sheets2["state"]["sheets"]) >= 2


async def test_bad_query_params_are_400(store: PostgresEventStore) -> None:
    # holistic-review fix (B3 x B5 + B3-internal): read endpoints honor the same bad-input→400
    # contract as the mutating ones (was: unknown ?sections= / non-int ?limit= / days<=0 → 500).
    async with _client(store) as client:
        world_id = (await client.post(_q("/worlds"), json={"name": "R"})).json()["world_id"]
        c = (
            await client.post(
                _q(f"/worlds/{world_id}/campaigns"),
                json={"participant": "alice", "new_pc_name": "Ash"},
            )
        ).json()["campaign_id"]
        assert (await client.get(_q(f"/campaigns/{c}/state?sections=bogus"))).status_code == 400
        assert (await client.get(_q(f"/campaigns/{c}/chronicle?limit=abc"))).status_code == 400
        assert (
            await client.post(_q(f"/campaigns/{c}/time-skip"), json={"days": 0})
        ).status_code == 400
        assert (
            await client.post(_q(f"/campaigns/{c}/time-skip"), json={"days": -5})
        ).status_code == 400


async def test_management_surface_requires_auth(store: PostgresEventStore) -> None:
    async with _client(store) as client:
        assert (await client.get("/worlds")).status_code == 401  # no token
        assert (await client.get("/worlds?token=nope")).status_code == 401  # unknown token


async def test_management_surface_404s(store: PostgresEventStore) -> None:
    async with _client(store) as client:
        assert (await client.get(_q("/campaigns/nope"))).status_code == 404
        r = await client.post(_q("/worlds/nope/campaigns"), json={"participant": "alice"})
        assert r.status_code == 404


async def test_bad_rule_pack_is_400_not_500(store: PostgresEventStore) -> None:
    # cross-item seam (B3 REST x the landed "validate rule-pack loudly" fix): create_world raises
    # a pydantic ValidationError (a ValueError) on a bad pack → the REST surface must 400, not 500.
    async with _client(store) as client:
        r = await client.post(
            _q("/worlds"), json={"name": "Bad", "rule_pack": {"version": 999, "rules": []}}
        )
        assert r.status_code == 400


async def test_malformed_body_is_400_not_500(store: PostgresEventStore) -> None:
    async with _client(store) as client:
        # missing required field → 400, not a bare KeyError→500
        assert (await client.post(_q("/worlds"), json={})).status_code == 400
        world_id = (await client.post(_q("/worlds"), json={"name": "W"})).json()["world_id"]
        c = (
            await client.post(
                _q(f"/worlds/{world_id}/campaigns"),
                json={"participant": "alice", "new_pc_name": "Ash"},
            )
        ).json()["campaign_id"]
        # non-integer days → 400, not 500
        r = await client.post(_q(f"/campaigns/{c}/time-skip"), json={"days": "soon"})
        assert r.status_code == 400


async def test_start_campaign_needs_exactly_one_pc_choice(store: PostgresEventStore) -> None:
    async with _client(store) as client:
        world_id = (await client.post(_q("/worlds"), json={"name": "W"})).json()["world_id"]
        # neither new_pc_name nor adopt_actor_id → 400, not a 500
        r = await client.post(_q(f"/worlds/{world_id}/campaigns"), json={"participant": "alice"})
        assert r.status_code == 400


async def test_management_surface_501_without_store() -> None:
    async def campaign_exists(campaign_id: str) -> bool:
        return False

    async def run_beat(campaign_id: str, participant: str, text: str) -> AsyncIterator[str]:
        yield ""

    deps = ServerDeps(
        resolve_participant=lambda t: "alice" if t == _TOK else None,
        campaign_exists=campaign_exists,
        run_beat=run_beat,
    )  # no store → management endpoints return 501
    app = create_app(deps)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get(_q("/worlds"))).status_code == 501
