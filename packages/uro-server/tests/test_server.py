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
