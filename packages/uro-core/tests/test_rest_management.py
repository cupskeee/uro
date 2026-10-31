"""B3 — the REST management surface (docs/18): the CRUD/read endpoints a non-Python / non-co-located
consumer needs. Driven here (not in uro-server tests) because the endpoints run against the REAL
`EngineStore` (the DB `store` fixture), which lives in uro-core; the uro-server tests exercise the
transport (auth/broadcast) with fake deps. Deterministic — no LLM (management ops don't run a beat).
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
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


async def test_a_campaign_starts_on_any_branch_of_the_world(store: PostgresEventStore) -> None:
    # Parity with `uro campaign new --branch`: a fork is only playable once a campaign starts on it,
    # and the server used to start every campaign on `main`.
    async with _client(store) as client:
        world_id = (await client.post(_q("/worlds"), json={"name": "Forked"})).json()["world_id"]
        world = await store.get_world(world_id)
        assert world is not None
        head = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
        fork = await store.fork_branch(world_id, head, "what-if")  # type: ignore[arg-type]

        by_name = await client.post(
            _q(f"/worlds/{world_id}/campaigns"),
            json={"participant": "alice", "new_pc_name": "Ash", "branch": "what-if"},
        )
        assert by_name.status_code == 200, by_name.text
        assert by_name.json()["branch_id"] == fork.branch_id
        by_id = await client.post(
            _q(f"/worlds/{world_id}/campaigns"),
            json={"participant": "bob", "new_pc_name": "Bane", "branch": fork.branch_id},
        )
        assert by_id.status_code == 200 and by_id.json()["branch_id"] == fork.branch_id
        # main is untouched by either campaign: no PC was bound there
        assert await store.active_pcs(world.main_branch_id) == []

        other = (await client.post(_q("/worlds"), json={"name": "Other"})).json()["world_id"]
        foreign = await client.post(
            _q(f"/worlds/{other}/campaigns"),
            json={"participant": "alice", "new_pc_name": "X", "branch": fork.branch_id},
        )
        assert foreign.status_code == 404  # another world's branch is not this world's
        missing = await client.post(
            _q(f"/worlds/{world_id}/campaigns"),
            json={"participant": "alice", "new_pc_name": "X", "branch": "nope"},
        )
        assert missing.status_code == 404
        bad = await client.post(
            _q(f"/worlds/{world_id}/campaigns"),
            json={"participant": "alice", "new_pc_name": "X", "branch": ["main"]},
        )
        assert bad.status_code == 400


async def test_a_fresh_rest_pc_gets_the_same_starting_gear_as_the_cli(
    store: PostgresEventStore,
) -> None:
    # Parity: the CLI seats a fresh PC with STARTING_GEAR (Phase 3 — a lost fight loots a real
    # item); the server seated one empty-handed, so REST play had nothing to loot.
    from uro_core.timeline.models import STARTING_GEAR

    async with _client(store) as client:
        world_id = (await client.post(_q("/worlds"), json={"name": "Geared"})).json()["world_id"]
        made = await client.post(
            _q(f"/worlds/{world_id}/campaigns"), json={"participant": "alice", "new_pc_name": "Ash"}
        )
        c = made.json()["campaign_id"]
        joined = await client.post(
            _q(f"/campaigns/{c}/join"), json={"participant": "bob", "new_pc_name": "Bane"}
        )
        branch = made.json()["branch_id"]
        for pc in (await store.campaign_pc(c), joined.json()["actor_id"]):
            ids = await store.items_owned_by(branch, pc)
            owned = [(await store.get_item(branch, i) or {}).get("name") for i in ids]
            assert owned == list(STARTING_GEAR), (pc, owned)


async def test_branch_diff_endpoint(store: PostgresEventStore) -> None:
    # Parity: `diff_branches` (docs/18 B5) had no surface at all; `uro branch diff` + this endpoint.
    from uro_core.domain.events import actor_created

    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    app = create_app(
        engine_deps(store, engine, {_TOK: "alice", "tok-player": "bob"}, admin_tokens={_TOK})
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        world_id = (await client.post(_q("/worlds"), json={"name": "Diffed"})).json()["world_id"]
        world = await store.get_world(world_id)
        assert world is not None
        head = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
        fork = await store.fork_branch(world_id, head, "what-if")  # type: ignore[arg-type]
        await store.append_beat(fork.branch_id, [actor_created(actor_id="a:g", name="Ghost")])

        r = await client.get(_q(f"/worlds/{world_id}/diff?a=main&b=what-if&sections=actors"))
        assert r.status_code == 200, r.text
        added = r.json()["diff"]["actors"]["added"]
        assert [a["name"] for a in added] == ["Ghost"]
        full = await client.get(_q(f"/worlds/{world_id}/diff?a=main&b=what-if"))
        assert set(full.json()["diff"]) == {"actors"}  # every section checked; only differing ones

        player = await client.get(f"/worlds/{world_id}/diff?a=main&b=what-if&token=tok-player")
        assert player.status_code == 403  # omniscient rows: operator-only (D-45)
        assert (await client.get(_q(f"/worlds/{world_id}/diff?a=main"))).status_code == 400
        missing = await client.get(_q(f"/worlds/{world_id}/diff?a=main&b=nope"))
        assert missing.status_code == 404
        typo = await client.get(_q(f"/worlds/{world_id}/diff?a=main&b=what-if&sections=nope"))
        assert typo.status_code == 400
        assert (await client.get(_q("/worlds/nope/diff?a=main&b=main"))).status_code == 404


async def test_tokens_are_listed_and_revoked_by_participant(store: PostgresEventStore) -> None:
    # Parity/ops gap: revoking needed the raw token (only its hash is stored) and nothing listed
    # tokens, so an operator could not remove a player. Now: list, and revoke by participant.
    async with _client(store) as client:
        world_id = (await client.post(_q("/worlds"), json={"name": "Tokens"})).json()["world_id"]
        c = (
            await client.post(
                _q(f"/worlds/{world_id}/campaigns"),
                json={"participant": "alice", "new_pc_name": "Ash"},
            )
        ).json()["campaign_id"]
        joined = await client.post(
            _q(f"/campaigns/{c}/join"), json={"participant": "bob", "new_pc_name": "Bane"}
        )
        bob = joined.json()["token"]  # mint-on-join
        await client.post(_q(f"/campaigns/{c}/tokens"), json={"participant": "bob"})  # a 2nd
        await client.post(
            _q(f"/campaigns/{c}/join"), json={"participant": "carol", "new_pc_name": "Cass"}
        )

        listed = (await client.get(_q(f"/campaigns/{c}/tokens"))).json()["tokens"]
        assert sorted(t["participant_id"] for t in listed) == ["bob", "bob", "carol"]
        assert all(len(t["token_id"]) == 12 and t["token_id"] not in bob for t in listed)
        own = (await client.get(f"/campaigns/{c}/tokens?token={bob}")).json()["tokens"]
        assert [t["participant_id"] for t in own] == ["bob", "bob"]  # never carol's

        # a player can't revoke someone else; both/neither fields is a 400
        r = await client.post(
            f"/campaigns/{c}/tokens/revoke?token={bob}", json={"participant": "alice"}
        )
        assert r.status_code == 403
        both = await client.post(
            _q(f"/campaigns/{c}/tokens/revoke"), json={"participant": "bob", "token": bob}
        )
        assert both.status_code == 400
        assert (await client.post(_q(f"/campaigns/{c}/tokens/revoke"), json={})).status_code == 400

        r = await client.post(_q(f"/campaigns/{c}/tokens/revoke"), json={"participant": "bob"})
        assert r.json() == {"revoked": True, "count": 2, "closed_sockets": 0}
        assert (await client.get(f"/campaigns/{c}/roster?token={bob}")).status_code == 401
        after = (await client.get(_q(f"/campaigns/{c}/tokens"))).json()["tokens"]
        assert {t["participant_id"]: t["revoked"] for t in after} == {"bob": True, "carol": False}
        again = await client.post(_q(f"/campaigns/{c}/tokens/revoke"), json={"participant": "bob"})
        assert again.json() == {"revoked": False, "count": 0, "closed_sockets": 0}


async def test_a_credential_is_rotated_in_place_and_a_connection_relinked(
    store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Parity/ops gap: no layer could change a connection's key — rotating meant a new connection
    # plus rebinding every role. Now: rotate the key in place, or relink the connection.
    from cryptography.fernet import Fernet

    monkeypatch.setenv("URO_SECRET_KEY", Fernet.generate_key().decode())
    old = await store.add_credential(provider="openai", access_token="sk-old")
    spare = await store.add_credential(provider="openai", access_token="sk-spare")
    conn = await store.add_connection(name="rot", provider="openai", auth_id=old)
    try:
        async with _client(store) as client:
            r = await client.patch(
                _q(f"/providers/credentials/{old}"), json={"access_token": "sk-new"}
            )
            assert r.status_code == 200, r.text
            assert (await store.get_secret(old))[0] == "sk-new"  # type: ignore[index]
            assert (await store.get_connection(conn)).auth_id == old  # type: ignore[union-attr]
            gone = await client.patch(_q("/providers/credentials/nope"), json={"access_token": "x"})
            assert gone.status_code == 404
            blank = await client.patch(
                _q(f"/providers/credentials/{old}"), json={"access_token": ""}
            )
            assert blank.status_code == 400

            r = await client.patch(_q(f"/providers/{conn}"), json={"auth_id": spare})
            assert r.status_code == 200, r.text
            assert (await store.get_connection(conn)).auth_id == spare  # type: ignore[union-attr]
            r = await client.patch(_q(f"/providers/{conn}"), json={"auth_id": None})
            assert (await store.get_connection(conn)).auth_id is None  # type: ignore[union-attr]
            bad = await client.patch(_q(f"/providers/{conn}"), json={"auth_id": "nope"})
            assert bad.status_code == 400
            truthy = await client.patch(_q(f"/providers/{conn}"), json={"is_enabled": "false"})
            assert truthy.status_code == 400  # bool("false") is True — refuse, don't coerce
            missing = await client.patch(_q("/providers/nope"), json={"auth_id": spare})
            assert missing.status_code == 404
    finally:
        await store.delete_connection(conn)
        await store.delete_credential(old)
        await store.delete_credential(spare)


async def test_a_rest_world_can_choose_its_ruleset(store: PostgresEventStore) -> None:
    # Parity: a pack declares its ruleset, but POST /worlds couldn't, so a REST-created world could
    # only ever play the default.
    async with _client(store) as client:
        r = await client.post(_q("/worlds"), json={"name": "Pbta", "ruleset": "uro-pbta"})
        assert r.status_code == 200, r.text
        world_id = r.json()["world_id"]
        made = await client.post(
            _q(f"/worlds/{world_id}/campaigns"), json={"participant": "alice", "new_pc_name": "Ash"}
        )
        c = made.json()["campaign_id"]
        assert (await client.get(_q(f"/campaigns/{c}"))).json()["ruleset_id"] == "uro-pbta"
        sheets = (await client.get(_q(f"/campaigns/{c}/state?sections=sheets"))).json()
        assert "harm" in sheets["state"]["sheets"][0]["sheet"]  # a PbtA sheet, not a d20 one
        unknown = await client.post(_q("/worlds"), json={"name": "X", "ruleset": "srd51"})
        assert unknown.status_code == 400
        typed = await client.post(_q("/worlds"), json={"name": "X", "ruleset": ["uro-pbta"]})
        assert typed.status_code == 400


async def test_version_endpoint_is_authed(store: PostgresEventStore) -> None:
    async with _client(store) as client:
        r = await client.get(_q("/version"))
        assert r.status_code == 200 and set(r.json()) == {"uro-server", "uro-core"}
        assert (await client.get("/version")).status_code == 401
        assert (await client.get("/healthz")).json() == {"status": "ok"}  # advertises nothing


def _zip_pack(src: Path, *, rename: str | None = None) -> bytes:
    """A pack directory as an upload; `rename` swaps the manifest's name (a different world)."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                data = f.read_bytes()
                if rename and f.name == "world.toml":
                    data = data.replace(b'name = "Emberfell"', f'name = "{rename}"'.encode())
                zf.writestr(f.relative_to(src).as_posix(), data)
    return buf.getvalue()


