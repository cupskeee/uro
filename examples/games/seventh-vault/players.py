"""Scripted crew — four concurrent `ScriptedPlayer` tasks obeying round-robin turn discipline.

Each player owns only ITS lines of the heist script. Turn inference is honest multiplayer: a
player watches the shared broadcast stream and acts when the number of committed beats says the
round-robin token is theirs (ring order == join order == CREW order, established by the arc
connecting clients sequentially). A `Pacer` lets the director (heist.py) hold the table between
beats — the skirmish must land before the next thief moves, or frame order (and the byte-stable
log) would race.

The scripts are STRICT round-robin (beat k belongs to crew k%4) because that is the only shape
the PartyArbiter admits — the moment the fiction wanted anything else (a lookout interrupting,
two actions at once, a vote) it hit S1; see stress/s1_arbiter.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from client import CrewClient
from rule_pack import (
    BLUNDER_GALLERY,
    BLUNDER_GATE,
    BLUNDER_HUB,
    GETAWAY_BETRAYED,
    GETAWAY_CLEAN,
)

# --- the heist script: (crew index, intent). Beat k MUST belong to crew k%4 (round-robin). ---
_COMMON = [
    # round 1 — the Outer Gate
    (0, "I read the Outer Gate's lock by shuttered lantern."),
    (1, "I spin the gate sentry a tale about a late wine delivery."),
    (2, "I go over the wall and slip the Outer Gate's inner bar."),
    (3, BLUNDER_GATE),  # alarm: calm -> suspicious (rule alarm-1)
    # round 2 — the Gallery
    (0, "I pick the Gallery door's triple lock."),
    (1, "I talk the Gallery guard into walking his round the long way."),
    (2, "I ghost along the Gallery cornice above the cases."),
    (3, BLUNDER_GALLERY),  # alarm: suspicious -> alerted (rule alarm-2)
    # round 3 — the Security Hub
    (0, "I splice the alarm line where it enters the Security Hub."),
    (1, "I mimic the Warden's voice into the Security Hub speaking tube."),
    (2, "I scout the Antechamber approach from the rafters."),
    (3, BLUNDER_HUB),  # alarm: alerted -> LOCKDOWN (rule alarm-3) -> the guard response
    # round 4 — the Antechamber and the seventh door
    (0, "I answer the Antechamber's puzzle-lock with the seventh word."),
    (1, "I keep the stair watched while the seventh door swings."),
    (2, "I rig our line down the coal chute for the getaway."),
    (3, "I haul the Seventh Vault's door wide."),
    # round 5 — the take and the getaway
    (0, "I lift the Heart of the Seventh Vault from its cradle."),  # -> host authors the take
    (1, "I lead the crew into the coal chute, prize first."),
]

CLEAN_SCRIPT = [
    *_COMMON,
    (2, "I wipe our scuff-marks from the chute lip."),
    (3, GETAWAY_CLEAN),  # score: prize-taken -> escaped (rule score-2)
]

BETRAYAL_SCRIPT = [
    *_COMMON,
    (2, "I slip the Heart from Vesna's pack in the dark of the chute."),  # -> host authors theft
    (3, GETAWAY_BETRAYED),  # score: prize-taken -> betrayed (rule score-3)
]

# 0-based beat indices the director hooks (heist.py): assert-alarm + act
HOOK_GATE = 3
HOOK_GALLERY = 7
HOOK_LOCKDOWN = 11
HOOK_PRIZE = 16
HOOK_THEFT = 18  # betrayal script only


class Pacer:
    """Per-beat clearance gates. All beats start clear; the director claims the gate AFTER a
    hooked beat so no player moves until the hook's canon (skirmish, prize transfer) lands."""

    def __init__(self, n_beats: int) -> None:
        self._gates = [asyncio.Event() for _ in range(n_beats + 1)]
        for g in self._gates:
            g.set()

    def hold(self, beat_index: int) -> None:
        self._gates[beat_index].clear()

    def release(self, beat_index: int) -> None:
        self._gates[beat_index].set()

    async def wait(self, beat_index: int) -> None:
        await self._gates[beat_index].wait()


