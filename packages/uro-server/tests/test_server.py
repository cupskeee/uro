"""Phase 5 inc 5.1: the transport shell — token auth + broadcast-shaped WS play channel (docs/08).

The acceptance's leg (a): two clients (two tokens) attached to one campaign both receive the
SAME streamed beats. Tested with fake deps so the transport is exercised without a live DB/model
(the engine path is tested in uro-core).
"""

import io
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uro_core.domain.events import BeatResolvedPayload
from uro_core.export import (
    BundleBranch,
    BundleCommit,
    BundleEvent,
    WorldBundle,
    stamp_chain,
    verify_bundle,
)
from uro_core.timeline.models import (
    Branch,
    BranchInfo,
    Campaign,
    LineageEntry,
    Marker,
    ParticipantNote,
    World,
)
from uro_server.app import ServerDeps, create_app
from uro_server.sessions import SessionHub

_TOKENS = {"tok-a": "player-1", "tok-b": "player-2"}


def _fake_deps() -> ServerDeps:
    async def campaign_exists(campaign_id: str) -> bool:
        return campaign_id == "camp-1"

    async def run_beat(campaign_id: str, participant: str, text: str) -> AsyncIterator[str]:
        for word in ("A ", "grim ", "tide ", "rises."):
            yield word

    return ServerDeps(
        resolve_participant=lambda token: _TOKENS.get(token),
        campaign_exists=campaign_exists,
        run_beat=run_beat,
    )


def _recv_until(ws: Any, target: str, cap: int = 25) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for _ in range(cap):
        m = ws.receive_json()
        msgs.append(m)
        if m.get("type") == target:
            return msgs
    raise AssertionError(f"never received {target!r}: {[m.get('type') for m in msgs]}")


# --- broadcast fan-out (the multiplayer seam) ---


async def test_session_hub_fans_out_to_all_subscribers() -> None:
    hub = SessionHub()
    a, b = hub.subscribe("c"), hub.subscribe("c")
    other = hub.subscribe("d")
    await hub.publish("c", {"type": "beat_committed"})
    assert a.get_nowait()["type"] == "beat_committed"
    assert b.get_nowait()["type"] == "beat_committed"  # both connections got the SAME message
    assert other.empty()  # a different campaign is isolated
    assert hub.connections("c") == 2
    hub.unsubscribe("c", a)
    hub.unsubscribe("c", b)
    assert hub.connections("c") == 0


# --- the acceptance leg: two clients, one campaign, same beats ---


def test_two_clients_receive_the_same_beat() -> None:
    client = TestClient(create_app(_fake_deps()))
    with (
        client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as a,
        client.websocket_connect("/campaigns/camp-1/play?token=tok-b") as b,
    ):
        a.send_json({"type": "intent", "text": "I scan the drowned pier"})
        msgs_a = _recv_until(a, "beat_committed")
        msgs_b = _recv_until(b, "beat_committed")

    def committed(msgs: list[dict[str, Any]]) -> dict[str, Any]:
        return next(m for m in msgs if m["type"] == "beat_committed")

    # BOTH clients saw the same committed beat — narration and the acting participant
    assert committed(msgs_a)["narration"] == "A grim tide rises."
    assert committed(msgs_b)["narration"] == "A grim tide rises."
    assert committed(msgs_b)["participant_id"] == "player-1"  # b sees a's beat (broadcast)
    # and both streamed the narration chunk-by-chunk
    assert [m for m in msgs_b if m["type"] == "narration_chunk"]


def test_healthz() -> None:
    assert TestClient(create_app(_fake_deps())).get("/healthz").json() == {"status": "ok"}


def test_bad_token_is_rejected_before_accept() -> None:
    client = TestClient(create_app(_fake_deps()))
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/campaigns/camp-1/play?token=nope"),
    ):
        pass
    assert excinfo.value.code == 4401  # unauthorized, closed before accept


def test_unknown_campaign_is_rejected() -> None:
    client = TestClient(create_app(_fake_deps()))
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/campaigns/nope/play?token=tok-a"),
    ):
        pass
    assert excinfo.value.code == 4404  # no such campaign


# --- Chronicler mode: the outcome-bundle endpoint (D-25) ---


