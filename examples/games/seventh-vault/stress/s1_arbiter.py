"""S1 — the headline: the five arbiter shapes the heist WANTS and round-robin can't do (OQ-7).

Each probe builds gameplay that needs the shape, runs it against the real PartyArbiter over the
real WS channel, and captures what actually happened. The verdicts name the specific arbiter
the game needed — that's the deliverable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_GAME_DIR))  # the game modules (host, client, world, frictionlog, ...)
sys.path.insert(0, str(_GAME_DIR / "stress"))  # common (the rig)

import asyncio  # noqa: E402

from common import Evidence, rig_up  # noqa: E402
from frictionlog import gap  # noqa: E402


async def probe_simultaneous(ev: Evidence) -> None:
    """'I pick the lock WHILE you watch the hall' — one fictional moment, two actors."""
    rig = await rig_up("s1-simultaneous", n_clients=2)
    a, b = rig.clients
    try:
        # both send at the same instant, wanting ONE simultaneous beat
        await asyncio.gather(
            a.send_intent("I pick the Gallery lock while Doran watches the hall."),
            b.send_intent("I watch the hall while Vesna picks the lock."),
        )
        committed = await a.wait_for("beat_committed")
        refused = await b.wait_for("not_your_turn")
        ev.log(
            f"wanted: ONE simultaneous beat; got: beat_committed for "
            f"{committed['participant_id']} and not_your_turn for {refused['participant_id']}"
        )
        # the second half of the moment can only happen as the NEXT sequential turn
        await b.send_intent("I watch the hall while Vesna picks the lock.")
        second = await b.wait_for("beat_committed", where={"participant_id": b.participant_id})
        ev.log(
            f"the 'simultaneous' action landed one turn later as its own beat: {second['intent']!r}"
        )
        beats = await rig.store.recent_beats(rig.hw.branch_id, 10)  # type: ignore[attr-defined]
        ev.log(
            f"committed as {len(beats)} sequential beats — the fiction's single moment does "
            "not exist in the log"
        )
        gap(
            gap="A simultaneous/party-action beat ('I pick the lock WHILE you watch the hall')",
            happened="PartyArbiter admits exactly one intent; the partner's send got "
            "not_your_turn and had to re-send NEXT turn — one fictional moment became two "
            "sequential beats with independent narration",
            workaround="Script the crew strictly round-robin; the 'while' is prose only",
            severity="major",
            needs="a simultaneous/parallel arbiter shape: collect co-declared intents into ONE "
            "composite beat (the AdmitDecision.QUEUED value is already reserved for this family)",
            evidence="stress/s1_arbiter.py probe_simultaneous; uro_core/session.py:97-101 admit; "
            "frames in out/stress/s1_arbiter.txt",
        )
    finally:
        await rig.close()


async def probe_proposal_and_vote(ev: Evidence) -> None:
    """The crew debates the plan (proposal window), then decides 'go loud?' by majority."""
    rig = await rig_up("s1-vote", n_clients=3)
    a, b, c = rig.clients
    try:
        # a proposal should be table-talk — instead it must be someone's TURN, and it becomes
        # canonical narrated fiction the GM answers
        await a.send_intent("PROPOSAL: do we go in loud through the gate, or quiet over the wall?")
        frame = await a.wait_for("beat_committed", where={"participant_id": a.participant_id})
        ev.log(
            f"the proposal COMMITTED as canon beat #1 with GM narration: "
            f"{frame['narration'][:60]!r}..."
        )
        # the vote: two more players each burn their whole turn saying a word
        await b.send_intent("I vote we go quiet.")
        await b.wait_for("beat_committed", where={"participant_id": b.participant_id})
        await c.send_intent("I vote quiet too.")
        await c.wait_for("beat_committed", where={"participant_id": c.participant_id})
        beats = await rig.store.recent_beats(rig.hw.branch_id, 10)  # type: ignore[attr-defined]
        ev.log(
            f"a 3-voice table decision consumed {len(beats)} full canon beats (each narrated "
            "by the GM as in-fiction action); no tally exists anywhere"
        )
        gap(
            gap="A proposal window / table-talk phase (debate the plan before anyone acts)",
            happened="No propose-then-act phase exists: a proposal is only expressible as a "
            "player's whole turn, and it commits as canonical narrated fiction",
            workaround="Burn turns on 'PROPOSAL:'/'I vote' beats; tally in game code",
            severity="major",
            needs="a proposal-window arbiter (QUEUED is reserved in AdmitDecision but nothing "
            "implements it) + a non-canon message lane on the WS channel",
            evidence="stress/s1_arbiter.py probe_proposal_and_vote; uro_core/session.py:27-34",
        )
        gap(
            gap="A crew vote ('go loud?') decided by majority, as one table decision",
            happened="No consensus arbiter: three votes = three sequential canon beats in "
            "round-robin order, and the 'majority' is a number only the game's own code knows",
            workaround="The heist scripts avoided votes entirely (the scripts ARE the consensus)",
            severity="annoyance",
            needs="a consensus/vote arbiter shape behind the same TurnArbiter port",
            evidence="stress/s1_arbiter.py probe_proposal_and_vote; out/stress/s1_arbiter.txt",
        )
    finally:
        await rig.close()


async def probe_lookout_interrupt(ev: Evidence) -> None:
    """A guard approaches on C's turn — the lookout must interrupt NOW, not two turns from now."""
    rig = await rig_up("s1-lookout", n_clients=3)
    a, b, c = rig.clients
    try:
        # advance the token to B (beat 1 by A commits, token moves on)
        await a.send_intent("I start on the gate lock, slow and quiet.")
        await a.wait_for("beat_committed", where={"participant_id": a.participant_id})
        # now it is B's turn; A is the lookout and MUST interrupt with a warning
        await a.send_intent("LOOKOUT: a guard is coming, freeze!")
        refused = await a.wait_for("not_your_turn", where={"participant_id": a.participant_id})
        ev.log(f"the lookout's interrupt was refused: not_your_turn {refused['text']!r}")
        await b.send_intent("I keep working the lock, unaware.")
        await b.wait_for("beat_committed", where={"participant_id": b.participant_id})
        await c.send_intent("I stroll across the yard, also unaware.")
        await c.wait_for("beat_committed", where={"participant_id": c.participant_id})
        # only NOW can the warning land — two whole beats of fiction too late
        await a.send_intent("LOOKOUT: a guard is coming, freeze!")
        late = await a.wait_for(
            "beat_committed", where={"intent": "LOOKOUT: a guard is coming, freeze!"}
        )
        ev.log(
            f"the warning finally committed 2 beats late: {late['intent']!r} — in the "
            "fiction, the guard arrived before the warning did"
        )
        gap(
            gap="An out-of-turn interrupt (the lookout warns the crew on someone else's turn)",
            happened="not_your_turn — round-robin has no reactive/interrupt lane; the warning "
            "could only commit two full beats later, after the fiction had moved on",
            workaround="None that preserves the fiction; the default arc simply never posts a "
            "lookout (the role exists in the crew, the play pattern cannot)",
            severity="major",
            needs="a reactive/interrupt arbiter shape: out-of-band intents a rule or the GM can "
            "admit ahead of the token (or a non-canon whisper lane)",
            evidence="stress/s1_arbiter.py probe_lookout_interrupt; uro_core/session.py:97-101",
        )
    finally:
        await rig.close()