@dataclass
class BeatRecord:
    index: int
    participant_id: str
    intent: str
    narration: str


class ScriptedPlayer:
    """One crew member: sends its scripted intents on its own turns, holds otherwise."""

    def __init__(
        self,
        client: CrewClient,
        crew_index: int,
        n_players: int,
        script: list[tuple[int, str]],
        pacer: Pacer,
    ) -> None:
        self.client = client
        self.crew_index = crew_index
        self.n_players = n_players
        self.my_beats = [(k, text) for k, (idx, text) in enumerate(script) if idx == crew_index]
        self.pacer = pacer

    async def play(self) -> None:
        for beat_index, intent in self.my_beats:
            await self.pacer.wait(beat_index)
            # turn inference: it's my turn when exactly beat_index beats have committed
            while len(self.client.commits()) < beat_index:
                await self.client.next_commit()
            await self.client.send_intent(intent)
            frame = await self.client.wait_for(
                "beat_committed", where={"intent": intent}, timeout=60
            )
            if frame["participant_id"] != self.client.participant_id:
                raise AssertionError(
                    f"beat {beat_index}: committed under {frame['participant_id']}, "
                    f"sent by {self.client.participant_id}"
                )


async def run_crew(
    clients: list[CrewClient],
    script: list[tuple[int, str]],
    pacer: Pacer,
) -> list[BeatRecord]:
    """Run all players concurrently; afterwards assert THE SHARED SCENE — every client received
    the identical committed beat sequence — and return the canonical beat log."""
    from frictionlog import gap

    gap(
        gap="Correlate a WS beat to committed state (which commit? which beat_id? did a check "
        "resolve? was anything extracted?)",
        happened="The beat_committed frame carries only {participant_id, intent, narration} "
        "(uro_server/app.py:224-232) — no beat_id, no commit_id, no checks, no extracted "
        "events; clients must identify beats by their intent TEXT (which forces every scripted "
        "intent to be unique) and learn consequences via library reads",
        workaround="players.py keys all frame-waits on exact intent strings; heist.py reads "
        "projections after each hooked beat",
        severity="major",
        needs="ids + a mechanics/extraction summary on beat_committed (the BeatResult fields "
        "already exist in-process)",
        evidence="players.py run_crew/ScriptedPlayer.play; uro_server/app.py:224-232",
    )
    # The whole frame-correlation scheme (G-14) keys on intent TEXT, so scripted intents MUST be
    # unique — a silent duplicate would corrupt every wait and the evidence built on them.
    texts = [text for _idx, text in script]
    assert len(set(texts)) == len(texts), "scripted intents must be unique (G-14 correlation)"
    players = [ScriptedPlayer(c, i, len(clients), script, pacer) for i, c in enumerate(clients)]
    async with asyncio.TaskGroup() as tg:
        for p in players:
            tg.create_task(p.play())
    # every client drains to the full script length, then logs must match byte-for-byte
    logs: list[list[BeatRecord]] = []
    for c in clients:
        while len(c.commits()) < len(script):
            await c.next_commit()
        logs.append(
            [
                BeatRecord(i, f["participant_id"], f["intent"], f["narration"])
                for i, f in enumerate(c.commits()[: len(script)])
            ]
        )
    for other, cl in zip(logs[1:], clients[1:], strict=True):
        if other != logs[0]:
            raise AssertionError(f"shared scene broken: {cl.participant_id} saw a different log")
    for k, rec in enumerate(logs[0]):
        want_idx, want_text = script[k]
        if rec.intent != want_text or rec.participant_id != clients[want_idx].participant_id:
            raise AssertionError(f"beat {k} committed out of order: {rec}")
    return logs[0]