def test_outcome_endpoint_distills_the_bundle() -> None:
    recorded: dict[str, Any] = {}

    async def report_outcome(campaign_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
        recorded["campaign"], recorded["bundle"] = campaign_id, bundle
        return {"committed_events": 4, "commit_id": "c:abc"}

    deps = _fake_deps()
    deps.report_outcome = report_outcome
    resp = TestClient(create_app(deps)).post(
        "/campaigns/camp-1/encounters/e:battle-7/outcome?token=tok-a",
        json={
            "feats": [{"actor": "a:hero", "description": "split the champion"}],
            "witnesses": ["a:raider1"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["committed_events"] == 4
    assert recorded["bundle"]["encounter_id"] == "e:battle-7"  # path param injected into the bundle


def test_outcome_endpoint_rejects_a_token_scoped_to_another_campaign() -> None:
    # D-41: the outcome endpoint mutates the timeline, so a campaign-scoped minted token (D-39) must
    # be enforced HERE too, not just on the WS play channel. A static token (campaign None) is
    # server-wide and passes.
    hit: dict[str, bool] = {}

    async def report_outcome(campaign_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
        hit["reached"] = True
        return {"committed_events": 0, "commit_id": "c", "receipt": []}

    deps = _fake_deps()
    deps.report_outcome = report_outcome
    deps.token_campaign = lambda t: "camp-1" if t == "tok-a" else None  # tok-a minted for camp-1
    client = TestClient(create_app(deps))
    blocked = client.post("/campaigns/camp-2/encounters/e/outcome?token=tok-a", json={})
    assert blocked.status_code == 403 and "reached" not in hit  # never reached the distiller
    own = client.post("/campaigns/camp-1/encounters/e/outcome?token=tok-a", json={})
    assert own.status_code == 200  # …but tok-a works on its OWN campaign
    static = client.post("/campaigns/camp-2/encounters/e/outcome?token=tok-b", json={})
    assert static.status_code == 200  # …and tok-b (unscoped/static) is server-wide


def test_outcome_endpoint_rejects_a_bad_token() -> None:
    resp = TestClient(create_app(_fake_deps())).post(
        "/campaigns/camp-1/encounters/e/outcome?token=nope", json={}
    )
    assert resp.status_code == 401  # an external resolver mutates state → must be authed


def test_outcome_endpoint_501_when_chronicler_disabled() -> None:
    resp = TestClient(create_app(_fake_deps())).post(  # authed, but report_outcome=None
        "/campaigns/camp-1/encounters/e/outcome?token=tok-a", json={}
    )
    assert resp.status_code == 501


# --- Phase 6 (D-30) cross-phase seam (P6 x P5): per-campaign ruleset pin vs one-Engine server ---


def test_beat_failure_is_broadcast_not_a_ws_crash() -> None:
    # A beat that raises (e.g. a ruleset mismatch, a provider error) must degrade to a graceful
    # 'beat_failed' broadcast, keeping the session alive — not crash the WS connection.
    async def boom(campaign_id: str, participant: str, text: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("kaboom")
        yield ""  # pragma: no cover — makes this an async generator

    async def campaign_exists(campaign_id: str) -> bool:
        return campaign_id == "camp-1"

    deps = ServerDeps(
        resolve_participant=lambda t: _TOKENS.get(t),
        campaign_exists=campaign_exists,
        run_beat=boom,
    )
    with TestClient(create_app(deps)).websocket_connect("/campaigns/camp-1/play?token=tok-a") as ws:
        _recv_until(ws, "participant_joined")
        ws.send_json({"type": "intent", "text": "I swing"})
        msgs = _recv_until(ws, "beat_failed")
        failed = msgs[-1]
        # Holistic review: the RAW exception ("kaboom") must NOT be fanned out to clients — a
        # generic reason is broadcast (info-disclosure fix); the detail is logged server-side.
        assert failed["error"] == "beat failed; nothing was saved" and failed["intent"] == "I swing"
        assert "kaboom" not in failed["error"]
        # the connection is still alive — another intent still gets a fresh beat_failed
        ws.send_json({"type": "intent", "text": "again"})
        assert _recv_until(ws, "beat_failed")[-1]["intent"] == "again"


async def test_engine_deps_rejects_a_ruleset_mismatch_with_a_clear_error() -> None:
    # engine_deps.run_beat must reject a campaign pinned to a DIFFERENT ruleset than the server's
    # single Engine holds — with a diagnostic naming both, not a raw ValidationError from deep in
    # sheet validation (P6 per-campaign pin x P5 one-ruleset server).
    from uro_core.pipeline.engine import Engine
    from uro_core.providers.adapters.stub import StubProvider
    from uro_core.providers.router import ProviderRouter
    from uro_core.rulesets.uro_basic import UroBasic
    from uro_core.timeline.models import Campaign
    from uro_server.app import engine_deps

    class _FakeStore:
        async def get_campaign(self, campaign_id: str) -> Campaign:
            return Campaign(
                campaign_id=campaign_id,
                world_id="w",
                branch_id="b",
                ruleset_id="uro-pbta",  # pinned to the alien ruleset
                ruleset_version=">=0",
            )

    store = _FakeStore()
    engine = Engine(store, ProviderRouter(bindings={}, default=StubProvider()), ruleset=UroBasic())
    deps = engine_deps(store, engine, _TOKENS)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="uro-pbta"):
        async for _ in deps.run_beat("camp-1", "player-1", "I seize the vein"):
            pass


# --- Phase 7 (OQ-7 → D-31): PartyArbiter round-robin over the WS play channel ---


def test_party_arbiter_round_robin_over_the_ws_channel() -> None:
    # Two connected participants share one campaign; free-roam turns rotate round-robin. The
    # holder's intent runs; an out-of-turn intent is told NOT_YOUR_TURN (not rejected); the token
    # rotates on beat_committed. Read all broadcasts off ONE socket (fan-out reaches both).
    from uro_core.session import PartyArbiter

    app = create_app(_fake_deps(), arbiter=PartyArbiter())
    client = TestClient(app)
    with (
        client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as a,  # player-1 joins first
        client.websocket_connect("/campaigns/camp-1/play?token=tok-b") as b,  # player-2 second
    ):
        _recv_until(a, "participant_joined")  # a sees its own join
        _recv_until(a, "participant_joined")  # ...and player-2's join → roster [player-1, player-2]

        # player-2 acts out of turn (player-1 holds) → NOT_YOUR_TURN, no beat
        b.send_json({"type": "intent", "text": "me first"})
        nyt = _recv_until(a, "not_your_turn")[-1]
        assert nyt["participant_id"] == "player-2"

        # player-1 (the holder) acts → a beat commits, and the token rotates to player-2
        a.send_json({"type": "intent", "text": "my turn"})
        assert _recv_until(a, "beat_committed")[-1]["participant_id"] == "player-1"

        # now player-2 holds → their intent runs
        b.send_json({"type": "intent", "text": "now me"})
        assert _recv_until(a, "beat_committed")[-1]["participant_id"] == "player-2"


# --- D-38 (#9): arbiter shapes beyond round-robin — non-canon lane + proposal-window + vote ---


def test_table_talk_fans_out_without_ever_committing_a_beat() -> None:
    # INC-1: the non-canon coordination lane broadcasts to the whole session and NEVER calls
    # run_beat — the SOLE path to append_beat. That run_beat is untouched IS the structural
    # non-canon guarantee (no branch-head move, no event, no commit can happen).
    calls: list[str] = []

    async def run_beat(campaign_id: str, participant: str, text: str):  # type: ignore[no-untyped-def]
        calls.append(text)
        yield "narr"

    async def campaign_exists(campaign_id: str) -> bool:
        return campaign_id == "camp-1"

    deps = ServerDeps(
        resolve_participant=lambda t: _TOKENS.get(t),
        campaign_exists=campaign_exists,
        run_beat=run_beat,
    )
    client = TestClient(create_app(deps))
    with (
        client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as a,
        client.websocket_connect("/campaigns/camp-1/play?token=tok-b") as b,
    ):
        _recv_until(a, "participant_joined")
        a.send_json({"type": "table_talk", "text": "we should split up"})
        tt = _recv_until(b, "table_talk")[-1]  # the OTHER client received it (broadcast fan-out)
        assert tt["participant_id"] == "player-1" and tt["text"] == "we should split up"
    assert calls == []  # run_beat NEVER ran → no beat, no append_beat, no commit (structural)


def test_proposal_window_surfaces_a_non_holder_intent_as_a_proposal() -> None:
    # INC-2 (G-10): a non-holder's intent becomes a first-class PROPOSAL (not a silent
    # not_your_turn, not a rejection, and NOT a beat); the holder enacts it as an ordinary beat.
    from uro_core.session import ProposalWindowArbiter

    client = TestClient(create_app(_fake_deps(), arbiter=ProposalWindowArbiter()))
    with (
        client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as a,  # player-1 holds
        client.websocket_connect("/campaigns/camp-1/play?token=tok-b") as b,  # player-2
    ):
        _recv_until(a, "participant_joined")
        _recv_until(a, "participant_joined")  # roster [player-1, player-2]
        b.send_json({"type": "intent", "text": "we should bribe the guard"})
        msgs = _recv_until(a, "proposal_opened")
        pr = msgs[-1]
        assert pr["participant_id"] == "player-2" and pr["text"] == "we should bribe the guard"
        assert not any(m["type"] == "beat_started" for m in msgs)  # a proposal is NOT a beat
        # the holder enacts → an ordinary beat commits, the token rotates
        a.send_json({"type": "intent", "text": "I bribe the guard"})
        assert _recv_until(a, "beat_committed")[-1]["participant_id"] == "player-1"
        # now player-2 holds → their intent runs as a real beat (proposal-window keeps round-robin)
        b.send_json({"type": "intent", "text": "now me"})
        assert _recv_until(a, "beat_committed")[-1]["participant_id"] == "player-2"


def test_vote_tallies_on_the_lane_and_decides_without_burning_a_beat() -> None:
    # INC-3 (G-11): votes ride the non-canon lane; the tally is server-side (session-only); a
    # decided vote is announced but NOT auto-enacted (take_pending deferred) — no beat is burned.
    from uro_core.session import VoteArbiter

    client = TestClient(create_app(_fake_deps(), arbiter=VoteArbiter()))
    with (
        client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as a,
        client.websocket_connect("/campaigns/camp-1/play?token=tok-b") as b,
    ):
        _recv_until(a, "participant_joined")
        _recv_until(a, "participant_joined")  # roster [player-1, player-2]
        a.send_json({"type": "vote", "choice": "go loud"})
        assert _recv_until(a, "vote_tally")[-1]["tally"] == {"go loud": 1}  # 1/2, no decision yet
        b.send_json({"type": "vote", "choice": "go loud"})
        msgs = _recv_until(a, "vote_decided")
        assert msgs[-1]["choice"] == "go loud"  # 2-0 → decided
        assert not any(m["type"] == "beat_started" for m in msgs)  # no vote burned a beat


def test_vote_on_a_non_vote_arbiter_gets_feedback_not_silence() -> None:
    # Review fix: the CLI advertises /vote unconditionally, but a non-VoteCoordinator arbiter (the
    # default party) can't tally — the server must say so, not silently swallow the frame.
    from uro_core.session import PartyArbiter

    client = TestClient(create_app(_fake_deps(), arbiter=PartyArbiter()))
    with client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as a:
        _recv_until(a, "participant_joined")
        a.send_json({"type": "vote", "choice": "go loud"})
        assert _recv_until(a, "vote_unsupported")[-1]["participant_id"] == "player-1"


def test_vote_resolves_when_the_last_holdout_disconnects() -> None:
    # Review liveness fix: two of three vote the same, the third leaves without voting → the round
    # is now complete; the server recomputes on disconnect (resolve_pending) and broadcasts the
    # decision, instead of the round silently never resolving.
    from uro_core.session import VoteArbiter

    _THREE = {"tok-a": "player-1", "tok-b": "player-2", "tok-c": "player-3"}

    async def campaign_exists(campaign_id: str) -> bool:
        return campaign_id == "camp-1"

    async def run_beat(campaign_id: str, participant: str, text: str):  # type: ignore[no-untyped-def]
        yield "narr"

    deps = ServerDeps(
        resolve_participant=lambda t: _THREE.get(t),
        campaign_exists=campaign_exists,
        run_beat=run_beat,
    )
    client = TestClient(create_app(deps, arbiter=VoteArbiter()))
    with (
        client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as a,
        client.websocket_connect("/campaigns/camp-1/play?token=tok-b") as b,
        client.websocket_connect("/campaigns/camp-1/play?token=tok-c") as c,
    ):
        for _ in range(3):
            _recv_until(a, "participant_joined")  # roster [player-1, player-2, player-3]
        a.send_json({"type": "vote", "choice": "flee"})
        _recv_until(a, "vote_tally")
        b.send_json({"type": "vote", "choice": "flee"})
        _recv_until(a, "vote_tally")  # 2/3 — undecided, waiting on player-3
        c.close()  # the last holdout leaves without voting → the round is now 2/2 complete
        assert _recv_until(a, "vote_decided")[-1]["choice"] == "flee"


# --- D-39 (#10): the runtime token registry (durable, hashed, revocable, off the branch axis) ---


class _FakeTokenStore:
    """In-memory SessionTokenStore for the registry unit test — mirrors the durable 018 table."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str, bool]] = {}  # hash → (participant, campaign, revoked)

    async def mint_token(self, token_hash: str, participant_id: str, campaign_id: str) -> None:
        self.rows[token_hash] = (participant_id, campaign_id, False)

    async def revoke_token(self, token_hash: str) -> bool:
        row = self.rows.get(token_hash)
        if row is not None and not row[2]:
            self.rows[token_hash] = (row[0], row[1], True)
            return True
        return False

    async def list_session_tokens(self) -> list[tuple[str, str, str]]:
        return [(h, p, c) for h, (p, c, revoked) in self.rows.items() if not revoked]


async def test_token_registry_mint_resolve_revoke_and_scope() -> None:
    from uro_server.sessions import TokenRegistry

    # "op" is the operator (admin) tier; "pleb" is a plain player token — NOT admin (D-39 review)
    reg = TokenRegistry(_FakeTokenStore(), {"op": "gm", "pleb": "carol"}, admin_tokens={"op"})
    assert reg.resolve("op") == "gm" and reg.is_admin("op")
    assert reg.resolve("pleb") == "carol" and not reg.is_admin("pleb")  # a plain peer is not admin
    tok = await reg.mint("bob", "camp-1")
    assert reg.resolve(tok) == "bob" and not reg.is_admin(tok)  # minted → resolves, NOT admin
    assert reg.campaign_of(tok) == "camp-1"  # minted tokens are campaign-scoped
    assert reg.campaign_of("op") is None  # a static/operator token is server-wide (unscoped)
    assert await reg.revoke(tok) is True
    assert reg.resolve(tok) is None  # revoked → denied


async def test_token_registry_stores_only_the_hash_and_survives_a_restart() -> None:
    import hashlib

    from uro_server.sessions import TokenRegistry

    store = _FakeTokenStore()
    tok = await TokenRegistry(store, {}).mint("bob", "camp-1")
    # only sha256(token) is persisted — never the plaintext
    assert list(store.rows.keys()) == [hashlib.sha256(tok.encode()).hexdigest()]
    # a FRESH registry (a server restart) with a cold cache hydrates from the store and resolves it
    reg2 = TokenRegistry(store, {})
    assert reg2.resolve(tok) is None  # cold before hydrate
    await reg2.hydrate()
    assert reg2.resolve(tok) == "bob"  # durable across a restart


def test_ws_rejects_a_token_scoped_to_another_campaign() -> None:
    # D-39 review: a MINTED token is campaign-scoped; using it on ANOTHER campaign's play channel is
    # rejected before accept (cross-campaign PC hijack). A static/operator token (token_campaign
    # None) is intentionally server-wide and unaffected.
    async def campaign_exists(campaign_id: str) -> bool:
        return campaign_id in ("camp-1", "camp-2")

    async def run_beat(campaign_id: str, participant: str, text: str):  # type: ignore[no-untyped-def]
        yield "x"

    deps = ServerDeps(
        resolve_participant=lambda t: "bob" if t == "bob-tok" else None,
        campaign_exists=campaign_exists,
        run_beat=run_beat,
        token_campaign=lambda t: "camp-1" if t == "bob-tok" else None,  # bob-tok minted for camp-1
    )
    client = TestClient(create_app(deps))
    with client.websocket_connect("/campaigns/camp-1/play?token=bob-tok") as ws:
        _recv_until(ws, "participant_joined")  # works on its OWN campaign
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/campaigns/camp-2/play?token=bob-tok"),
    ):
        pass
    assert exc.value.code == 4403  # rejected on a DIFFERENT campaign, before accept


# --- BE-1 (#33): GET /worlds/{w}/branches — the branch-list read (docs/18 B3, D-44) ---


class _FakeBranchStore:
    """Minimal EngineStore stand-in for the branch-list read — no live DB (mirrors the
    management-GET fake idiom). Only the four methods the endpoint calls."""

    def __init__(self, *, world_exists: bool = True) -> None:
        self._world_exists = world_exists

    async def get_world(self, world_id: str) -> World | None:
        if not self._world_exists:
            return None
        return World(world_id=world_id, name="Ashfall", main_branch_id="b:main")

    async def list_branches(self, world_id: str) -> list[BranchInfo]:
        return [
            BranchInfo(
                branch_id="b:main", world_id=world_id, name="main", head_commit="c:7", head_depth=7
            ),
            BranchInfo(
                branch_id="b:whatif",
                world_id=world_id,
                name="what-if",
                head_commit="c:9",
                forked_from="c:3",
                head_depth=5,
            ),
        ]

    async def current_world_time_batch(self, branch_ids: list[str]) -> dict[str, int]:
        return {"b:whatif": 365}  # b:main is ABSENT → the endpoint must default it to 0

    async def list_markers(self, world_id: str) -> list[Marker]:
        return [Marker(marker_id="m:1", world_id=world_id, name="pre-strike", commit_id="c:3")]


def test_list_world_branches_returns_branches_markers_and_the_in_fiction_day() -> None:
    deps = _fake_deps()
    deps.store = _FakeBranchStore()  # type: ignore[assignment]
    body = TestClient(create_app(deps)).get("/worlds/w:1/branches?token=tok-a").json()

    assert [b["name"] for b in body["branches"]] == ["main", "what-if"]
    main, whatif = body["branches"]
    # the in-fiction day is merged in; an absent branch defaults to 0
    assert main["world_day"] == 0 and main["forked_from"] is None and main["head_depth"] == 7
    assert whatif["world_day"] == 365 and whatif["forked_from"] == "c:3"
    assert [m["name"] for m in body["markers"]] == ["pre-strike"]


def test_list_world_branches_404_for_an_unknown_world() -> None:
    deps = _fake_deps()
    deps.store = _FakeBranchStore(world_exists=False)  # type: ignore[assignment]
    assert TestClient(create_app(deps)).get("/worlds/nope/branches?token=tok-a").status_code == 404


def test_list_world_branches_401_without_a_valid_token() -> None:
    # _auth fires before the store is touched → a bad token is 401 even for an existing world
    deps = _fake_deps()
    deps.store = _FakeBranchStore()  # type: ignore[assignment]
    assert TestClient(create_app(deps)).get("/worlds/w:1/branches?token=nope").status_code == 401


def test_list_world_branches_501_when_the_management_surface_is_disabled() -> None:
    # _fake_deps() leaves store=None → _mgmt() raises 501 (authed, but no store wired)
    resp = TestClient(create_app(_fake_deps())).get("/worlds/w:1/branches?token=tok-a")
    assert resp.status_code == 501


def test_a_failed_beat_still_rotates_the_party_token() -> None:
    # cross-phase review: a deterministically-failing turn-holder must NOT wedge the party — the
    # token still rotates on beat_failed, so the next participant can act.
    from uro_core.session import PartyArbiter

    async def always_fail(campaign_id: str, participant: str, text: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")
        yield ""  # pragma: no cover — makes this an async generator

    async def campaign_exists(campaign_id: str) -> bool:
        return campaign_id == "camp-1"

    deps = ServerDeps(
        resolve_participant=lambda t: _TOKENS.get(t),
        campaign_exists=campaign_exists,
        run_beat=always_fail,
    )
    app = create_app(deps, arbiter=PartyArbiter())
    client = TestClient(app)
    with (
        client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as a,  # player-1 (holder)
        client.websocket_connect("/campaigns/camp-1/play?token=tok-b") as b,  # player-2
    ):
        _recv_until(a, "participant_joined")
        _recv_until(a, "participant_joined")
        # player-1 (holder) submits → beat FAILS; the token must rotate to player-2 anyway
        a.send_json({"type": "intent", "text": "boom"})
        assert _recv_until(a, "beat_failed")[-1]["participant_id"] == "player-1"
        # player-2 now holds → their intent runs (also fails) — NOT a not_your_turn (rotation held)
        b.send_json({"type": "intent", "text": "boom"})
        assert _recv_until(a, "beat_failed")[-1]["participant_id"] == "player-2"


# --- BE-2 (#34) + BE-3 (#35): fork / marker-create (operator-tier, D-44) + log (read) ---


class _FakeTimelineStore:
    """EngineStore stand-in for the BE-2/BE-3 timeline endpoints — no live DB. Error knobs let a
    test drive fork_branch/create_marker's ValueError(dup)/KeyError(bad ref) paths into 400s."""

    def __init__(
        self,
        *,
        world_exists: bool = True,
        branch_exists: bool = True,
        fork_error: Exception | None = None,
        marker_error: Exception | None = None,
    ) -> None:
        self._world_exists = world_exists
        self._branch_exists = branch_exists
        self._fork_error = fork_error
        self._marker_error = marker_error
        self.forked: tuple[str, str, str] | None = None  # (world_id, from_ref, name)

    async def get_world(self, world_id: str) -> World | None:
        if not self._world_exists:
            return None
        return World(world_id=world_id, name="Ashfall", main_branch_id="b:main")

    async def get_branch_by_name(self, world_id: str, name: str) -> BranchInfo | None:
        if not self._branch_exists:
            return None
        return BranchInfo(
            branch_id="b:main", world_id=world_id, name=name, head_commit="c:7", head_depth=3
        )

    async def fork_branch(self, world_id: str, from_ref: str, name: str) -> Branch:
        if self._fork_error is not None:
            raise self._fork_error
        self.forked = (world_id, from_ref, name)
        return Branch(
            branch_id="b:fork", world_id=world_id, name=name, head_commit="c:3", forked_from="c:3"
        )

    async def create_marker(self, world_id: str, name: str, branch_id: str) -> Marker:
        if self._marker_error is not None:
            raise self._marker_error
        return Marker(marker_id="m:1", world_id=world_id, name=name, commit_id="c:7")

    async def lineage(self, branch_id: str, limit: int = 50) -> list[LineageEntry]:
        return [
            LineageEntry(
                commit_id="c:7",
                depth=3,
                event_types=["BeatResolved"],
                summary="I open the door",
                markers=["pre-strike"],
            ),
            LineageEntry(
                commit_id="c:0",
                depth=0,
                event_types=["WorldGenesis"],
                summary="genesis",
                markers=[],
            ),
        ]


def _operator_deps(store: object) -> ServerDeps:
    """A fake-deps with a wired store where tok-a is the OPERATOR (tok-b a plain player)."""
    deps = _fake_deps()
    deps.store = store  # type: ignore[assignment]
    deps.is_admin = lambda t: t == "tok-a"
    return deps


# BE-2: fork


def test_fork_branch_operator_can_fork_from_a_ref() -> None:
    store = _FakeTimelineStore()
    resp = TestClient(create_app(_operator_deps(store))).post(
        "/worlds/w:1/branches?token=tok-a", json={"from_ref": "pre-strike", "name": "what-if"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["branch_id"] == "b:fork" and body["forked_from"] == "c:3"
    assert store.forked == ("w:1", "pre-strike", "what-if")  # the ref passed straight through


def test_fork_branch_403_for_a_plain_player_token() -> None:
    # D-44: a fork is a structural write → operator-only. tok-b resolves but is NOT admin.
    resp = TestClient(create_app(_operator_deps(_FakeTimelineStore()))).post(
        "/worlds/w:1/branches?token=tok-b", json={"from_ref": "pre-strike", "name": "x"}
    )
    assert resp.status_code == 403


def test_fork_branch_401_without_a_valid_token() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeTimelineStore()))).post(
        "/worlds/w:1/branches?token=nope", json={"from_ref": "pre-strike", "name": "x"}
    )
    assert resp.status_code == 401


def test_fork_branch_404_for_an_unknown_world() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeTimelineStore(world_exists=False)))).post(
        "/worlds/nope/branches?token=tok-a", json={"from_ref": "pre-strike", "name": "x"}
    )
    assert resp.status_code == 404


def test_fork_branch_400_on_a_duplicate_name() -> None:
    # fork_branch re-raises the UNIQUE violation as a ValueError → 400 (not a raw 500)
    store = _FakeTimelineStore(fork_error=ValueError("branch 'main' already exists in this world"))
    resp = TestClient(create_app(_operator_deps(store))).post(
        "/worlds/w:1/branches?token=tok-a", json={"from_ref": "pre-strike", "name": "main"}
    )
    assert resp.status_code == 400


def test_fork_branch_400_on_an_unknown_ref() -> None:
    # resolve_ref raises KeyError for a bad marker/commit — must be caught into a 400, not a 500
    store = _FakeTimelineStore(fork_error=KeyError("no marker or commit 'ghost' in world"))
    resp = TestClient(create_app(_operator_deps(store))).post(
        "/worlds/w:1/branches?token=tok-a", json={"from_ref": "ghost", "name": "x"}
    )
    assert resp.status_code == 400


# BE-3: marker create


def test_create_marker_operator_names_a_branch_head() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeTimelineStore()))).post(
        "/worlds/w:1/markers?token=tok-a", json={"name": "pre-strike"}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "marker_id": "m:1",
        "world_id": "w:1",
        "name": "pre-strike",
        "commit_id": "c:7",
    }


