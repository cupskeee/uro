"""The pacing director (D-95; OQ-2's director half): name one quiet plot to the narrator.

Nothing here calls a model. Each beat records the live threads it brought into play
(`BeatResolved.threads`: the KNOWN threads the extractor says the narration developed, plus any it
created or resolved) and the thread the engine named to the narrator (`paced_thread`). From the
branch's last beats the director picks the live thread that has gone longest without either, once
it has been quiet for `quiet_after` beats, and at most once every `every` beats; the narrator gets
one line beside the intent, which leaves the player's action first. Naming a thread counts as
touching it, so a thread the scene had no room for waits another `quiet_after` beats.

A pure function of the branch's recorded beats and threads: a fork, a replay or a dry run of the
same beat names the same thread. Which plots matter and how hard to push is the game's (`[pacing]`
in its world pack); the engine only says which plots went quiet (D-80's split, applied to plots).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from uro_core.domain.events import BeatResolvedPayload
from uro_core.ports.projections import EngineStore
from uro_core.timeline.models import ThreadView

QUIET_AFTER = 25  # beats a live thread may go without coming up before it is named
EVERY = 10  # at most one named thread in this many beats
LIVE = ("active", "offered")  # the threads the narrator is shown (dormant ones wait for a rule)


class PacingSettings(BaseModel):
    """`[pacing]` (D-95): when the engine names a quiet plot to the narrator."""

    model_config = ConfigDict(extra="forbid")  # a misspelt setting is an error, not a no-op
    quiet_after: int = Field(default=QUIET_AFTER, ge=0, le=200)  # 0 = never name one
    every: int = Field(default=EVERY, ge=1, le=200)

    @property
    def window(self) -> int:
        """How many of the branch's last beats the director reads: enough to rank threads quiet
        past `quiet_after`, and to see the last `every` beats."""
        return max(2 * self.quiet_after, self.every)


class ThreadPacing(BaseModel):
    thread_id: str
    stakes: str
    state: str
    provenance: str
    # Beats since the thread last came up or was named. With `at_least`, it didn't within the
    # beats read (the window): it has been quiet at least this long, or longer.
    quiet_beats: int
    at_least: bool = False


class PacingReport(BaseModel):
    settings: PacingSettings
    beats_read: int  # how many beats the counts are over (fewer than the window on a new branch)
    threads: list[ThreadPacing]
    # The thread the next beat would name to the narrator, "" if none (not quiet long enough, a
    # thread was named in the last `every` beats, or pacing is off).
    next_paced: str = ""


def pacing_line(stakes: str) -> str:
    """The narrator's line for a named thread. The wording passed the D-95 A/B; keep it as is."""
    return (
        f"A PLOT TO KEEP ALIVE: {stakes.rstrip('.')}. If it can fit the player's action, let this "
        "scene touch it: a reminder, a person, a clue or a consequence tied to it. Never at the "
        "cost of the player's action, and don't resolve it unless the action does."
    )


def settings_from(raw: dict[str, object]) -> PacingSettings:
    """A world's stored `[pacing]` dict → settings ({} → the defaults). Raises on a bad value."""
    return PacingSettings.model_validate(raw)


def thread_pacing(
    threads: list[ThreadView], recent: list[BeatResolvedPayload]
) -> list[ThreadPacing]:
    """How long each live thread has gone quiet, over `recent` (the branch's last beats, oldest
    first). A beat touches a thread it lists in `threads` or names as its `paced_thread`."""
    last: dict[str, int] = {}
    for i, beat in enumerate(recent):
        for tid in beat.threads:
            last[tid] = i
        if beat.paced_thread:
            last[beat.paced_thread] = i
    out = []
    for t in threads:
        if t.state not in LIVE:
            continue
        seen = t.thread_id in last
        out.append(
            ThreadPacing(
                thread_id=t.thread_id,
                stakes=t.stakes,
                state=t.state,
                provenance=t.provenance,
                quiet_beats=len(recent) - 1 - last[t.thread_id] if seen else len(recent),
                at_least=not seen,
            )
        )
    return out


def quiet_thread(
    threads: list[ThreadView], recent: list[BeatResolvedPayload], settings: PacingSettings
) -> ThreadView | None:
    """The live thread to name on the next beat, or None. `recent` is the branch's last
    `settings.window` beats, oldest first."""
    if settings.quiet_after <= 0 or len(recent) < settings.quiet_after:
        return None  # off, or too few beats played for anything to have gone quiet
    if any(b.paced_thread for b in recent[max(0, len(recent) - (settings.every - 1)) :]):
        return None  # a thread was named in the last `every` beats
    quiet = [p for p in thread_pacing(threads, recent) if p.quiet_beats >= settings.quiet_after]
    if not quiet:
        return None
    # Quiet longest first (unseen in the whole window counts as longest); among equals the first
    # in the store's order (by id), so the pick is deterministic.
    best = max(quiet, key=lambda p: (p.at_least, p.quiet_beats))
    return next(t for t in threads if t.thread_id == best.thread_id)


async def pacing_report(
    store: EngineStore, branch_id: str, *, settings: PacingSettings | None = None
) -> PacingReport:
    """What the director sees on a branch: each live thread's quiet beats and the thread the next
    beat would name (the CLI's `uro campaign pacing` and the server's `/pacing`, D-95). `settings`
    overrides the world's, as an Engine built with `pacing=` does."""
    if settings is None:
        settings = settings_from(await store.world_pacing(branch_id))
    threads = await store.list_threads(branch_id)
    recent = await store.recent_beats(branch_id, settings.window)
    paced = quiet_thread(threads, recent, settings)
    return PacingReport(
        settings=settings,
        beats_read=len(recent),
        threads=thread_pacing(threads, recent),
        next_paced=paced.thread_id if paced else "",
    )
