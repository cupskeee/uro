"""S5 — session lifecycle: disconnect / reconnect mid-heist, late joiners, and which state
survives (event-sourced PC bindings) vs. which is lost (session-only turn state).
"""

from __future__ import annotations

import sys
from pathlib import Path

_GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_GAME_DIR))  # the game modules (host, client, world, frictionlog, ...)
sys.path.insert(0, str(_GAME_DIR / "stress"))  # common (the rig)

import asyncio  # noqa: E402

from client import CrewClient  # noqa: E402
from common import Evidence, rig_up  # noqa: E402
from frictionlog import gap  # noqa: E402


async def _committed_order(clients: list[CrewClient], n: int) -> list[str]:
    """Drive n beats, each by whoever's turn the arbiter says it is — we discover the order by
    letting every live client offer an intent and seeing who commits."""
    order: list[str] = []
    for i in range(n):
        target = len(clients[0].commits()) + 1
        for c in clients:
            await c.send_intent(f"probe beat {i} from {c.participant_id}")
        # exactly one commits; the rest are refused
        while len(clients[0].commits()) < target:
            await clients[0].next_commit()
        order.append(clients[0].commits()[target - 1]["participant_id"])
        await asyncio.sleep(0.1)  # let stray not_your_turn frames drain
    return order