def test_create_marker_403_for_a_plain_player_token() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeTimelineStore()))).post(
        "/worlds/w:1/markers?token=tok-b", json={"name": "x"}
    )
    assert resp.status_code == 403


def test_create_marker_404_for_an_unknown_branch() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeTimelineStore(branch_exists=False)))).post(
        "/worlds/w:1/markers?token=tok-a", json={"name": "x", "branch": "ghost"}
    )
    assert resp.status_code == 404


def test_create_marker_400_on_a_duplicate_name() -> None:
    store = _FakeTimelineStore(marker_error=ValueError("marker 'x' already exists in this world"))
    resp = TestClient(create_app(_operator_deps(store))).post(
        "/worlds/w:1/markers?token=tok-a", json={"name": "x"}
    )
    assert resp.status_code == 400


# BE-3: log (a plain read — D-44 keeps reads open, so a NON-operator token works)


def test_world_log_is_a_plain_read_open_to_any_token() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeTimelineStore()))).get(
        "/worlds/w:1/log?token=tok-b"  # tok-b is NOT an operator → reads stay open per D-44
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["head_depth"] == 3
    assert [e["commit_id"] for e in body["entries"]] == ["c:7", "c:0"]  # head→genesis
    assert body["entries"][0]["markers"] == ["pre-strike"]


def test_world_log_404_for_an_unknown_branch() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeTimelineStore(branch_exists=False)))).get(
        "/worlds/w:1/log?token=tok-a&branch=ghost"
    )
    assert resp.status_code == 404