async def test_a_pack_is_imported_and_seeded_over_the_api(store: PostgresEventStore) -> None:
    # Parity: `uro world create` / `uro world seed` had no endpoint — so over the API a world
    # couldn't come from a pack, carry its ruleset, or get a history.
    pack_dir = Path(__file__).resolve().parents[3] / "worlds" / "emberfell"
    name = f"Emberfell-{new_id()}"
    upload = {"pack": ("emberfell.zip", _zip_pack(pack_dir, rename=name), "application/zip")}
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    app = create_app(
        engine_deps(store, engine, {_TOK: "alice", "tok-player": "bob"}, admin_tokens={_TOK})
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(_q("/worlds/create"), files=upload)
        assert r.status_code == 200, r.text
        made = r.json()
        assert made["name"] == name and made["grade"] in ("runnable", "thin")
        pin = await store.world_ruleset(made["main_branch_id"])
        assert (pin.id, pin.version) == ("uro-pbta", ">=0")
        player = await client.post("/worlds/create?token=tok-player", files=upload)
        assert player.status_code == 403  # a structural write: operator-only (D-44)
        flag = await client.post(_q("/worlds/create?backfill=yes"), files=upload)
        assert flag.status_code == 400

        w = made["world_id"]
        seeded = await client.post(_q(f"/worlds/{w}/seed?seed=7"), files=upload)
        assert seeded.status_code == 200, seeded.text
        assert seeded.json()["dynasties"] > 0
        other = {"pack": ("e.zip", _zip_pack(pack_dir, rename=f"Other-{new_id()}"), "x/zip")}
        wrong = await client.post(_q(f"/worlds/{w}/seed"), files=other)
        assert wrong.status_code == 400  # another world's pack would seed its history here
        assert (await client.post(_q(f"/worlds/{w}/seed?seed=x"), files=upload)).status_code == 400
        assert (await client.post(_q("/worlds/nope/seed"), files=upload)).status_code == 404


async def test_the_pack_import_refuses_an_unknown_ruleset(store: PostgresEventStore) -> None:
    from uro_core.errors import PackError
    from uro_core.worldpack.create import import_pack
    from uro_core.worldpack.parse import parse_pack

    pack = parse_pack(Path(__file__).resolve().parents[3] / "worlds" / "emberfell")
    pack.manifest.ruleset.id = "srd51"
    with pytest.raises(PackError, match="unknown ruleset"):
        await import_pack(store, pack)


async def test_probe_and_backfill_follow_a_reloaded_router(store: PostgresEventStore) -> None:
    # They captured the startup router, so after POST /providers/reload they kept calling the old
    # connections while beats used the new ones.
    from uro_core.worldpack.parse import parse_pack

    class _Recording(_Stub):
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, req: CompletionRequest) -> str:
            self.calls += 1
            return "{}"

    old, new = _Recording(), _Recording()
    engine = Engine(store, ProviderRouter(bindings={}, default=old))
    deps = engine_deps(store, engine, {_TOK: "alice"}, router=engine.router)
    engine.rebind_router(ProviderRouter(bindings={}, default=new))  # what /providers/reload does
    manifest = parse_pack(Path(__file__).resolve().parents[3] / "worlds" / "ashfall").manifest
    assert deps.probe is not None
    await deps.probe(manifest, 1)
    assert new.calls > 0 and old.calls == 0


