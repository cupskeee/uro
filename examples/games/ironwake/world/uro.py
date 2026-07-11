"""The two Uro posture backends (TASK inc 7, brief section 3).

- EMBED (Posture A): `uro_core` in-process. Writes = distill_outcome + append_beat + react;
  narration = engine.run_beat. Fully deterministic with the stub provider; the CI path.
- SERVER (Posture B): boots `uro serve` as a subprocess, POSTs OutcomeBundles over HTTP and
  narrates town scenes over WS — the network Chronicler contract. READS still go through the
  embedded library (world/reads.py), because no REST read surface exists; the exact endpoints
  a real network game would have needed are logged here (stress goal 6, the headline gap).

Both postures share one Postgres and one world; the season driver only swaps the write/narrate
calls, which is exactly the comparison the TASK wants documented.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass

import websockets
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.chronicler import OutcomeBundle
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.router import ProviderRouter

from ironwake import frictionlog

DEFAULT_DSN = os.environ.get("URO_DATABASE_URL", "postgresql://uro:uro@localhost:5433/uro")
# Off the beaten 8000 so a dev `uro serve` doesn't collide; override for parallel runs/tests.
SERVER_PORT = int(os.environ.get("IRONWAKE_SERVER_PORT", "8971"))
SERVER_TOKEN = "ironwake-season"  # maps to participant player-1 (first --token)


def build_router(provider: str, model: str | None) -> ProviderRouter:
    """stub (default, deterministic, no key) or an opt-in real model via the CLI's wiring."""
    if provider == "stub":
        return ProviderRouter(bindings={}, default=StubProvider())
    from uro_cli.wiring import build_router as cli_build_router

    return cli_build_router(provider, model)


@dataclass
class UroSession:
    """The embedded engine — always present (even in server posture, for reads/setup/fork)."""

    store: PostgresEventStore
    engine: Engine

    @classmethod
    async def connect(
        cls, dsn: str = DEFAULT_DSN, provider: str = "stub", model: str | None = None
    ) -> UroSession:
        store = PostgresEventStore(dsn)
        await store.connect()
        await store.migrate()
        return cls(store=store, engine=Engine(store, build_router(provider, model)))

    async def close(self) -> None:
        await self.store.close()


# --- the server posture -------------------------------------------------------------------------


def _reap(proc: subprocess.Popen) -> None:
    """terminate -> wait -> kill: a uvicorn child that shrugs off SIGTERM must not leak."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@dataclass
class ServerHandle:
    proc: subprocess.Popen
    base_url: str
    token: str

    @classmethod
    def start(
        cls, dsn: str = DEFAULT_DSN, port: int | None = None, token: str = SERVER_TOKEN
    ) -> ServerHandle:
        """Boot `uro serve --provider stub` as a child process and wait for /healthz.

        Pre-flight: the port must be FREE before spawning — otherwise a foreign process
        already answering /healthz makes the poll below declare OUR child healthy while it
        dies of a bind failure (a reviewer reproduced exactly that race). Fail loudly."""
        port = port if port is not None else SERVER_PORT
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(
                    f"port {port} is already in use — set IRONWAKE_SERVER_PORT to a free one"
                )
        snippet = (
            "from uro_cli.main import app; "
            f"app(['serve', '--port', '{port}', '--token', '{token}', '--provider', 'stub'])"
        )
        env = dict(os.environ, URO_DATABASE_URL=dsn)
        proc = subprocess.Popen(
            [sys.executable, "-c", snippet],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("uro serve exited during startup")
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=1) as resp:
                    if resp.status == 200:
                        return cls(proc=proc, base_url=base, token=token)
            except OSError:
                time.sleep(0.2)
        _reap(proc)
        raise RuntimeError("uro serve did not become healthy in 30s")

    def stop(self) -> None:
        _reap(self.proc)

    # --- the network Chronicler contract ---

    def post_outcome(self, campaign_id: str, bundle: OutcomeBundle) -> dict:
        """POST the bundle. The response is ONLY {committed_events, commit_id} — the server
        does not say WHICH casualties committed vs downgraded, so even the network posture
        must fall back to library reads to learn what the world accepted (logged below)."""
        url = (
            f"{self.base_url}/campaigns/{campaign_id}/encounters/{bundle.encounter_id}"
            f"/outcome?token={self.token}"
        )
        body = json.dumps(bundle.model_dump()).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    async def narrate_ws(self, campaign_id: str, intent: str) -> str:
        """Send one intent over the WS play channel; collect streamed narration until the beat
        commits. Raises on beat_failed so a broken server posture is loud, not silent."""
        uri = (
            f"ws://{self.base_url.removeprefix('http://')}"
            f"/campaigns/{campaign_id}/play?token={self.token}"
        )
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "intent", "text": intent}))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if frame.get("type") == "beat_committed":
                    return str(frame.get("narration", ""))
                if frame.get("type") == "beat_failed":
                    raise RuntimeError(f"beat failed over WS: {frame.get('error')}")


def log_server_read_gap() -> None:
    """Filed once per server-posture run: the headline REST gap, with the endpoints we needed."""
    frictionlog.gap(
        gap="read roster/chronicle/rumors over HTTP to render towns in --posture server",
        happened=(
            "the entire HTTP surface is WS /play + POST /outcome + GET /healthz; every read "
            "(list_actors, claims_about, beliefs_of, list_threads, items_owned_by, "
            "current_world_time) had to go through a SECOND in-process library connection to "
            "the same Postgres — impossible for a game not written in Python on the same host"
        ),
        workaround=(
            "world/reads.py runs on the embedded store even in server posture (a network game "
            "that cannot read back what it wrote)"
        ),
        severity="blocker",
        needs=(
            "a REST read surface: GET /worlds/{w}/branches/{b}/actors · /actors/{id} · "
            "/claims?about= · /beliefs/{actor} · /threads · /edges?rel= · /items?owner= · "
            "/time · /campaigns/{c}/chronicle — plus POST /outcome returning per-item verdicts"
        ),
        evidence="world/uro.py ServerHandle.post_outcome + world/reads.py (all of it)",
    )
    frictionlog.gap(
        gap="learn from the outcome POST what was accepted vs downgraded/refused",
        happened=(
            "POST /outcome returns {committed_events: N, commit_id} — no per-casualty/per-loot "
            "verdict; the Vorlund downgrade is invisible in the response and must be inferred "
            "by re-reading canon through the library"
        ),
        workaround="world/chronicle.py read_back diffs bundle claims against projections",
        severity="major",
        needs="a structured ingestion receipt: per-ref {committed|downgraded|dropped, reason}",
        evidence="world/uro.py ServerHandle.post_outcome (response schema)",
    )
    frictionlog.gap(
        gap="narrate over WS with the same narrative-only engine the embed posture uses",
        happened=(
            "`uro serve` always binds a ruleset (default uro-basic) to its one Engine, so WS "
            "town beats run the planner/mechanics pipeline while embed beats (Engine with no "
            "ruleset) run the plain Phase-1 flow — same stub prose, different pipeline; a "
            "narration-only campaign cannot ask the server to skip mechanics"
        ),
        workaround="none needed with the stub (its planner reply is an empty valid plan)",
        severity="annoyance",
        needs="per-campaign engine/ruleset binding on the server (the documented D-30 deferral)",
        evidence="world/uro.py ServerHandle.start vs UroSession.connect; uro_cli/main.py serve",
    )
