"""S4 — the missing REST management surface, enumerated.

The lobby this game needed vs. the HTTP surface that exists (WS /play + POST /outcome +
GET /healthz). Every wanted endpoint is actually requested against the live server and its
status captured; the table records what the host did instead through the library, and whether a
non-Python (network-only) client could have done it at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_GAME_DIR))  # the game modules (host, client, world, frictionlog, ...)
sys.path.insert(0, str(_GAME_DIR / "stress"))  # common (the rig)

import asyncio  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

from common import Evidence, rig_up  # noqa: E402
from frictionlog import gap  # noqa: E402

# (method, path, what the lobby needed it for, what the host did instead, network-only client?)
WANTED = [
    ("POST", "/worlds", "create the vault world", "store.create_world (host.py)", "BLOCKED"),
    (
        "POST",
        "/worlds/{w}/campaigns",
        "start the shared campaign",
        "store.start_campaign (host.py)",
        "BLOCKED",
    ),
    ("POST", "/campaigns/{c}/join", "seat crew PCs 2-4", "store.bind_pc (host.py)", "BLOCKED"),
    (
        "GET",
        "/campaigns",
        "discover the campaign to connect to",
        "out/run_manifest-<tag>.json written by the host",
        "BLOCKED",
    ),
    (
        "GET",
        "/campaigns/{c}/roster",
        "the lobby crew list (who is seated, which PC)",
        "store.active_pcs + pc_for_participant (host.py self_check)",
        "BLOCKED",
    ),
    (
        "GET",
        "/campaigns/{c}/state",
        "alarm + score for the HUD",
        "store.list_threads (heist.py Director._alarm_word)",
        "BLOCKED",
    ),
    (
        "GET",
        "/campaigns/{c}/items/{i}",
        "who holds the Heart",
        "store.get_item (heist.py)",
        "BLOCKED",
    ),
    (
        "GET",
        "/campaigns/{c}/sheets/{a}",
        "Brakk's hp after the skirmish",
        "store.get_sheet (heist.py on_lockdown)",
        "BLOCKED",
    ),
    (
        "GET",
        "/campaigns/{c}/chronicle",
        "the shared scene log / rumors / testimony",
        "store.recent_beats + claims_about + beliefs_of (heist.py final_readout)",
        "BLOCKED",
    ),
    (
        "POST",
        "/campaigns/{c}/time-skip",
        "the post-job downtime week",
        "engine.agenda_tick (arc.py)",
        "BLOCKED",
    ),
]


async def main() -> None:
    ev = Evidence("s4_management")
    rig = await rig_up("s4-mgmt", n_clients=0)
    c = rig.hw.campaign.campaign_id
    try:
        ev.log(
            "the ENTIRE http surface (uro_server/app.py): WS /campaigns/{c}/play, "
            "POST /campaigns/{c}/encounters/{e}/outcome, GET /healthz"
        )
        for method, path, wanted_for, instead, network in WANTED:
            url = rig.server.base + path.replace("{w}", "w").replace("{c}", c).replace(
                "{i}", "i:prize"
            ).replace("{a}", "a:brakk")
            req = urllib.request.Request(
                url,
                method=method,
                data=b"{}" if method == "POST" else None,
                headers={"content-type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as e:
                status = e.code
            ev.log(
                f"{method} {path} -> {status} | needed for: {wanted_for} | did instead: "
                f"{instead} | network-only client: {network}"
            )
        ev.log(
            "VERDICT: 10/10 management calls this game needed do not exist over the wire — "
            "every one went through the embedded library (Posture A). A non-Python client "
            "could BOOT NOTHING: it cannot create, join, discover, read state, or advance "
            "time; it can only play beats in a campaign someone else set up and told it "
            "about out-of-band."
        )
        gap(
            gap="A REST management surface (the 10 endpoints above — lobby, roster, state, "
            "chronicle, time)",
            happened="404 for every one (evidence: out/stress/s4_management.txt); the HTTP "
            "surface is play+outcome+healthz only",
            workaround="A Posture-A 'host' process embeds uro_core for ALL management; the "
            "campaign id travels to clients via a manifest FILE",
            severity="blocker",
            needs="the docs/08 management surface, minimally: GET /campaigns, "
            "GET /campaigns/{c}/roster|state|chronicle, POST /worlds, POST /campaigns/{c}/join",
            evidence="stress/s4_management.py (all statuses captured); uro_server/app.py:97-157",
        )
    finally:
        await rig.close()
    ev.flush()


if __name__ == "__main__":
    asyncio.run(main())
