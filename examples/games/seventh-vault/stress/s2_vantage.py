"""S2 — party co-op + PC-anchored recall: does a beat see the world from THIS PC's vantage?

The heist splits the crew: Sable is on the roof, Vesna is in the Gallery. Each player's beat
should draw on what THEIR thief could plausibly know. This probe measures where Uro anchors a
beat to the acting participant's PC (Phase-7 threading) and where it does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_GAME_DIR))  # the game modules (host, client, world, frictionlog, ...)
sys.path.insert(0, str(_GAME_DIR / "stress"))  # common (the rig)

import asyncio  # noqa: E402
import inspect  # noqa: E402

from common import Evidence, rig_up  # noqa: E402
from frictionlog import gap  # noqa: E402


async def main() -> None:
    ev = Evidence("s2_vantage")
    rig = await rig_up("s2-vantage", n_clients=2)
    vesna, doran = rig.clients
    store, branch = rig.store, rig.hw.branch_id
    try:
        from uro_core.pipeline.recall import assemble_recall

        # 1. ATTRIBUTION is real: each beat is committed under the acting participant's PC.
        await vesna.send_intent("I examine the Gallery cases up close.")
        f1 = await vesna.wait_for("beat_committed", where={"participant_id": "player-1"})
        await doran.send_intent("I keep to the roofline and watch the yard below.")
        f2 = await doran.wait_for("beat_committed", where={"participant_id": "player-2"})
        ev.log(
            f"attribution: beat 1 committed by {f1['participant_id']}, beat 2 by "
            f"{f2['participant_id']} — participant threading is real (P7)"
        )

        # 2. But RECALL has no PC parameter at all — knowledge has no owner.
        sig = str(inspect.signature(assemble_recall))
        ev.log(
            f"assemble_recall{sig} — no participant/pc argument exists; recall is assembled "
            "for the BRANCH, not for a vantage"
        )
        r_vesna = await assemble_recall(store, branch, "What do I know from inside the Gallery?", 8)
        r_doran = await assemble_recall(store, branch, "What do I know from the roof?", 8)
        beats_v = [b.intent_text for b in r_vesna.recent_beats]
        beats_d = [b.intent_text for b in r_doran.recent_beats]
        ev.log(f"recall for the Gallery question sees beats: {beats_v}")
        ev.log(f"recall for the roof question sees beats:    {beats_d}")
        shared = set(beats_v) & set(beats_d)
        assert f1["intent"] in shared and f2["intent"] in shared, (beats_v, beats_d)
        ev.log(
            "BOTH vantages recall BOTH beats — the roof-bound Ghost 'knows' what happened "
            "inside the Gallery cases the moment it commits, and vice versa; the only "
            "personalization lever is the intent text itself (semantic similarity)"
        )
        gap(
            gap="PC-anchored recall (a beat narrates what THIS thief could plausibly know)",
            happened="assemble_recall(store, branch_id, intent_text, k) has no participant/PC "
            "parameter — recall is branch-global; a split crew shares one omniscient memory. "
            "Attribution (who acted) IS threaded (P7), knowledge vantage is not",
            workaround="None inside the engine; the scripts keep the crew's knowledge "
            "convergent so the omniscience never shows on screen",
            severity="major",
            needs="recall anchored to the acting PC: filter/weight claims and beats by what the "
            "PC witnessed, believes (beliefs_of), or was co-located for",
            evidence="stress/s2_vantage.py; pipeline/recall.py assemble_recall signature",
        )

        # 3. Party co-combat: cannot even be attempted — the stub planner starts no encounter,
        # and a live-planner encounter auto-resolves ONE aggressor's fight in one beat (D-29);
        # there is no multi-PC side. Recorded as attempted-and-unreachable.
        ev.log(
            "party co-combat: unreachable — no encounter can start under the stub planner "
            "(BeatPlan always has mechanics=[]), and even live, run_encounter auto-resolves "
            "a single aggressor-vs-defender fight (D-29); a 4-thief fighting retreat has no "
            "expressible shape. The guard response therefore went EXTERNAL (Chronicler)."
        )
        gap(
            gap="Party co-combat (the whole crew fights the guard response together)",
            happened="Encounters are single-aggressor auto-resolved (D-29) and unreachable "
            "under the stub planner anyway; the crew cannot fight as a party",
            workaround="The guard response is an external skirmish (game dice) reported via "
            "the Chronicler — which is exactly the posture Uro designed for this (D-25)",
            severity="annoyance",
            needs="interactive/party encounter mode (deferred per D-29) — or nothing, if "
            "Chronicler-out is the intended answer for party fights (then document it as such)",
            evidence="stress/s2_vantage.py; pipeline/encounter.py run_encounter; heist.py "
            "resolve_skirmish",
        )
        ev.log(
            "VERDICT: PARTIAL — per-PC attribution real (participant -> own PC, P7), "
            "per-PC vantage ABSENT (recall is branch-global omniscience), party co-combat "
            "absent-by-design (external Chronicler is the working path)."
        )
    finally:
        await rig.close()
    ev.flush()


if __name__ == "__main__":
    asyncio.run(main())