async def test_a_pbta_campaign_plays_on_a_uro_basic_server(store: PostgresEventStore) -> None:
    # End to end: `uro serve` binds uro-basic, a REST world pins uro-pbta, and a beat plays under
    # PbtA — the server no longer refuses it ("one ruleset per process").
    from uro_core.rulesets.uro_basic import UroBasic

    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=UroBasic())
    deps = engine_deps(store, engine, {_TOK: "alice"}, admin_tokens={_TOK})
    app = create_app(deps)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(_q("/worlds"), json={"name": "Pbta2", "ruleset": "uro-pbta"})
        made = await client.post(
            _q(f"/worlds/{r.json()['world_id']}/campaigns"),
            json={"participant": "alice", "new_pc_name": "Ash"},
        )
    c = made.json()["campaign_id"]
    chunks = [chunk async for chunk in deps.run_beat(c, "alice", "I look around the camp")]
    assert chunks  # narrated, not refused
    campaign = await store.get_campaign(c)
    assert campaign is not None
    beats = await store.recent_beats(campaign.branch_id, 5)
    assert [b.intent_text for b in beats] == ["I look around the camp"]
    pc = await store.campaign_pc(c)
    assert "harm" in (await store.get_sheet(campaign.branch_id, pc) or {})  # type: ignore[arg-type]