# --- BE-4 (#36): raw event log + commit detail — OPERATOR-only observability (D-45) ---


class _FakeEventStore:
    """EngineStore stand-in for the BE-4 event-inspector reads (no live DB)."""

    def __init__(
        self, *, world_exists: bool = True, branch_exists: bool = True, commit_exists: bool = True
    ) -> None:
        self._world_exists = world_exists
        self._branch_exists = branch_exists
        self._commit_exists = commit_exists
        self.filters: dict[str, object] = {}

    async def get_world(self, world_id: str) -> World | None:
        if not self._world_exists:
            return None
        return World(world_id=world_id, name="Ashfall", main_branch_id="b:main")

    async def get_branch_by_name(self, world_id: str, name: str) -> BranchInfo | None:
        if not self._branch_exists:
            return None
        return BranchInfo(
            branch_id="b:main", world_id=world_id, name=name, head_commit="c:7", head_depth=3
        )

    async def branch_events(
        self,
        branch_id: str,
        *,
        event_type: str | None = None,
        entity_ref: str | None = None,
        caused_by: str | None = None,
        limit: int = 50,
    ) -> list[BundleEvent]:
        self.filters = {
            "event_type": event_type,
            "entity_ref": entity_ref,
            "caused_by": caused_by,
            "limit": limit,
        }
        return [
            BundleEvent(
                event_id="e:1",
                seq=0,
                event_type="ClaimRecorded",
                entity_refs=["a:hero"],
                caused_by={"kind": "player_action"},
                payload={"truth": True},
            )
        ]

    async def commit_detail(self, world_id: str, commit_id: str) -> BundleCommit | None:
        if not self._commit_exists:
            return None
        return BundleCommit(
            commit_id=commit_id,
            parent_id="c:0",
            depth=1,
            commit_hash="h:1",
            events=[
                BundleEvent(
                    event_id="e:1",
                    seq=0,
                    event_type="WorldGenesis",
                    caused_by={"kind": "system"},
                    payload={},
                )
            ],
        )


def test_world_events_operator_sees_the_raw_log_and_filters_pass_through() -> None:
    store = _FakeEventStore()
    resp = TestClient(create_app(_operator_deps(store))).get(
        "/worlds/w:1/events?token=tok-a&type=ClaimRecorded&entity_ref=a:hero"
        "&caused_by=player_action&limit=10"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"][0]["event_type"] == "ClaimRecorded"
    assert body["events"][0]["payload"] == {"truth": True}  # the raw (omniscient) payload
    assert store.filters == {
        "event_type": "ClaimRecorded",
        "entity_ref": "a:hero",
        "caused_by": "player_action",
        "limit": 10,
    }


def test_world_events_403_for_a_plain_player_token() -> None:
    # D-45: the raw event log carries omniscient truth → operator-only, never a player read
    resp = TestClient(create_app(_operator_deps(_FakeEventStore()))).get(
        "/worlds/w:1/events?token=tok-b"
    )
    assert resp.status_code == 403


def test_world_events_401_without_a_valid_token() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeEventStore()))).get(
        "/worlds/w:1/events?token=nope"
    )
    assert resp.status_code == 401


