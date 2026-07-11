"""arc.py — the full default heist: one shared campaign, four scripted thieves, the stub GM.

    uv run python examples/games/seventh-vault/arc.py --ending clean
    uv run python examples/games/seventh-vault/arc.py --ending betrayal

Deterministic, zero API keys. Everything the run claims at the end — the alarm the crew left,
who holds the Heart, who died, what rumor spread — is read back from Uro's committed state, and
the whole observable arc is folded into out/digest-<ending>.txt: two runs of the same ending
must produce byte-identical digests (run.sh does exactly that comparison).

Opt-in live mode: --provider openai narrates with a real model over the same wire. NOT the
default, NOT deterministic, needs OPENAI_API_KEY in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import frictionlog
import host
from client import CrewClient
from frictionlog import gap
from heist import Director, assert_ending, digest, final_readout
from players import BETRAYAL_SCRIPT, CLEAN_SCRIPT, Pacer, run_crew
from rule_pack import register_refusals
from world import CREW


async def run_arc(ending: str, *, provider: str = "stub", port: int | None = None) -> str:
    """One complete heist; returns the run digest."""
    register_refusals()
    if provider == "stub":
        gap(
            gap="'A player's intent maps to a d20 check via uro-basic through run_beat' — in "
            "the DEFAULT deterministic mode (the only mode CI can run)",
            happened="The mechanics gate is planner-gated and the planner is an LLM stage: "
            "StubProvider's planner answer is the constant no-op "
            '\'{"intent_class": "action", "triggers": [], "mechanics": []}\' '
            "(providers/adapters/stub.py:49-54) — so with `--provider stub` NO check ever "
            "resolves and NO encounter can start, for any intent, under any ruleset. The "
            "engine's entire mechanics layer is unreachable without a live LLM key",
            workaround="The heist's obstacles resolve as scripted narrative beats; every "
            "mechanical consequence (alarm, prize, wounds, deaths) is driven by Reaction-Layer "
            "rules on committed events, host-authored events, or the game's own dice via the "
            "Chronicler — Uro rolled nothing",
            severity="blocker",
            needs="a deterministic planner path that needs no model: either a rule-based "
            "planner fallback (intent keyword -> affordance), a client-supplied plan on the "
            "intent frame, or a scriptable planner provider over the wire",
            evidence="arc.py run_arc(provider='stub'); providers/adapters/stub.py:49-54; "
            "stress/s7_counters.py (b)",
        )
    script = CLEAN_SCRIPT if ending == "clean" else BETRAYAL_SCRIPT
    store = await host.connect_store()
    server = None
    clients: list[CrewClient] = []
    try:
        hw = await host.build_world(store, server_port=port or host.free_port(), run_tag=ending)
        await host.self_check_world(store, hw)
        server = host.start_server(
            port=hw.manifest["server"]["port"], tokens=[c[1] for c in CREW], provider=provider
        )
        print(f"[arc] uro serve up on {server.base} (provider={provider}, 4 tokens)")

        # Connect in CREW order, one at a time — the PartyArbiter ring is JOIN order, and the
        # scripts assume ring == CREW order. (Turn order is an accident of connection timing:
        # session state, not event-sourced — see stress/s5_lifecycle.py.)
        for seat in hw.manifest["crew"]:
            c = CrewClient(
                base=server.base,
                campaign_id=hw.campaign.campaign_id,
                token=seat["token"],
                participant_id=seat["participant_id"],
                role=seat["role"],
            )
            await c.connect()
            await c.wait_for("participant_joined", where={"participant_id": seat["participant_id"]})
            clients.append(c)
        print(f"[arc] {len(clients)} crew seated: " + ", ".join(c.role for c in clients))

        director = Director(store, host.host_engine(store), hw, server, ending)
        pacer = Pacer(len(script))
        for beat_index in director.hooks():
            if beat_index + 1 < len(script):
                pacer.hold(beat_index + 1)

        async with asyncio.TaskGroup() as tg:
            crew_task = tg.create_task(run_crew(clients, script, pacer))
            tg.create_task(director.run(clients[0], script, pacer))
        beat_log = crew_task.result()
        print(f"[arc] {len(beat_log)} beats committed; shared scene verified across 4 clients")

        # the week after the job: downtime spreads whichever legend the score earned
        await host.host_engine(store).agenda_tick(hw.branch_id, 7)

        readout = await final_readout(store, hw, ending, director.skirmish or _no_skirmish())
        assert_ending(readout)
        d = digest(readout, beat_log)

        host.OUT_DIR.mkdir(exist_ok=True)
        (host.OUT_DIR / f"digest-{ending}.txt").write_text(d + "\n")
        (host.OUT_DIR / f"beatlog-{ending}.txt").write_text(
            "\n".join(f"{b.participant_id} | {b.intent} | {b.narration}" for b in beat_log) + "\n"
        )
        _print_finale(readout)
        return d
    finally:
        for c in clients:
            with contextlib.suppress(Exception):  # one bad socket must not skip server/store
                await c.close()
        if server is not None:
            server.stop()
        await store.close()


def _no_skirmish():  # pragma: no cover — the lockdown hook always runs in both scripts
    raise AssertionError("the arc ended without a lockdown skirmish")


def _print_finale(r: dict) -> None:
    print("\n=== THE SEVENTH VAULT — read back from committed Uro state ===")
    print(f"  ending:        {r['ending']}")
    print(f"  t:alarm:       {r['alarm']} (the House is locked down behind them)")
    print(f"  t:score:       {r['score']}")
    print(f"  the Heart:     held by {r['prize_owner']}")
    print(
        f"  the fallen:    Ott={r['statuses']['a:guard-7']}, "
        f"Umble={r['statuses']['a:guard-11']}, Warden={r['statuses']['a:warden']}"
    )
    for s in r["warden_testimony"]:
        print(f"  testimony:     “{s}” (truth=unknown — the game could not kill a tier-3)")
    for actor, pairs in sorted(r["feat_beliefs"].items()):
        for claim, conf in pairs:
            print(f"  belief:        {actor} believes {claim} at {conf}")
    for s in r["legend"]:
        print(f"  the legend:    “{s}”")
    print(f"  Brakk's hp:    {r['brakk_hp']} (wounds authored — the bundle had no field)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ending", choices=["clean", "betrayal"], default="clean")
    ap.add_argument("--provider", default="stub", help="stub (default) | openai | anthropic")
    ap.add_argument("--port", type=int, default=None, help="server port (default: ephemeral)")
    ap.add_argument("--print-log", action="store_true", help="print friction+refusal logs")
    args = ap.parse_args()
    d = asyncio.run(run_arc(args.ending, provider=args.provider, port=args.port))
    print(f"\n[arc] digest ({len(d)} bytes) -> out/digest-{args.ending}.txt")
    if args.print_log:
        frictionlog.print_gap_table()
        frictionlog.print_refusal_log()


if __name__ == "__main__":
    main()