async def probe_pvp_double_cross(ev: Evidence) -> None:
    """The betrayal as an ACTION: Sable tries to take the prize off Vesna on her turn."""
    rig = await rig_up("s1-pvp", n_clients=3)
    _a, _b, c = rig.clients
    try:
        # advance to C (Sable): A and B act first
        await _a.send_intent("I hand the Heart to no one and keep walking.")
        await _a.wait_for("beat_committed", where={"participant_id": _a.participant_id})
        await _b.send_intent("I follow close behind.")
        await _b.wait_for("beat_committed", where={"participant_id": _b.participant_id})
        await c.send_intent("I snatch the Heart of the Seventh Vault from Vesna's pack.")
        frame = await c.wait_for(
            "beat_committed",
            where={"intent": "I snatch the Heart of the Seventh Vault from Vesna's pack."},
        )
        prize = await rig.store.get_item(rig.hw.branch_id, "i:prize")  # type: ignore[attr-defined]
        ev.log(
            f"the snatch NARRATED ({frame['narration'][:50]!r}...) but committed nothing: "
            f"i:prize owner is still {prize.get('owner_ref')!r}"
        )
        ev.log(
            "with a live planner the same intent maps to `attack` -> _resolve_encounter "
            "refuses PC defenders (pipeline/engine.py:507-508) -> free-roam narration; with "
            "the stub planner no mechanic fires at all — either way the theft is prose"
        )
        gap(
            gap="A consensual-PvP resolution path (the double-cross as a contested action "
            "between two PCs)",
            happened="Attacking a PC falls back to free-roam by design (engine.py:507-508, the "
            "P7 anti-grief guard) and no non-violent contested-transfer affordance exists; the "
            "snatch narrates and changes nothing",
            workaround="The arc's betrayal ending host-authors the ItemTransferred (heist.py "
            "on_double_cross) — canon by fiat, not by play",
            severity="major",
            needs="the consensual-PvP arbiter shape: target's client consents or contests "
            "(opposed check) before a PC-vs-PC effect commits",
            evidence="stress/s1_arbiter.py probe_pvp_double_cross; pipeline/engine.py:507-508; "
            "heist.py on_double_cross",
        )
    finally:
        await rig.close()


async def main() -> None:
    ev = Evidence("s1_arbiter")
    await probe_simultaneous(ev)
    await probe_proposal_and_vote(ev)
    await probe_lookout_interrupt(ev)
    await probe_pvp_double_cross(ev)
    ev.log(
        "VERDICT: the heist needed FOUR missing arbiter shapes — simultaneous/parallel, "
        "proposal-window (QUEUED reserved, unimplemented), consensus/vote, and "
        "reactive/interrupt — plus a consensual-PvP protocol. Round-robin forced the entire "
        "game into strict-rotation scripts."
    )
    ev.flush()


if __name__ == "__main__":
    asyncio.run(main())