def test_world_events_404_for_unknown_world_and_branch() -> None:
    no_world = TestClient(create_app(_operator_deps(_FakeEventStore(world_exists=False))))
    assert no_world.get("/worlds/nope/events?token=tok-a").status_code == 404
    no_branch = TestClient(create_app(_operator_deps(_FakeEventStore(branch_exists=False))))
    assert no_branch.get("/worlds/w:1/events?token=tok-a&branch=ghost").status_code == 404


def test_commit_detail_operator_sees_a_commits_events() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeEventStore()))).get(
        "/worlds/w:1/commits/c:7?token=tok-a"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_id"] == "c:7" and body["commit_hash"] == "h:1"
    assert body["events"][0]["event_type"] == "WorldGenesis"


def test_commit_detail_403_for_a_plain_player_token() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeEventStore()))).get(
        "/worlds/w:1/commits/c:7?token=tok-b"
    )
    assert resp.status_code == 403


def test_commit_detail_404_for_an_unknown_commit() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeEventStore(commit_exists=False)))).get(
        "/worlds/w:1/commits/ghost?token=tok-a"
    )
    assert resp.status_code == 404


# --- BE-5 (#37): dry-run (intent-only, D-37) + consistency (T2 proxy) ---


def test_dry_run_returns_the_would_be_events_and_commits_nothing() -> None:
    seen: dict[str, str] = {}

    async def fake_preview(campaign_id: str, participant: str, intent: str) -> list[dict[str, Any]]:
        if campaign_id != "camp-1":
            raise HTTPException(status_code=404, detail="no such campaign")
        seen["participant"], seen["intent"] = participant, intent
        return [
            {
                "event_type": "ClaimRecorded",
                "entity_refs": ["a:hero"],
                "payload": {"statement": intent},
            }
        ]

    deps = _fake_deps()
    deps.preview_beat = fake_preview
    resp = TestClient(create_app(deps)).post(
        "/campaigns/camp-1/dry-run?token=tok-a", json={"intent": "I bribe the guard"}
    )
    assert resp.status_code == 200
    assert resp.json()["events"][0]["event_type"] == "ClaimRecorded"
    assert seen == {"participant": "player-1", "intent": "I bribe the guard"}  # token→participant


def test_dry_run_401_without_a_valid_token() -> None:
    # _auth fires before the preview/None check → a bad token is 401 regardless
    resp = TestClient(create_app(_fake_deps())).post(
        "/campaigns/camp-1/dry-run?token=nope", json={"intent": "x"}
    )
    assert resp.status_code == 401


def test_dry_run_501_when_preview_is_disabled() -> None:
    # _fake_deps() leaves preview_beat=None
    resp = TestClient(create_app(_fake_deps())).post(
        "/campaigns/camp-1/dry-run?token=tok-a", json={"intent": "x"}
    )
    assert resp.status_code == 501


def test_dry_run_400_on_an_empty_intent() -> None:
    async def fake_preview(campaign_id: str, participant: str, intent: str) -> list[dict[str, Any]]:
        return []

    deps = _fake_deps()
    deps.preview_beat = fake_preview
    resp = TestClient(create_app(deps)).post(
        "/campaigns/camp-1/dry-run?token=tok-a", json={"intent": "   "}
    )
    assert resp.status_code == 400


def test_dry_run_404_for_an_unknown_campaign() -> None:
    async def fake_preview(campaign_id: str, participant: str, intent: str) -> list[dict[str, Any]]:
        raise HTTPException(status_code=404, detail="no such campaign")

    deps = _fake_deps()
    deps.preview_beat = fake_preview
    resp = TestClient(create_app(deps)).post(
        "/campaigns/nope/dry-run?token=tok-a", json={"intent": "x"}
    )
    assert resp.status_code == 404


class _FakeConsistencyStore:
    def __init__(self, *, campaign_exists: bool = True) -> None:
        self._exists = campaign_exists

    async def get_campaign(self, campaign_id: str) -> Campaign | None:
        if not self._exists:
            return None
        return Campaign(campaign_id=campaign_id, world_id="w", branch_id="b:main")

    async def fact_consistency(self, branch_id: str) -> tuple[int, int]:
        return (3, 4)


def test_consistency_reports_the_survival_ratio() -> None:
    deps = _fake_deps()
    deps.store = _FakeConsistencyStore()  # type: ignore[assignment]
    body = TestClient(create_app(deps)).get("/campaigns/camp-1/consistency?token=tok-a").json()
    assert body == {"consistent": 3, "total": 4, "ratio": 0.75}


def test_consistency_404_for_an_unknown_campaign() -> None:
    deps = _fake_deps()
    deps.store = _FakeConsistencyStore(campaign_exists=False)  # type: ignore[assignment]
    resp = TestClient(create_app(deps)).get("/campaigns/nope/consistency?token=tok-a")
    assert resp.status_code == 404


def test_consistency_401_without_a_valid_token() -> None:
    deps = _fake_deps()
    deps.store = _FakeConsistencyStore()  # type: ignore[assignment]
    assert (
        TestClient(create_app(deps)).get("/campaigns/camp-1/consistency?token=nope").status_code
        == 401
    )


# --- BE-6 (#38): pack upload (multipart .zip) + validate — parse-only, any-authed ---

_REPO = Path(__file__).resolve().parents[3]  # tests → uro-server → packages → repo root


def _zip_dir(src: Path, *, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, arcname=prefix + f.relative_to(src).as_posix())
    return buf.getvalue()


def _post_pack(deps: ServerDeps, zip_bytes: bytes, *, token: str = "tok-a") -> Any:
    return TestClient(create_app(deps)).post(
        f"/worlds/validate?token={token}",
        files={"pack": ("pack.zip", zip_bytes, "application/zip")},
    )


def test_validate_pack_grades_an_uploaded_pack() -> None:
    resp = _post_pack(_fake_deps(), _zip_dir(_REPO / "worlds" / "ashfall"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"]  # the manifest name parsed from world.toml
    assert body["grade"] in ("runnable", "thin", "insufficient")
    assert body["counts"]["places"] > 0 and body["counts"]["actors"] > 0
    assert any(d["name"] == "geography" for d in body["dimensions"])
    assert body["ruleset_ok"] is True  # ashfall pins an installed ruleset


def test_validate_pack_handles_a_top_level_dir_in_the_zip() -> None:
    # a zip built WITH a top-level `ashfall/` dir still validates (single-subdir root detection)
    resp = _post_pack(_fake_deps(), _zip_dir(_REPO / "worlds" / "ashfall", prefix="ashfall/"))
    assert resp.status_code == 200


def test_validate_pack_400_on_a_non_zip_upload() -> None:
    assert _post_pack(_fake_deps(), b"this is not a zip").status_code == 400


def test_validate_pack_400_when_no_world_toml() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello")
    assert _post_pack(_fake_deps(), buf.getvalue()).status_code == 400


def test_validate_pack_400_on_zip_slip() -> None:
    # a malicious archive escaping the extraction dir is rejected before extractall
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "pwned")
    assert _post_pack(_fake_deps(), buf.getvalue()).status_code == 400


def test_validate_pack_401_without_a_valid_token() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("world.toml", "")
    assert _post_pack(_fake_deps(), buf.getvalue(), token="nope").status_code == 401


# --- BE-9 (#41): campaign end (operator, D-44) + codex/participant memory (self-or-admin, D-39) ---


class _FakeCodexStore:
    """EngineStore stand-in for BE-9 — end_campaign + participant memory (no live DB)."""

    def __init__(self, *, campaign_exists: bool = True, end_error: Exception | None = None) -> None:
        self._exists = campaign_exists
        self._end_error = end_error
        self.remembered: tuple[str, str, str, str | None, bool, list[str] | None] | None = None

    async def get_campaign(self, campaign_id: str) -> Campaign | None:
        return (
            Campaign(campaign_id=campaign_id, world_id="w:1", branch_id="b")
            if self._exists
            else None
        )

    async def end_campaign(
        self, campaign_id: str, marker_name: str, *, outcome: str = ""
    ) -> Marker:
        if self._end_error is not None:
            raise self._end_error
        return Marker(marker_id="m:end", world_id="w:1", name=marker_name, commit_id="c:9")

    async def participant_notes(self, participant_id: str, world_ref: str) -> list[ParticipantNote]:
        return [
            ParticipantNote(
                key="k1", text="the vault code is 4-7-1", pinned=True, entity_refs=["name:vault"]
            )
        ]

    async def participant_remember(
        self,
        participant_id: str,
        world_ref: str,
        text: str,
        *,
        key: str | None = None,
        pinned: bool = False,
        entity_refs: list[str] | None = None,
    ) -> str:
        self.remembered = (participant_id, world_ref, text, key, pinned, entity_refs)
        return key or "hash:abc"


def test_end_campaign_operator_marks_and_returns_the_marker() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeCodexStore()))).post(
        "/campaigns/camp-1/end?token=tok-a",
        json={"marker": "campaign-a-end", "outcome": "the heir fell"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "campaign-a-end"


def test_end_campaign_403_for_a_plain_player_token() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeCodexStore()))).post(
        "/campaigns/camp-1/end?token=tok-b", json={"marker": "x"}
    )
    assert resp.status_code == 403


