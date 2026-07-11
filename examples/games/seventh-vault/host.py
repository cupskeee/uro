"""The host/lobby — Posture A (embedded `uro_core`), because there is no management API.

Everything in this file is work a real network client COULD NOT DO: create the world, start the
campaign, seat four PCs, discover the campaign id, read the roster. The only HTTP surface Uro
exposes is WS /play + POST /outcome + GET /healthz (uro_server/app.py), so the host embeds the
engine library and then hands off to `uro serve` for play. Every management action done here
through the library instead of an endpoint is logged to the friction log (stress goal S4).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frictionlog import gap
from rule_pack import HEIST_RULE_PACK
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.router import ProviderRouter
from uro_core.timeline.models import Campaign
from world import (
    ALARM_THREAD,
    ALARM_WORDS,
    CREW,
    DSN_DEFAULT,
    LAYERS,
    PRIZE,
    RULESET_ID,
    SCORE_THREAD,
    SCORE_WORDS,
    crew_sheet,
    genesis_events,
)

OUT_DIR = Path(__file__).resolve().parent / "out"


def dsn() -> str:
    return os.environ.get("URO_DATABASE_URL", DSN_DEFAULT)


async def connect_store() -> PostgresEventStore:
    store = PostgresEventStore(dsn())
    await store.connect()
    await store.migrate()
    return store


def host_engine(store: PostgresEventStore) -> Engine:
    """A host-side Engine for the library-only calls (react on authored commits, agenda_tick).
    Stub provider — none of those paths touch a model."""
    return Engine(store, ProviderRouter(bindings={}, default=StubProvider()))


# --------------------------------------------------------------------------------------------
# World + campaign bootstrap (Stage 1)
# --------------------------------------------------------------------------------------------


@dataclass
class HeistWorld:
    world_id: str
    branch_id: str
    campaign: Campaign
    manifest: dict[str, Any]


async def build_world(store: PostgresEventStore, *, server_port: int, run_tag: str) -> HeistWorld:
    """Create the vault, seat the crew on ONE campaign, and emit the run manifest."""
    gap(
        gap="Create a world + campaign over HTTP (a lobby's first two calls)",
        happened="No such endpoints exist — the HTTP surface is WS /play + POST /outcome + "
        "GET /healthz only (uro_server/app.py:97-157)",
        workaround="The host embeds uro_core (Posture A): store.create_world + start_campaign "
        "+ bind_pc against Postgres directly",
        severity="major",
        needs="POST /worlds, POST /worlds/{w}/campaigns (docs/08 lists them as unbuilt)",
        evidence="host.py build_world -> store.create_world/start_campaign/bind_pc; "
        "uro_server/app.py:97-157 (the entire route table)",
    )
    world = await store.create_world(
        "The Seventh Vault",
        tone=["heist", "tense", "noir"],
        ruleset_id=RULESET_ID,
        rule_pack=HEIST_RULE_PACK,
        extra_events=genesis_events(),
    )
    branch = world.main_branch_id

    # ONE campaign, four participants. `uro serve` maps bearer tokens POSITIONALLY to
    # player-1..player-N (uro_cli/main.py:485-486: {t: f"player-{i+1}"}), so the participant
    # ids we bind here MUST be player-1..player-4 in CREW/token order — they are NOT the
    # token strings, whatever a lobby would prefer to name its seats.
    gap(
        gap="Name the participants (participant_id == the crew token, e.g. 'crew-ghost')",
        happened="`uro serve --token A --token B` maps tokens positionally to player-1..N "
        "(uro_cli/main.py:485-486); the participant identity is an accident of flag order and "
        "invisible to the client until frames arrive",
        workaround="Bind PCs to player-1..player-4 and carry the token->participant->actor "
        "mapping in run_manifest.json; token order is load-bearing config",
        severity="annoyance",
        needs="`--token TOKEN=PARTICIPANT` (or a join endpoint that returns the mapping)",
        evidence="host.py build_world; uro_cli/main.py:485-486",
    )
    _role, _token, lead_actor, _name, lead_emph = CREW[0]
    campaign = await store.start_campaign(
        world.world_id,
        branch,
        participant_id="player-1",
        adopt_actor_id=lead_actor,
        pc_sheet=crew_sheet(lead_emph),
        ruleset_id=RULESET_ID,
        seed=1234,  # recorded in CampaignStarted but UNUSED by the beat RNG — see the gap below
    )
    for i, (_role, _token, actor_id, _name, emph) in enumerate(CREW[1:], start=2):
        await store.bind_pc(
            campaign.campaign_id,
            f"player-{i}",
            adopt_actor_id=actor_id,
            pc_sheet=crew_sheet(emph),
            ruleset_id=RULESET_ID,
        )
    gap(
        gap="Pin the campaign's dice (a deterministic heist wants seeded checks)",
        happened="start_campaign(seed=1234) stores the seed in CampaignStarted but nothing "
        "reads it: the per-beat RNG is sha256(campaign_id:head_commit) "
        "(pipeline/engine.py:632-638) and campaign_id is a fresh ulid per run — mechanically "
        "resolved outcomes can never be reproduced across two runs",
        workaround="The default arc never relies on an engine roll (the stub planner resolves "
        "zero checks anyway); the game's own dice (skirmish) use the game's own fixed seed",
        severity="major",
        needs="wire CampaignStarted.seed into _beat_rng (seed XOR head), or accept a caller "
        "campaign_id/seed override",
        evidence="host.py build_world(seed=1234); pipeline/engine.py:632-638; "
        "stress/s7_counters.py cross-run roll probe",
    )

    manifest = {
        "world_id": world.world_id,
        "branch_id": branch,
        "campaign_id": campaign.campaign_id,
        "ruleset_id": RULESET_ID,
        "server": {"host": "127.0.0.1", "port": server_port},
        "run_tag": run_tag,
        "crew": [
            {
                "role": role,
                "token": token,
                "participant_id": f"player-{i + 1}",
                "actor_id": actor_id,
                "name": name,
            }
            for i, (role, token, actor_id, name, _e) in enumerate(CREW)
        ],
        "places": [p for p, _ in LAYERS],
        "threads": {
            ALARM_THREAD: {"states": ALARM_WORDS},
            SCORE_THREAD: {"states": SCORE_WORDS},
        },
        "prize": PRIZE,
    }
    gap(
        gap="Clients discover the shared campaign (GET /campaigns, or a lobby code)",
        happened="No discovery endpoint — a WS client must already know the campaign_id, and "
        "nothing over the wire will ever tell it one",
        workaround="The host writes out/run_manifest-<tag>.json and clients read the id from "
        "disk — a file IS the lobby",
        severity="major",
        needs="GET /campaigns (+ campaign metadata: branch, ruleset, participants)",
        evidence="host.py write_manifest; uro_server/app.py (no such route)",
    )
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"run_manifest-{run_tag}.json").write_text(json.dumps(manifest, indent=2))
    return HeistWorld(world.world_id, branch, campaign, manifest)


async def self_check_world(store: PostgresEventStore, hw: HeistWorld) -> None:
    """Stage-1 self-check: re-read the world through projections and assert it matches the
    manifest. Every one of these reads is a library call a network lobby could not make (S4)."""
    gap(
        gap="Read the crew roster / world state over HTTP (the lobby screen)",
        happened="No REST read surface at all — roster, sheets, threads, items, places, "
        "claims, beats are all library-only projections",
        workaround="Posture-A reads: active_pcs, pc_for_participant, get_sheet, list_threads, "
        "list_places, get_item, items_owned_by, claims_about, recent_beats",
        severity="major",
        needs="GET /campaigns/{c}/roster, /state, /threads, /items, /chronicle (docs/08)",
        evidence="host.py self_check_world + heist.py readouts; uro_server/app.py route table",
    )
    branch = hw.branch_id
    pcs = await store.active_pcs(branch)
    expected = sorted(c["actor_id"] for c in hw.manifest["crew"])
    assert sorted(pcs) == expected, f"active_pcs {pcs} != crew {expected}"
    gap(
        gap="A campaign-scoped roster read (the lobby wants 'who is in THIS game')",
        happened="active_pcs is BRANCH-scoped (store.py:619); fine here (one campaign per "
        "branch by construction) — campaign_pcs exists internally but is not part of the "
        "documented read surface",
        workaround="Used the branch read; one campaign per branch",
        severity="cosmetic",
        needs="document/expose campaign_pcs in the projection read surface",
        evidence="host.py self_check_world; adapters/postgres/store.py active_pcs",
    )
    for c in hw.manifest["crew"]:
        pc = await store.pc_for_participant(hw.campaign.campaign_id, c["participant_id"])
        assert pc == c["actor_id"], f"{c['participant_id']} -> {pc}, wanted {c['actor_id']}"
        sheet = await store.get_sheet(branch, c["actor_id"])
        assert sheet and "hp" in sheet, f"{c['actor_id']} has no uro-basic sheet"
    threads = {t.thread_id: t.state for t in await store.list_threads(branch)}
    assert threads[ALARM_THREAD] == "dormant", threads  # calm, in the pun vocabulary
    assert threads[SCORE_THREAD] == "dormant", threads  # pending
    places = {p.place_id for p in await store.list_places(branch)}
    assert {p for p, _ in LAYERS} <= places, places
    prize = await store.get_item(branch, PRIZE)
    assert prize and prize.get("owner_ref") == "p:seventh-vault", prize
    print("[host] stage-1 self-check PASS: 4 PCs seated, threads calm/pending, prize in place")


# --------------------------------------------------------------------------------------------
# The server process (Posture B) — mirrors ironwake's ServerHandle pattern
# --------------------------------------------------------------------------------------------


@dataclass
class ServerHandle:
    port: int
    proc: subprocess.Popen[bytes]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_server(
    *, port: int, tokens: list[str], provider: str = "stub", ruleset: str = RULESET_ID
) -> ServerHandle:
    """Boot `uro serve` as a subprocess (one ruleset, one process) and wait for /healthz."""
    args = ["serve", "--port", str(port), "--provider", provider, "--ruleset", ruleset]
    for t in tokens:
        args += ["--token", t]
    snippet = f"from uro_cli.main import app; app({args!r})"
    proc = subprocess.Popen(
        [sys.executable, "-c", snippet],
        env={**os.environ, "URO_DATABASE_URL": dsn()},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    url = f"http://127.0.0.1:{port}/healthz"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if json.loads(resp.read()).get("status") == "ok":
                    return ServerHandle(port, proc)
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    proc.terminate()
    try:
        proc.wait(timeout=10)  # reap — don't leave the child mid-SIGTERM while we unwind
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    raise RuntimeError(f"uro serve did not become healthy on port {port}")
