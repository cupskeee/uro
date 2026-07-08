"""Phase 5 inc 5.1: the transport shell — token auth + broadcast-shaped WS play channel (docs/08).

The acceptance's leg (a): two clients (two tokens) attached to one campaign both receive the
SAME streamed beats. Tested with fake deps so the transport is exercised without a live DB/model
(the engine path is tested in uro-core).
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
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
        assert failed["error"] == "kaboom" and failed["intent"] == "I swing"
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