def test_end_campaign_404_for_an_unknown_campaign() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeCodexStore(campaign_exists=False)))).post(
        "/campaigns/nope/end?token=tok-a", json={"marker": "x"}
    )
    assert resp.status_code == 404


def test_end_campaign_400_on_a_duplicate_marker() -> None:
    store = _FakeCodexStore(end_error=ValueError("marker 'x' already exists in this world"))
    resp = TestClient(create_app(_operator_deps(store))).post(
        "/campaigns/camp-1/end?token=tok-a", json={"marker": "x"}
    )
    assert resp.status_code == 400


def test_codex_add_and_list_own_notes() -> None:
    store = _FakeCodexStore()
    client = TestClient(create_app(_operator_deps(store)))
    # player-2 (tok-b) writes their OWN codex (world-scoped, fork-surviving)
    add = client.post(
        "/campaigns/camp-1/codex?token=tok-b",
        json={"text": "the vault code is 4-7-1", "pinned": True, "refs": ["name:vault"]},
    )
    assert add.status_code == 200
    assert store.remembered == (
        "player-2",
        "w:1",
        "the vault code is 4-7-1",
        None,
        True,
        ["name:vault"],
    )
    got = client.get("/campaigns/camp-1/codex?token=tok-b").json()  # …and lists it back
    assert got["participant"] == "player-2"
    assert (
        got["notes"][0]["text"] == "the vault code is 4-7-1" and got["notes"][0]["pinned"] is True
    )


def test_codex_403_reading_another_participants_notes_as_non_operator() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeCodexStore()))).get(
        "/campaigns/camp-1/codex?token=tok-b&participant=player-1"  # tok-b is player-2, not admin
    )
    assert resp.status_code == 403


def test_codex_operator_may_read_anothers_notes() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeCodexStore()))).get(
        "/campaigns/camp-1/codex?token=tok-a&participant=player-2"  # tok-a is the operator
    )
    assert resp.status_code == 200


def test_codex_401_without_a_valid_token() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeCodexStore()))).get(
        "/campaigns/camp-1/codex?token=nope"
    )
    assert resp.status_code == 401


# BE-8: world export / import


def _valid_bundle(name: str = "Ashfall") -> WorldBundle:
    """A minimal, hash-chain-STAMPED bundle (one genesis commit) — verify_bundle passes as-is."""
    bundle = WorldBundle(
        world_name=name,
        commits=[
            BundleCommit(
                commit_id="c:1",
                parent_id=None,
                depth=0,
                events=[
                    BundleEvent(
                        event_id="e:1",
                        seq=0,
                        event_type="WorldGenesis",
                        world_time={},
                        caused_by={"kind": "system"},
                        payload={"world_name": name},
                    )
                ],
            )
        ],
        branches=[BundleBranch(branch_id="b:main", name="main", head_commit="c:1")],
        markers=[],
    )
    stamp_chain(bundle)
    return bundle


class _FakeExportStore:
    """EngineStore stand-in for BE-8 export/import (no live DB). `import_world` runs the REAL
    verify_bundle, so the tamper test exercises the genuine ExportError path."""

    def __init__(self, *, world_exists: bool = True) -> None:
        self._world_exists = world_exists
        self.imported: WorldBundle | None = None

    async def get_world(self, world_id: str) -> World | None:
        if not self._world_exists:
            return None
        return World(world_id=world_id, name="Ashfall", main_branch_id="b:main")

    async def export_world(self, world_id: str) -> WorldBundle:
        return _valid_bundle()

    async def import_world(self, bundle: WorldBundle) -> World:
        verify_bundle(bundle)  # ExportError on a tampered bundle — BEFORE any write
        self.imported = bundle
        return World(world_id="w:new", name=bundle.world_name, main_branch_id="b:new")


def test_export_operator_gets_a_verifiable_bundle() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeExportStore()))).get(
        "/worlds/w:1/export?token=tok-a"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["world_name"] == "Ashfall" and body["manifest_hash"]  # stamped chain present
    verify_bundle(WorldBundle.model_validate(body))  # the wire bundle round-trips + verifies


def test_export_403_for_a_plain_player_token() -> None:
    # D-45: the whole event log is omniscient disclosure → operator-only, never player-facing.
    resp = TestClient(create_app(_operator_deps(_FakeExportStore()))).get(
        "/worlds/w:1/export?token=tok-b"
    )
    assert resp.status_code == 403


def test_export_401_without_a_valid_token() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeExportStore()))).get(
        "/worlds/w:1/export?token=nope"
    )
    assert resp.status_code == 401


def test_export_404_for_a_missing_world() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeExportStore(world_exists=False)))).get(
        "/worlds/nope/export?token=tok-a"
    )
    assert resp.status_code == 404


def test_import_operator_instantiates_a_fresh_world() -> None:
    store = _FakeExportStore()
    resp = TestClient(create_app(_operator_deps(store))).post(
        "/worlds/import?token=tok-a", json=_valid_bundle("Emberfell").model_dump(mode="json")
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"world_id": "w:new", "name": "Emberfell", "main_branch_id": "b:new"}
    assert store.imported is not None  # the verified bundle reached the store


def test_import_403_for_a_plain_player_token() -> None:
    # D-44: import is a structural write (instantiates a world) → operator-only.
    resp = TestClient(create_app(_operator_deps(_FakeExportStore()))).post(
        "/worlds/import?token=tok-b", json=_valid_bundle().model_dump(mode="json")
    )
    assert resp.status_code == 403


def test_import_400_for_a_tampered_bundle_nothing_written() -> None:
    store = _FakeExportStore()
    bundle = _valid_bundle().model_dump(mode="json")
    bundle["world_name"] = "Tampered"  # break the manifest digest after stamping
    resp = TestClient(create_app(_operator_deps(store))).post(
        "/worlds/import?token=tok-a", json=bundle
    )
    assert resp.status_code == 400
    assert "verification failed" in resp.json()["detail"]
    assert store.imported is None  # rejected BEFORE any write


def test_import_400_for_a_malformed_bundle() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeExportStore()))).post(
        "/worlds/import?token=tok-a", json={"not": "a bundle", "commits": "nope"}
    )
    assert resp.status_code == 400
    assert "malformed bundle" in resp.json()["detail"]


# BE-10: usage telemetry + world-scoped chronicle + ruleset registry


class _FakeUsageStore:
    """EngineStore stand-in for BE-10 usage + world-chronicle (no live DB)."""

    def __init__(self, *, world_exists: bool = True, branch_exists: bool = True) -> None:
        self._world_exists = world_exists
        self._branch_exists = branch_exists
        self.stage_arg: str | None = "<<unset>>"

    async def get_world(self, world_id: str) -> World | None:
        if not self._world_exists:
            return None
        return World(world_id=world_id, name="Ashfall", main_branch_id="b:main")

    async def get_branch_by_name(self, world_id: str, name: str) -> BranchInfo | None:
        if not self._branch_exists:
            return None
        return BranchInfo(
            branch_id="b:what-if", world_id=world_id, name=name, head_commit="c:9", head_depth=4
        )

    async def recent_beats(self, branch_id: str, limit: int) -> list[BeatResolvedPayload]:
        return [
            BeatResolvedPayload(
                beat_id="beat:1",
                participant_id="player-1",
                intent_text="strike the warlord",
                narration="Steel meets steel under a bleeding sky.",
            )
        ]

    async def usage_by_stage(self, stage: str | None = None) -> list[dict[str, Any]]:
        self.stage_arg = stage
        return [
            {
                "stage_tag": "narrator",
                "model": None,
                "calls": 3,
                "tokens_in": 0,
                "tokens_out": 0,
                "avg_latency_ms": 12,
            },
            {
                "stage_tag": "planner",
                "model": "gpt-x",
                "calls": 2,
                "tokens_in": 40,
                "tokens_out": 10,
                "avg_latency_ms": 30,
            },
        ]


def test_usage_operator_gets_aggregated_stage_telemetry() -> None:
    store = _FakeUsageStore()
    resp = TestClient(create_app(_operator_deps(store))).get("/usage?token=tok-a")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_calls"] == 5 and len(body["by_stage"]) == 2  # 3 + 2
    assert store.stage_arg is None  # no ?stage= → aggregate over all


def test_usage_stage_filter_passes_through() -> None:
    store = _FakeUsageStore()
    resp = TestClient(create_app(_operator_deps(store))).get("/usage?token=tok-a&stage=planner")
    assert resp.status_code == 200 and store.stage_arg == "planner"