async def main() -> None:
    ev = Evidence("s5_lifecycle")
    rig = await rig_up("s5-lifecycle", n_clients=3)
    a, b, c = rig.clients
    store, hw = rig.store, rig.hw
    try:
        # --- baseline turn order (join order): p1, p2, p3 --------------------------------
        await a.send_intent("I check the gate.")
        await a.wait_for("beat_committed", where={"intent": "I check the gate."})
        await b.send_intent("I check the wall.")
        await b.wait_for("beat_committed", where={"intent": "I check the wall."})
        ev.log("baseline ring = join order: player-1 then player-2 committed in turn")

        # --- advance so the token lands on B (p2), then B vanishes MID-TURN ---------------
        await c.send_intent("I watch the yard.")
        await c.wait_for("beat_committed", where={"intent": "I watch the yard."})
        await a.send_intent("I wave the crew forward.")
        await a.wait_for("beat_committed", where={"intent": "I wave the crew forward."})
        # token now on p2 — and p2 vanishes (mid-turn drop)
        await b.close()
        await a.wait_for("participant_left", where={"participant_id": "player-2"})
        ev.log("player-2 dropped while HOLDING the turn")
        # the table expects the token to pass FORWARD to player-3. Watch what actually happens:
        await c.send_intent("I press on without Doran.")
        refused = await c.wait_for("not_your_turn", where={"text": "I press on without Doran."})
        ev.log(f"player-3 (the next player) was REFUSED: not_your_turn {refused['text']!r}")
        await a.send_intent("I double back to look for Doran.")
        frame = await a.wait_for(
            "beat_committed", where={"intent": "I double back to look for Doran."}
        )
        ev.log(
            f"...and {frame['participant_id']} committed instead — the departing HOLDER's "
            "token went BACKWARD to the player who had JUST acted (a double turn), skipping "
            "player-3. note_left's cursor compensation decrements for idx <= cursor "
            "(session.py:124), which is one-off when the departing participant IS the holder "
            "at a NON-ZERO cursor. The existing engine test "
            "(test_party.py test_party_arbiter_departure_passes_the_token) DOES remove a "
            "3-ring's holder — but at cursor 0, where max(0, cur-1) clamps to 0 and lands "
            "right by coincidence, masking the bug"
        )
        gap(
            gap="The token passes to the NEXT survivor when the turn-holder disconnects "
            "(an engine BUG found by play, not a missing feature)",
            happened="Ring [p1,p2,p3], holder p2 (cursor 1) drops: the token went backward to "
            "p1 (double turn) and p3 was skipped — PartyArbiter.note_left decrements the "
            "cursor whenever removed idx <= cur % (len+1); for the departing HOLDER the ring "
            "removal already shifts the successor into the cursor slot, so the decrement "
            "overshoots. The party does not wedge, but turn fairness silently breaks for "
            "rings of 3+ whenever the holder departs at a non-zero cursor",
            workaround="None in the game (the default arc never drops a client); the probe "
            "documents the misrotation",
            severity="major",
            needs="strict `<` at uro_core/session.py:124 + a departure test with the holder at "
            "a NON-ZERO cursor (test_party.py:215 already removes a 3-ring's holder, but at "
            "cursor 0 the max(0, cur-1) clamp coincidentally lands right and masks the bug)",
            evidence="stress/s5_lifecycle.py mid-turn-drop probe (frames captured); "
            "uro_core/session.py:119-125; uro-core tests/test_party.py:213-223",
        )
        # let the ring settle back to a known holder for the reconnect probe: p3 then holds
        await c.send_intent("I press on without Doran, take two.")
        await c.wait_for("beat_committed", where={"intent": "I press on without Doran, take two."})

        # --- B reconnects on the same token: binding survives, turn position does not ----
        b2 = CrewClient(
            base=rig.server.base,
            campaign_id=hw.campaign.campaign_id,
            token=hw.manifest["crew"][1]["token"],
            participant_id="player-2",
            role="face",
        )
        await b2.connect()
        await b2.wait_for("participant_joined", where={"participant_id": "player-2"})
        rig.clients[1] = b2
        pc = await store.pc_for_participant(hw.campaign.campaign_id, "player-2")  # type: ignore[attr-defined]
        ev.log(
            f"reconnect on the same token: pc_for_participant(player-2) = {pc} — the PC "
            "binding SURVIVED (it is event-sourced: PCBound in the log)"
        )
        order = await _committed_order([a, b2, c], 3)
        ev.log(f"post-reconnect turn order over one full rotation: {order}")
        successor_of_p1 = order[(order.index("player-1") + 1) % 3]
        assert successor_of_p1 == "player-3", order
        ev.log(
            "player-2 rejoined at the END of the ring (join order is session state, not "
            "event-sourced) — the crew's cyclic order silently changed from 1->2->3 to "
            "1->3->2 across a single reconnect"
        )
        gap(
            gap="Turn order survives a reconnect (the crew agreed on an order at the table)",
            happened="The ring is per-session join order (session.py note_joined appends); a "
            "drop+reconnect moves that player to the END — order mutated by transport noise, "
            "and nothing in the event log records or restores it",
            workaround="The default arc never reconnects; this probe documents the mutation",
            severity="major",
            needs="turn/roster state as campaign events (or an arbiter that orders by PC "
            "binding order, which IS event-sourced), per the P7 deferral",
            evidence="stress/s5_lifecycle.py; uro_core/session.py:104-125",
        )

        # --- a 5th player tries to join late ----------------------------------------------
        actor = await store.bind_pc(  # type: ignore[attr-defined]
            hw.campaign.campaign_id, "player-5", new_pc_name="Quill", ruleset_id="uro-basic"
        )
        pcs = await store.active_pcs(hw.branch_id)  # type: ignore[attr-defined]
        ev.log(
            f"library bind_pc seated a 5th PC {actor} (active_pcs now {len(pcs)}) — the "
            "EVENT layer happily takes a late joiner"
        )
        try:
            late = CrewClient(
                base=rig.server.base,
                campaign_id=hw.campaign.campaign_id,
                token="late-joiner-token",
                participant_id="player-5",
                role="fifth",
            )
            await late.connect()
            await late.wait_for("participant_joined", timeout=3)
            ev.log("UNEXPECTED: a token the server was never given connected")
        except Exception as exc:
            ev.log(
                f"but the SESSION layer refused: connecting with an unknown token failed "
                f"({type(exc).__name__}: {exc}) — the token list is frozen at process "
                "launch (uro serve --token ...), so a late 5th player can be SEATED but "
                "never AUTHENTICATED"
            )
        gap(
            gap="A 5th player joins mid-heist (seat + authenticate + take turns)",
            happened="bind_pc seats them in committed state, but `uro serve` tokens are fixed "
            "argv at launch (uro_cli/main.py:485) — no runtime token issuance; the seat exists, "
            "the door does not",
            workaround="Restart the server with 5 tokens (killing every live session) — the "
            "arc pre-seats everyone instead",
            severity="major",
            needs="runtime participant/token management (POST /campaigns/{c}/join returning a "
            "token, or token config reload)",
            evidence="stress/s5_lifecycle.py; uro_cli/main.py:461-505",
        )
        await b2.close()
        ev.log(
            "VERDICT: PC bindings (event-sourced) survive any reconnect; turn ORDER and the "
            "roster (session-only) do not — a holder's mid-turn drop MISROTATES the token "
            "backward (engine bug, gap row), a reconnect reshuffles the table, and a late "
            "seat can never get in the door."
        )
    finally:
        await rig.close()
    ev.flush()


if __name__ == "__main__":
    asyncio.run(main())
