"""S3 — the party-race / turn-token edge: two clients fire for the same turn AT THE SAME TIME.

Is the turn token race-safe? Who wins, does the loser get a clean not_your_turn, and is
anything ever double-committed?
"""

from __future__ import annotations

import sys
from pathlib import Path

_GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_GAME_DIR))  # the game modules (host, client, world, frictionlog, ...)
sys.path.insert(0, str(_GAME_DIR / "stress"))  # common (the rig)

import asyncio  # noqa: E402

from common import Evidence, rig_up  # noqa: E402

ROUNDS = 8


async def main() -> None:
    ev = Evidence("s3_race")
    rig = await rig_up("s3-race", n_clients=2)
    a, b = rig.clients
    store, branch = rig.store, rig.hw.branch_id
    try:
        holders = [a, b]
        for i in range(ROUNDS):
            holder, rival = holders[i % 2], holders[(i + 1) % 2]
            act, jump = f"race round {i}: the holder acts.", f"race round {i}: the rival jumps."
            # both send simultaneously for the SAME turn — the rival is out of turn
            await asyncio.gather(holder.send_intent(act), rival.send_intent(jump))
            committed = await holder.wait_for(
                "beat_committed", where={"participant_id": holder.participant_id, "intent": act}
            )
            refused = await rival.wait_for(
                "not_your_turn", where={"participant_id": rival.participant_id, "text": jump}
            )
            ev.log(
                f"round {i}: committed={committed['participant_id']} "
                f"refused={refused['participant_id']} (clean not_your_turn, no error)"
            )
        beats = await store.recent_beats(branch, ROUNDS * 3)  # type: ignore[attr-defined]
        ev.log(
            f"committed beats after {ROUNDS} contested rounds: {len(beats)} "
            f"(exactly one per round — nothing double-committed, nothing lost)"
        )
        assert len(beats) == ROUNDS, len(beats)
        ev.log(
            "VERDICT: race-safe in-process. PartyArbiter.admit mutates under asyncio's "
            "single-threaded loop with no await between read and decide "
            "(uro_core/session.py:97-101 — atomic by cooperative scheduling, not by lock); "
            "the loser always gets a clean not_your_turn and may retry when the token "
            "rotates. Caveat for the report: that atomicity is an accident of the single "
            "event loop — a multi-process server would need the (unbuilt) expected_head "
            "optimistic-concurrency guard the P7 review deferred."
        )
    finally:
        await rig.close()
    ev.flush()


if __name__ == "__main__":
    asyncio.run(main())
