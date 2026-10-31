"""`uro connect`, the reference play client, against a real server on a local port.

Its prompt used to come back the moment an intent was sent: it sat above the streaming narration,
and nothing was on screen while the beat was still being extracted and committed, so a player in
the first live run (2026-10-31) retyped an intent and played it twice.
"""

import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator

import uvicorn
from typer.testing import CliRunner
from uro_cli.main import app
from uro_server.app import ServerDeps, create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_the_prompt_waits_for_each_beat_to_settle() -> None:
    started: list[str] = []

    async def run_beat(campaign_id: str, participant: str, text: str) -> AsyncIterator[str]:
        started.append(text)
        for word in ("The ", "tide ", "turns."):
            await asyncio.sleep(0.05)
            yield word
        await asyncio.sleep(0.3)  # extraction: the narration is done, the beat isn't

    async def campaign_exists(campaign_id: str) -> bool:
        return True

    deps = ServerDeps(
        resolve_participant={"tok": "player-1"}.get,
        campaign_exists=campaign_exists,
        run_beat=run_beat,
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(deps), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        result = CliRunner().invoke(
            app,
            ["connect", "camp-1", "--server", f"http://127.0.0.1:{port}", "--token", "tok"],
            input="first\nsecond\n/quit\n",  # typed ahead, as an impatient player would
        )
    finally:
        server.should_exit = True
        thread.join(5)
    out = result.output
    assert result.exit_code == 0, out
    assert started == ["first", "second"]
    prompts = [i for i in range(len(out)) if out.startswith("> ", i)]
    first, second = out.index("✓ [player-1] 'first'"), out.index("✓ [player-1] 'second'")
    assert len(prompts) == 3, out
    assert first < prompts[1] < second < prompts[2], out  # each prompt only after its beat settled