async def test_world_state_at_a_branch_head_or_any_commit(store: PostgresEventStore) -> None:
    # docs/08's `GET /worlds/{w}/state?branch=&at=` — deferred until a read-only materialize-at-
    # commit existed; it reuses the fork path, rolled back.
    from uro_core.domain.events import actor_created

    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    app = create_app(
        engine_deps(store, engine, {_TOK: "alice", "tok-player": "bob"}, admin_tokens={_TOK})
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        world_id = (await client.post(_q("/worlds"), json={"name": "Then"})).json()["world_id"]
        world = await store.get_world(world_id)
        assert world is not None
        first = await store.append_beat(
            world.main_branch_id, [actor_created(actor_id="a:1", name="One")]
        )
        await store.append_beat(world.main_branch_id, [actor_created(actor_id="a:2", name="Two")])

        then = await client.get(_q(f"/worlds/{world_id}/state?at={first.commit_id}"))
        assert then.status_code == 200, then.text
        assert [a["name"] for a in then.json()["state"]["actors"]] == ["One"]
        now = (await client.get(_q(f"/worlds/{world_id}/state"))).json()
        assert sorted(a["name"] for a in now["state"]["actors"]) == ["One", "Two"]
        assert now["branch"] == "main"

        player = await client.get(f"/worlds/{world_id}/state?at={first.commit_id}&token=tok-player")
        assert player.status_code == 403  # any commit, every section: operator-only (D-45)
        both = await client.get(_q(f"/worlds/{world_id}/state?at={first.commit_id}&branch=main"))
        assert both.status_code == 400
        assert (await client.get(_q(f"/worlds/{world_id}/state?at=nope"))).status_code == 404
        assert (await client.get(_q(f"/worlds/{world_id}/state?branch=nope"))).status_code == 404
        typo = await client.get(_q(f"/worlds/{world_id}/state?sections=nope"))
        assert typo.status_code == 400


async def test_a_campaign_cannot_adopt_another_players_live_pc(store: PostgresEventStore) -> None:
    # Review: start_campaign had no taken-PC guard (bind_pc did), so any player could start a
    # campaign adopting someone else's live PC — now on any branch. A binding a FORK copied in is
    # history there, and stays adoptable (the meteor "continue" leg).
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    app = create_app(
        engine_deps(store, engine, {_TOK: "alice", "tok-bob": "bob"}, admin_tokens={_TOK})
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        world_id = (await client.post(_q("/worlds"), json={"name": "Taken"})).json()["world_id"]
        made = await client.post(
            _q(f"/worlds/{world_id}/campaigns"), json={"participant": "alice", "new_pc_name": "Ash"}
        )
        ash = await store.campaign_pc(made.json()["campaign_id"])
        steal = await client.post(
            f"/worlds/{world_id}/campaigns?token=tok-bob",
            json={"participant": "bob", "adopt_actor_id": ash},
        )
        assert steal.status_code == 400 and "already an active PC" in steal.text
        world = await store.get_world(world_id)
        head = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
        await store.fork_branch(world_id, head, "continue")  # type: ignore[arg-type]
        on_fork = await client.post(
            f"/worlds/{world_id}/campaigns?token=tok-bob",
            json={"participant": "bob", "adopt_actor_id": ash, "branch": "continue"},
        )
        assert on_fork.status_code == 200, on_fork.text


async def test_review_fixes_on_the_management_surface(
    store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("URO_SECRET_KEY", Fernet.generate_key().decode())
    async with _client(store) as client:
        # commit detail takes a marker or a prefix, like `uro show` (it took only a full id)
        world_id = (await client.post(_q("/worlds"), json={"name": "Refs"})).json()["world_id"]
        world = await store.get_world(world_id)
        assert world is not None
        marker = await store.create_marker(world_id, "start", world.main_branch_id)
        r = await client.get(_q(f"/worlds/{world_id}/commits/start"))
        assert r.status_code == 200 and r.json()["commit_id"] == marker.commit_id
        assert (await client.get(_q(f"/worlds/{world_id}/commits/nope"))).status_code == 404

        # a token minted for campaign X can't mint for campaign Y (it re-seated a revoked player)
        made = [
            (
                await client.post(
                    _q(f"/worlds/{world_id}/campaigns"),
                    json={"participant": "bob", "new_pc_name": f"B{i}"},
                )
            ).json()["campaign_id"]
            for i in range(2)
        ]
        tok_x = (
            await client.post(_q(f"/campaigns/{made[0]}/tokens"), json={"participant": "bob"})
        ).json()["token"]
        cross = await client.post(
            f"/campaigns/{made[1]}/tokens?token={tok_x}", json={"participant": "bob"}
        )
        assert cross.status_code == 403

        # an OAuth credential can't be rotated with an access token alone: the old grant's
        # refresh token would silently restore the old login
        oauth = await store.add_credential(
            provider="codex", access_token="at", refresh_token="rt", auth_mode="oauth_device"
        )
        try:
            alone = await client.patch(
                _q(f"/providers/credentials/{oauth}"), json={"access_token": "at2"}
            )
            assert alone.status_code == 400
            both = await client.patch(
                _q(f"/providers/credentials/{oauth}"),
                json={"access_token": "at2", "refresh_token": "rt2"},
            )
            assert both.status_code == 200
            assert await store.get_secret(oauth) == ("at2", "rt2")
        finally:
            await store.delete_credential(oauth)


async def test_a_rest_worlds_settings_reach_the_engine_that_plays_it(
    store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D-62: POST /worlds takes `ruleset_config`; the server plays each campaign under its world's
    # settings — a different engine per (ruleset, settings), the process engine for the defaults.
    from uro_core.rulesets.uro_basic import UroBasic

    played: list[tuple[str, dict[str, Any], int]] = []

    async def fake_stream(self: Engine, campaign: Any, participant: str, text: str):  # type: ignore[no-untyped-def]
        cfg = self._ruleset.config.model_dump() if self._ruleset is not None else {}
        played.append((self.ruleset_id, cfg, id(self)))
        yield "ok"

    monkeypatch.setattr(Engine, "run_beat_stream", fake_stream)
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=UroBasic())
    deps = engine_deps(store, engine, {_TOK: "alice"}, admin_tokens={_TOK})
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        bad = await client.post(
            _q("/worlds"), json={"name": "X", "ruleset": "uro-pool", "ruleset_config": {"no": 1}}
        )
        assert bad.status_code == 400 and "uro-pool" in bad.text
        campaigns = []
        for name, rid, cfg in (
            ("Tuned", "uro-pool", {"push_strain": 2}),
            ("Plain", "uro-pool", {}),
            ("Basic", "", {}),
            ("TunedBasic", "uro-basic", {"max_level": 3}),  # the server's ruleset, other settings
        ):
            r = await client.post(
                _q("/worlds"), json={"name": name, "ruleset": rid, "ruleset_config": cfg}
            )
            assert r.status_code == 200, r.text
            made = await client.post(
                _q(f"/worlds/{r.json()['world_id']}/campaigns"),
                json={"participant": "alice", "new_pc_name": "Ash"},
            )
            campaigns.append(made.json()["campaign_id"])
    for c in campaigns:
        assert [chunk async for chunk in deps.run_beat(c, "alice", "go")] == ["ok"]
    (tuned, plain, basic, tuned_basic) = played
    assert (
        tuned[:2] == ("uro-pool", {**tuned[1], "push_strain": 2}) and plain[1]["push_strain"] == 1
    )
    assert tuned[2] != plain[2]  # different settings → a different engine
    assert basic[2] == id(engine)  # the process ruleset with default settings → its own engine
    assert tuned_basic[1]["max_level"] == 3 and tuned_basic[2] != id(engine)


async def test_a_server_time_skip_rests_under_the_campaigns_own_ruleset(
    store: PostgresEventStore,
) -> None:
    # D-63: the downtime hook runs under the CAMPAIGN's ruleset and settings — here a Uro Pool
    # campaign on a uro-basic server — both for a campaign time-skip and a fork with a time-skip.
    from uro_core.domain.events import sheet_updated
    from uro_core.rulesets.uro_basic import UroBasic

    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=UroBasic())
    app = create_app(engine_deps(store, engine, {_TOK: "alice"}, admin_tokens={_TOK}))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(_q("/worlds"), json={"name": "Weary", "ruleset": "uro-pool"})
        world_id = r.json()["world_id"]
        made = await client.post(
            _q(f"/worlds/{world_id}/campaigns"), json={"participant": "alice", "new_pc_name": "Ash"}
        )
        c, branch = made.json()["campaign_id"], made.json()["branch_id"]
        pc = await store.campaign_pc(c)
        sheet = await store.get_sheet(branch, pc)  # type: ignore[arg-type]
        strained = {**sheet, "strain": 3}  # type: ignore[dict-item]
        await store.append_beat(
            branch,
            [sheet_updated(actor_id=pc, sheet=strained, ruleset_id="uro-pool")],  # type: ignore[arg-type]
        )
        head = (await store.get_branch(branch)).head_commit  # type: ignore[union-attr]

        skipped = await client.post(_q(f"/campaigns/{c}/time-skip"), json={"days": 1})
        assert skipped.status_code == 200, skipped.text
        assert (await store.get_sheet(branch, pc))["strain"] == 0  # type: ignore[arg-type,index]

        fork = await client.post(
            _q(f"/worlds/{world_id}/branches"),
            json={"from_ref": head, "name": "later", "time_skip_days": 1},
        )
        assert fork.status_code == 200, fork.text
        later = await store.get_branch_by_name(world_id, "later")
        assert (await store.get_sheet(later.branch_id, pc))["strain"] == 0  # type: ignore[arg-type,index,union-attr]


async def test_a_time_skip_survives_a_world_whose_ruleset_is_gone(
    store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review: a world whose ruleset (a plugin) is no longer installed made every time-skip fail —
    # the fork endpoint 500'd AFTER committing the fork. Now the skip runs, without downtime rules.
    from uro_core.rulesets import registry
    from uro_core.rulesets.uro_pool import UroPool

    class _Gone(UroPool):
        id = "gone"

    registry.register("gone", _Gone)
    world = await store.create_world(f"Orphan-{new_id()}", ruleset_id="gone")
    registry._FACTORIES.pop("gone")  # the plugin is uninstalled
    async with _client(store) as client:
        head = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
        r = await client.post(
            _q(f"/worlds/{world.world_id}/branches"),
            json={"from_ref": head, "name": "later", "time_skip_days": 3},
        )
        assert r.status_code == 200, r.text
    later = await store.get_branch_by_name(world.world_id, "later")
    assert await store.current_world_time(later.branch_id) == 3  # type: ignore[union-attr]
