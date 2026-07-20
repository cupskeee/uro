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
    # _TOK is the OPERATOR credential (may seat/mint for others, D-39); ordinary players are minted.
    app = create_app(engine_deps(store, engine, {_TOK: "alice"}, admin_tokens={_TOK}))
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


# --- D-39 (#10): runtime session tokens over the real store (mint-on-join, scope, revoke) ---


async def _world_campaign(client: httpx.AsyncClient, name: str) -> str:
    world_id = (await client.post(_q("/worlds"), json={"name": name})).json()["world_id"]
    return (
        await client.post(
            _q(f"/worlds/{world_id}/campaigns"),
            json={"participant": "alice", "new_pc_name": "Ash"},
        )
    ).json()["campaign_id"]


async def test_rest_mint_on_join_returns_a_working_token(store: PostgresEventStore) -> None:
    async with _client(store) as client:
        cid = await _world_campaign(client, "Tok")
        # alice (the bootstrap/operator token) seats bob → mint-on-join issues bob a durable token
        r = await client.post(
            _q(f"/campaigns/{cid}/join"), json={"participant": "bob", "new_pc_name": "Bane"}
        )
        assert r.status_code == 200, r.text
        bob_token = r.json()["token"]
        assert bob_token
        # bob's minted token authenticates an authed call (it resolves to bob) — no server restart
        got = await client.get(f"/campaigns/{cid}/roster?token={bob_token}")
        assert got.status_code == 200


async def test_rest_join_scope_blocks_seating_another_without_operator(
    store: PostgresEventStore,
) -> None:
    async with _client(store) as client:
        cid = await _world_campaign(client, "Scope")
        bob_token = (
            await client.post(
                _q(f"/campaigns/{cid}/join"), json={"participant": "bob", "new_pc_name": "Bane"}
            )
        ).json()["token"]
        # bob (a non-operator token) may NOT seat carol → 403 (no arbitrary-identity mint)
        r = await client.post(
            f"/campaigns/{cid}/join?token={bob_token}",
            json={"participant": "carol", "new_pc_name": "Cy"},
        )
        assert r.status_code == 403
        # but bob acting as HIMSELF is fine (idempotent re-join)
        r2 = await client.post(
            f"/campaigns/{cid}/join?token={bob_token}", json={"participant": "bob"}
        )
        assert r2.status_code == 200


async def test_rest_revoke_denies_further_use(store: PostgresEventStore) -> None:
    async with _client(store) as client:
        cid = await _world_campaign(client, "Rev")
        bob_token = (
            await client.post(
                _q(f"/campaigns/{cid}/join"), json={"participant": "bob", "new_pc_name": "Bane"}
            )
        ).json()["token"]
        assert (await client.get(f"/campaigns/{cid}/roster?token={bob_token}")).status_code == 200
        # alice (operator) revokes bob's token
        rv = await client.post(_q(f"/campaigns/{cid}/tokens/revoke"), json={"token": bob_token})
        assert rv.status_code == 200 and rv.json()["revoked"] is True
        # now bob's token is denied (blocks a NEW connect/call)
        assert (await client.get(f"/campaigns/{cid}/roster?token={bob_token}")).status_code == 401