def test_usage_403_for_a_plain_player_token() -> None:
    # D-44: cost/model telemetry → operator-only.
    resp = TestClient(create_app(_operator_deps(_FakeUsageStore()))).get("/usage?token=tok-b")
    assert resp.status_code == 403


def test_usage_401_without_a_valid_token() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeUsageStore()))).get("/usage?token=nope")
    assert resp.status_code == 401


def test_usage_400_for_unsupported_world_or_campaign_filter() -> None:
    # Honest: llm_calls isn't keyed by world/campaign — reject, never silently ignore.
    client = TestClient(create_app(_operator_deps(_FakeUsageStore())))
    for q in ("world=w:1", "campaign=camp-1"):
        resp = client.get(f"/usage?token=tok-a&{q}")
        assert resp.status_code == 400
        assert "not supported yet" in resp.json()["detail"]


def test_world_chronicle_operator_reads_a_named_branch() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeUsageStore()))).get(
        "/worlds/w:1/chronicle?token=tok-a&branch=what-if"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["branch"] == "what-if" and body["beats"][0]["beat_id"] == "beat:1"


def test_world_chronicle_403_for_a_plain_player_token() -> None:
    # Cross-branch narration (incl. sibling forks) → operator timeline-inspection surface.
    resp = TestClient(create_app(_operator_deps(_FakeUsageStore()))).get(
        "/worlds/w:1/chronicle?token=tok-b"
    )
    assert resp.status_code == 403


def test_world_chronicle_404_for_a_missing_world() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeUsageStore(world_exists=False)))).get(
        "/worlds/nope/chronicle?token=tok-a"
    )
    assert resp.status_code == 404


def test_world_chronicle_404_for_a_missing_branch() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeUsageStore(branch_exists=False)))).get(
        "/worlds/w:1/chronicle?token=tok-a&branch=ghost"
    )
    assert resp.status_code == 404


def test_world_chronicle_400_for_a_non_integer_limit() -> None:
    resp = TestClient(create_app(_operator_deps(_FakeUsageStore()))).get(
        "/worlds/w:1/chronicle?token=tok-a&limit=abc"
    )
    assert resp.status_code == 400


def _rulesets_deps(rulesets: object) -> ServerDeps:
    deps = _fake_deps()
    deps.is_admin = lambda t: t == "tok-a"
    deps.list_rulesets = rulesets  # type: ignore[assignment]
    return deps


def test_rulesets_any_authed_lists_the_registry() -> None:
    canned = [{"id": "uro-basic", "version": "0", "sheet_schema": {"hp": "int"}}]
    resp = TestClient(create_app(_rulesets_deps(lambda: canned))).get("/rulesets?token=tok-b")
    assert resp.status_code == 200  # a plain player token is fine — public capability info
    assert resp.json()["rulesets"] == canned


def test_rulesets_reflects_the_real_registry() -> None:
    # Wire the REAL composition closure: the two built-ins, each with id@version + sheet shape.
    from uro_core.rulesets import registry as ruleset_registry

    def real() -> list[dict[str, Any]]:
        return [
            {
                "id": ruleset_registry.resolve(rid).id,
                "version": ruleset_registry.resolve(rid).version,
                "sheet_schema": ruleset_registry.resolve(rid).sheet_schema(),
            }
            for rid in ruleset_registry.available()
        ]

    resp = TestClient(create_app(_rulesets_deps(real))).get("/rulesets?token=tok-a")
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()["rulesets"]}
    assert {"uro-basic", "uro-pbta"} <= ids


def test_rulesets_501_when_not_wired() -> None:
    resp = TestClient(create_app(_rulesets_deps(None))).get("/rulesets?token=tok-a")
    assert resp.status_code == 501


def test_rulesets_401_without_a_valid_token() -> None:
    resp = TestClient(create_app(_rulesets_deps(lambda: []))).get("/rulesets?token=nope")
    assert resp.status_code == 401


# BE-7 (#39): AI world-authoring stages (backfill + probe) over HTTP — operator-only, stub-tested


async def _fake_backfill(pack: Any) -> Any:
    """A canned backfill closure (no live model): append one ai_backfill conflict seed."""
    from uro_core.worldpack.models import ThreadSeed

    seed = ThreadSeed(
        id="t:ai",
        stakes="a rival house covets the throne",
        state="offered",
        provenance="ai_backfill",
    )
    added = [f"conflict seed (ai_backfill): {seed.stakes}"]
    return pack.model_copy(update={"threads": [*pack.threads, seed]}), added


async def _fake_probe(manifest: Any, tries: int) -> Any:
    """A canned probe report (no live model). Echoes `tries` into the detail for passthrough."""
    from uro_core.engines.probe import ProbeReport, ProbeResult

    return ProbeReport(
        world=manifest.name,
        results=[
            ProbeResult(name="structured_output", status="pass", detail=f"{tries}/{tries} valid"),
            ProbeResult(name="content_rating", status="warn", detail="model softened a category"),
        ],
    )


def _authoring_deps() -> ServerDeps:
    """Operator (tok-a) deps with backfill+probe wired to canned closures (no live LLM)."""
    deps = _fake_deps()
    deps.is_admin = lambda t: t == "tok-a"
    deps.backfill = _fake_backfill  # type: ignore[assignment]
    deps.probe = _fake_probe  # type: ignore[assignment]
    return deps


def _post_authoring(deps: ServerDeps, path: str, *, token: str = "tok-a") -> Any:
    return TestClient(create_app(deps)).post(
        f"{path}?token={token}",
        files={"pack": ("pack.zip", _zip_dir(_REPO / "worlds" / "thornwood"), "application/zip")},
    )


# --- backfill ---


def test_backfill_operator_previews_ai_seeds() -> None:
    resp = _post_authoring(_authoring_deps(), "/worlds/backfill")
    assert resp.status_code == 200
    body = resp.json()
    assert body["added"] and body["before_grade"] and body["after_grade"]
    # only the ai_backfill seed is surfaced (preview — nothing committed)
    assert [s["provenance"] for s in body["seeds"]] == ["ai_backfill"]


def test_backfill_403_for_a_plain_player_token() -> None:
    # D-44: a live, uncapped LLM call → operator-only (cost).
    assert _post_authoring(_authoring_deps(), "/worlds/backfill", token="tok-b").status_code == 403


def test_backfill_401_without_a_valid_token() -> None:
    assert _post_authoring(_authoring_deps(), "/worlds/backfill", token="nope").status_code == 401


def test_backfill_501_when_no_provider_wired() -> None:
    deps = _fake_deps()
    deps.is_admin = lambda t: t == "tok-a"  # operator, but deps.backfill stays None
    assert _post_authoring(deps, "/worlds/backfill").status_code == 501


def test_backfill_502_on_a_provider_error() -> None:
    from uro_core.errors import ProviderError

    async def _boom(pack: Any) -> Any:
        raise ProviderError("model unreachable")

    deps = _authoring_deps()
    deps.backfill = _boom  # type: ignore[assignment]
    resp = _post_authoring(deps, "/worlds/backfill")
    assert resp.status_code == 502 and "provider error" in resp.json()["detail"]


def test_backfill_400_on_a_non_zip_upload() -> None:
    resp = TestClient(create_app(_authoring_deps())).post(
        "/worlds/backfill?token=tok-a",
        files={"pack": ("pack.zip", b"not a zip", "application/zip")},
    )
    assert resp.status_code == 400


# --- probe ---


