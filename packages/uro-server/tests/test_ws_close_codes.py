"""D-107: a refused play socket's close code reaches a real client over a real server.

The starlette TestClient hands a test the close code even when the server closed the socket before
accepting it, so the unit tests passed while every real client (a browser, `uro connect`) got a
bare HTTP 403 handshake rejection instead (a browser reports 1006). This runs uvicorn and the
`websockets` client, as a deployment does.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import AsyncIterator, Iterator

import pytest
import uvicorn
import websockets
from uro_server.app import ServerDeps, create_app


def _deps() -> ServerDeps:
    async def campaign_exists(campaign_id: str) -> bool:
        return campaign_id in ("camp-1", "camp-2")

    async def run_beat(campaign_id: str, participant: str, text: str) -> AsyncIterator[str]:
        yield ""

    return ServerDeps(
        resolve_participant=lambda token: {"tok": "player-1", "scoped": "player-2"}.get(token),
        campaign_exists=campaign_exists,
        run_beat=run_beat,
        token_campaign=lambda token: "camp-1" if token == "scoped" else None,
    )


@contextlib.contextmanager
def _serving() -> Iterator[int]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(_deps()), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def _close_code(port: int, path: str) -> int | None:
    async with websockets.connect(f"ws://127.0.0.1:{port}{path}") as ws:
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), timeout=5)
        return ws.close_code


def test_a_real_client_gets_each_refusal_code() -> None:
    with _serving() as port:
        codes = [
            asyncio.run(_close_code(port, path))
            for path in (
                "/campaigns/camp-1/play?token=nope",  # unknown token
                "/campaigns/nowhere/play?token=tok",  # no such campaign
                "/campaigns/camp-2/play?token=scoped",  # a token minted for another campaign
            )
        ]
    assert codes == [4401, 4404, 4403]