async def test_rest_non_operator_static_token_cannot_seat_another(
    store: PostgresEventStore,
) -> None:
    # D-39 review: the bug was that EVERY --token peer was treated as admin. A plain PLAYER token
    # (a static token NOT in admin_tokens) must NOT be able to seat/mint for another participant.
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    app = create_app(engine_deps(store, engine, {"op": "gm", "pleb": "carol"}, admin_tokens={"op"}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        wid = (await client.post("/worlds?token=op", json={"name": "W"})).json()["world_id"]
        cid = (
            await client.post(
                f"/worlds/{wid}/campaigns?token=op",
                json={"participant": "gm", "new_pc_name": "G"},
            )
        ).json()["campaign_id"]
        # carol (a plain --token peer, NOT an operator) tries to seat "dave" → 403
        r = await client.post(
            f"/campaigns/{cid}/join?token=pleb", json={"participant": "dave", "new_pc_name": "D"}
        )
        assert r.status_code == 403


async def test_outcome_endpoint_rejects_a_malformed_bundle(store: PostgresEventStore) -> None:
    # D-41 review: a forged extra field (extra='forbid') or an unsupported `v` must be a LOUD 400 at
    # the wire (the endpoint validates the bundle inside report_outcome), not an uncaught 500.
    async with _client(store) as client:
        cid = await _world_campaign(client, "Bad-bundle")
        forged = await client.post(
            _q(f"/campaigns/{cid}/encounters/e1/outcome"),
            json={"participants": [], "trusted": True},  # a forged trust field
        )
        assert forged.status_code == 400
        badv = await client.post(
            _q(f"/campaigns/{cid}/encounters/e1/outcome"),
            json={"participants": [], "v": 2},  # unsupported schema version
        )
        assert badv.status_code == 400


def _two_tier(store: PostgresEventStore) -> httpx.AsyncClient:
    """A real-engine app with an OPERATOR token ('op'→gm) and a PLAYER token ('pleb'→carol) — for
    the holistic-review authority + epistemic fixes (D-45/D-46)."""
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    app = create_app(engine_deps(store, engine, {"op": "gm", "pleb": "carol"}, admin_tokens={"op"}))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_holistic_review_authority_and_epistemic_fixes(store: PostgresEventStore) -> None:
    async with _two_tier(store) as c:

        def op(p: str) -> str:
            return f"{p}{'&' if '?' in p else '?'}token=op"

        def pl(p: str) -> str:
            return f"{p}{'&' if '?' in p else '?'}token=pleb"

        # create_world is now OPERATOR-only (D-46): a player token → 403, operator → 200.
        assert (await c.post(pl("/worlds"), json={"name": "PlebWorld"})).status_code == 403
        wr = await c.post(op("/worlds"), json={"name": "HRev"})
        assert wr.status_code == 200
        wid = wr.json()["world_id"]

        # start_campaign self-or-admin (D-39): a player may name only THEMSELVES.
        assert (
            await c.post(
                pl(f"/worlds/{wid}/campaigns"),
                json={"participant": "not-carol", "new_pc_name": "B"},
            )
        ).status_code == 403
        cr = await c.post(
            pl(f"/worlds/{wid}/campaigns"), json={"participant": "carol", "new_pc_name": "Carol PC"}
        )
        assert cr.status_code == 200
        cid = cr.json()["campaign_id"]

        # a non-int seed (JSON array) → 400, not a 500 (TypeError used to escape the catch).
        assert (
            await c.post(
                op(f"/worlds/{wid}/campaigns"),
                json={"participant": "z", "new_pc_name": "Z", "seed": [1, 2]},
            )
        ).status_code == 400

        # D-45 epistemic boundary: a player CANNOT read claims/beliefs; an operator can.
        assert (await c.get(pl(f"/campaigns/{cid}/state?sections=claims"))).status_code == 403
        assert (
            await c.get(pl(f"/campaigns/{cid}/state?sections=actors,beliefs"))
        ).status_code == 403
        assert (
            await c.get(pl(f"/campaigns/{cid}/state?sections=actors,threads,places,factions,pcs"))
        ).status_code == 200
        assert (
            await c.get(op(f"/campaigns/{cid}/state?sections=claims,beliefs"))
        ).status_code == 200

        # time-skip is now OPERATOR-only (D-46) + a day cap.
        assert (
            await c.post(pl(f"/campaigns/{cid}/time-skip"), json={"days": 5})
        ).status_code == 403
        assert (
            await c.post(op(f"/campaigns/{cid}/time-skip"), json={"days": 10**9})
        ).status_code == 400
        assert (
            await c.post(op(f"/campaigns/{cid}/time-skip"), json={"days": 3})
        ).status_code == 200

        # Campaign.seed (mechanics RNG) is hidden from a player, exposed to an operator.
        assert "seed" not in (await c.get(pl(f"/campaigns/{cid}"))).json()
        assert "seed" in (await c.get(op(f"/campaigns/{cid}"))).json()

        # roster on an unknown campaign is 404 (was 200); a negative ?limit is 400 (was a 500).
        assert (await c.get(op("/campaigns/nope/roster"))).status_code == 404
        assert (await c.get(op(f"/campaigns/{cid}/chronicle?limit=-1"))).status_code == 400