def test_probe_operator_gets_a_warn_not_fail_report() -> None:
    resp = TestClient(create_app(_authoring_deps())).post(
        "/worlds/probe?token=tok-a&tries=5",
        files={"pack": ("pack.zip", _zip_dir(_REPO / "worlds" / "thornwood"), "application/zip")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True  # a 'warn' is not a 'fail' → report.ok stays True
    assert any("softened" in w for w in body["warnings"])
    assert body["results"][0]["detail"] == "5/5 valid"  # tries passed through


def test_probe_403_for_a_plain_player_token() -> None:
    assert _post_authoring(_authoring_deps(), "/worlds/probe", token="tok-b").status_code == 403


def test_probe_501_when_no_provider_wired() -> None:
    deps = _fake_deps()
    deps.is_admin = lambda t: t == "tok-a"
    assert _post_authoring(deps, "/worlds/probe").status_code == 501


def test_probe_400_on_a_bad_or_out_of_range_tries() -> None:
    client = TestClient(create_app(_authoring_deps()))
    z = {"pack": ("pack.zip", _zip_dir(_REPO / "worlds" / "thornwood"), "application/zip")}
    assert client.post("/worlds/probe?token=tok-a&tries=abc", files=z).status_code == 400
    assert client.post("/worlds/probe?token=tok-a&tries=99", files=z).status_code == 400


def test_probe_reports_a_failing_model_as_200_not_an_error() -> None:
    # warn-not-fail (D-24): even an all-fail report is a 200 with ok=False, never a 4xx/5xx.
    async def _all_fail(manifest: Any, tries: int) -> Any:
        from uro_core.engines.probe import ProbeReport, ProbeResult

        return ProbeReport(
            world=manifest.name,
            results=[ProbeResult(name="structured_output", status="fail", detail="0/3 valid")],
        )

    deps = _authoring_deps()
    deps.probe = _all_fail  # type: ignore[assignment]
    resp = _post_authoring(deps, "/worlds/probe")
    assert resp.status_code == 200 and resp.json()["ok"] is False


def test_probe_502_on_a_provider_error() -> None:
    from uro_core.errors import ProviderError

    async def _boom(manifest: Any, tries: int) -> Any:
        raise ProviderError("judge model unreachable")

    deps = _authoring_deps()
    deps.probe = _boom  # type: ignore[assignment]
    assert _post_authoring(deps, "/worlds/probe").status_code == 502


def test_pack_upload_rejects_oversized_body_before_parsing(monkeypatch: Any) -> None:
    # BE-7 hardening: an over-cap multipart upload to a pack route is 413'd by Content-Length
    # BEFORE the body is spooled — so a large body can't be buffered pre-auth. Even a NO-token
    # request is rejected (the guard runs ahead of auth). Small cap keeps the test fast.
    monkeypatch.setattr("uro_server.app._MAX_PACK_BYTES", 100)
    big = {"pack": ("big.zip", b"0" * 500, "application/zip")}
    client = TestClient(create_app(_authoring_deps()))
    for path in ("/worlds/backfill", "/worlds/probe", "/worlds/validate"):
        assert (
            client.post(f"{path}?token=nope", files=big).status_code == 413
        )  # no token, still 413
    # /worlds/import (a JSON bundle) is NOT capped by this guard — a large world must import.
    assert (
        "/worlds/import"
        not in __import__("uro_server.app", fromlist=["_PACK_UPLOAD_PATHS"])._PACK_UPLOAD_PATHS
    )


# BE-11 (#43): the WS wire contract — docs/08 now agrees frame-for-frame with app.py.
# These assert the EXACT shape (key set) of each real frame + that undocumented client frames
# are ignored. (vote_*/proposal_opened/not_your_turn/table_talk/beat_failed are shape-checked in
# the arbiter tests above.)


def test_ws_intent_beat_frame_shapes() -> None:
    client = TestClient(create_app(_fake_deps()))
    with client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as ws:
        assert set(_recv_until(ws, "participant_joined")[-1]) == {"type", "participant_id"}
        ws.send_json({"type": "intent", "text": "I scan the pier"})
        msgs = _recv_until(ws, "beat_committed")
    started = next(m for m in msgs if m["type"] == "beat_started")
    chunk = next(m for m in msgs if m["type"] == "narration_chunk")
    committed = next(m for m in msgs if m["type"] == "beat_committed")
    assert set(started) == {"type", "participant_id", "intent"}
    assert set(chunk) == {"type", "participant_id", "text"}
    assert set(committed) == {"type", "participant_id", "intent", "narration"}
    # No universal envelope: real frames carry NO campaign_id / beat_id (docs/08 BE-11 retraction).
    assert all("campaign_id" not in m and "beat_id" not in m for m in msgs)


def test_ws_unknown_client_frames_are_ignored() -> None:
    # encounter_action / pin_actor are documented as future GROWs, NOT handled today — sending them
    # must be a silent no-op (no beat, no echo), and the loop must keep serving the next intent.
    client = TestClient(create_app(_fake_deps()))
    with client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as ws:
        _recv_until(ws, "participant_joined")
        ws.send_json({"type": "encounter_action", "action": "swing"})
        ws.send_json({"type": "pin_actor", "actor_id": "a:hero"})
        ws.send_json(
            {"type": "intent", "text": "I press on"}
        )  # still served after the ignored ones
        msgs = _recv_until(ws, "beat_committed")
    assert not any(m["type"] in ("encounter_action", "pin_actor") for m in msgs)  # never echoed


def test_ws_intent_rejected_frame_shape() -> None:
    from uro_core.session import AdmitDecision, SoloArbiter

    class _RejectArbiter(SoloArbiter):
        async def admit(self, campaign_id: str, participant_id: str, intent: str) -> AdmitDecision:
            return AdmitDecision.REJECTED

    client = TestClient(create_app(_fake_deps(), arbiter=_RejectArbiter()))
    with client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as ws:
        _recv_until(ws, "participant_joined")
        ws.send_json({"type": "intent", "text": "I do the forbidden thing"})
        rejected = _recv_until(ws, "intent_rejected")[-1]
    assert set(rejected) == {"type", "participant_id", "text"}
    assert rejected["participant_id"] == "player-1" and rejected["text"]


def test_ws_outcome_recorded_broadcast_shape() -> None:
    # The Chronicler outcome endpoint broadcasts an `outcome_recorded` frame to the play channel —
    # a real server→client frame docs/08 previously omitted (BE-11). Assert it reaches a listener.
    async def report_outcome(campaign_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
        return {"committed_events": 2, "commit_id": "c:xyz"}

    deps = _fake_deps()
    deps.report_outcome = report_outcome
    client = TestClient(create_app(deps))
    with client.websocket_connect("/campaigns/camp-1/play?token=tok-a") as ws:
        _recv_until(ws, "participant_joined")
        resp = client.post(
            "/campaigns/camp-1/encounters/e:battle-9/outcome?token=tok-a", json={"feats": []}
        )
        assert resp.status_code == 200
        frame = _recv_until(ws, "outcome_recorded")[-1]
    assert frame["encounter_id"] == "e:battle-9"
    assert frame["committed_events"] == 2 and frame["commit_id"] == "c:xyz"


# Holistic-review fixes: transport-level (zip-bomb decompressed cap, dry-run campaign-scope)


def test_pack_upload_rejects_a_zip_bomb_by_decompressed_size(monkeypatch: Any) -> None:
    # A tiny archive whose DECOMPRESSED bytes exceed the cap is 413'd mid-extraction — the
    # compressed-byte cap + Content-Length middleware don't bound this (holistic review).
    monkeypatch.setattr("uro_server.app._MAX_UNCOMPRESSED_BYTES", 100)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("world.toml", b"x" * 5000)  # 5 KB decompressed > 100-byte cap
    resp = TestClient(create_app(_authoring_deps())).post(
        "/worlds/validate?token=tok-a",
        files={"pack": ("pack.zip", buf.getvalue(), "application/zip")},
    )
    assert resp.status_code == 413


def test_dry_run_rejects_a_foreign_scoped_token() -> None:
    # A minted token scoped to camp-1 can't dry-run camp-2 (LLM cost + state read) — review fix.
    async def _preview(campaign_id: str, participant: str, intent: str) -> list[dict[str, Any]]:
        return [{"event_type": "BeatResolved"}]

    deps = _fake_deps()
    deps.preview_beat = _preview
    deps.token_campaign = lambda t: "camp-1" if t == "tok-a" else None  # tok-a minted for camp-1
    client = TestClient(create_app(deps))
    blocked = client.post("/campaigns/camp-2/dry-run?token=tok-a", json={"intent": "x"})
    assert blocked.status_code == 403
    own = client.post("/campaigns/camp-1/dry-run?token=tok-a", json={"intent": "x"})
    assert own.status_code == 200  # …but tok-a works on its OWN campaign


def test_cors_disabled_by_default() -> None:
    # No --cors-origin → no CORS header, so a browser SPA on another origin is blocked (the symptom
    # that surfaced this: uro-loom on :5173 → uro-server on :8000 showed all-red network calls).
    client = TestClient(create_app(_fake_deps()))
    resp = client.get("/healthz", headers={"Origin": "http://localhost:5173"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_allows_a_configured_origin() -> None:
    # An allowed origin gets echoed back (simple request) and cleared by preflight (OPTIONS).
    origin = "http://localhost:5173"
    client = TestClient(create_app(_fake_deps(), cors_origins=[origin]))
    simple = client.get("/healthz", headers={"Origin": origin})
    assert simple.headers.get("access-control-allow-origin") == origin

    preflight = client.options(
        "/campaigns/camp-1/dry-run",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers.get("access-control-allow-origin") == origin
    # Bearer tokens ride the Authorization header — the preflight must green-light it.
    assert "authorization" in preflight.headers.get("access-control-allow-headers", "").lower()


def test_cors_wildcard_drops_credentials() -> None:
    # '*' is dev-only allow-any; per the CORS spec a wildcard origin cannot carry credentials, so
    # the middleware must NOT also assert allow-credentials (browsers reject that combination).
    client = TestClient(create_app(_fake_deps(), cors_origins=["*"]))
    resp = client.get("/healthz", headers={"Origin": "http://anything.example"})
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in resp.headers
