"""The pacing director (D-95; OQ-2's director half): name one quiet plot to the narrator.

Nothing here calls a model. Each beat records the live threads it brought into play
(`BeatResolved.threads`: the KNOWN threads the extractor says the narration developed, plus any it
created or resolved) and the thread the engine named to the narrator (`paced_thread`). From the
branch's last beats the director picks the live thread that has gone longest without either, once
it has been quiet for `quiet_after` beats, and at most once every `every` beats; the narrator gets
one line beside the intent, which leaves the player's action first. Naming a thread counts as
touching it, so a thread the scene had no room for waits another `quiet_after` beats.

A pure function of the branch's recorded beats and threads: a fork, a replay or a dry run of the
same beat names the same thread. The engine always records and reports which plots went quiet (the
signal: `BeatResolved.threads`, `pacing_report`); naming one to the narrator is a narrative policy,
so it is OFF unless the game turns it on (`[pacing] quiet_after` in its world pack, or
`Engine(pacing=)`), and its wording is the world's prompt-pack template `pacing.j2` (D-96).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from uro_core.domain.events import BeatResolvedPayload
from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.ports.projections import EngineStore
from uro_core.timeline.models import ThreadView

logger = logging.getLogger(__name__)

# The measured setting (D-95: the A/B's quiet plots had been quiet 25+ beats) — what a game sets to
# opt in. The default is off (D-96): naming plots steers the story, which is the game's call.
SUGGESTED_QUIET_AFTER = 25
EVERY = 10  # at most one named thread in this many beats
LIVE = ("active", "offered")  # the threads the narrator is shown (dormant ones wait for a rule)


class PacingSettings(BaseModel):
    """`[pacing]` (D-95): when the engine names a quiet plot to the narrator."""

    model_config = ConfigDict(extra="forbid")  # a misspelt setting is an error, not a no-op
    quiet_after: int = Field(default=0, ge=0, le=200)  # 0 = never name one (the default, D-96)
    every: int = Field(default=EVERY, ge=1, le=200)

    @property
    def window(self) -> int:
        """How many of the branch's last beats the director reads: enough to rank threads quiet
        past `quiet_after` and to see the last `every` beats. Off, the report still reads as many
        as the suggested setting would (the signal is the game's whether or not it opts in)."""
        return max(2 * (self.quiet_after or SUGGESTED_QUIET_AFTER), self.every)


class ThreadPacing(BaseModel):
    thread_id: str
    stakes: str
    state: str
    provenance: str
    developments: int = 0  # D-102: beats that brought it into play
    beats_live: int = 0  # D-102: beats it has been active or offered
    # Beats since the thread last came up or was named. With `at_least`, it didn't within the
    # beats read (the window), so this is the window's length: a thread quiet longer than that, or
    # one that became live outside a beat (a rule woke it, an author's commit added it). Either way
    # the narration hasn't shown it, and the next eligible beat names it first: a woken plot reaches
    # the story (D-95 review, pinned in test_a_thread_a_rule_wakes_is_named_next).
    quiet_beats: int
    at_least: bool = False


class PacingReport(BaseModel):
    settings: PacingSettings
    beats_read: int  # how many beats the counts are over (fewer than the window on a new branch)
    threads: list[ThreadPacing]
    # The thread the next beat would name to the narrator, "" if none (not quiet long enough, a
    # thread was named in the last `every` beats, or pacing is off).
    next_paced: str = ""


def pacing_line(stakes: str, env: PromptEnv | None = None) -> str:
    """The narrator's line for a named thread: the world's `pacing.j2` (a prompt pack may reword
    or translate it, D-96), rendered with `stakes` (its trailing period dropped). The default
    template is the wording that passed the D-95 A/B."""
    return (env or DEFAULT_ENV).render("pacing.j2", stakes=stakes.rstrip("."))


def settings_from(raw: dict[str, object]) -> PacingSettings:
    """A world's stored `[pacing]` dict → settings ({} → the defaults). Raises on a bad value."""
    return PacingSettings.model_validate(raw)


async def world_settings(store: object, branch_id: str) -> PacingSettings:
    """A branch's world's `[pacing]` as settings. A store from before D-95 (no `world_pacing`)
    gets the defaults; a stored setting that fails validation (only reachable by writing the event
    log directly: world creation and import both check it) turns the director off, logged."""
    read = getattr(store, "world_pacing", None)
    raw = await read(branch_id) if read is not None else {}
    try:
        return settings_from(raw)
    except ValidationError as exc:
        logger.warning("pacing: the world's [pacing] is invalid, so it is off: %s", exc)
        return PacingSettings(quiet_after=0)


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
                developments=t.developments,
                beats_live=t.beats_live,
                quiet_beats=len(recent) - 1 - last[t.thread_id] if seen else len(recent),
                at_least=not seen,
            )
        )
    return out


def quiet_thread(
    threads: list[ThreadView], recent: list[BeatResolvedPayload], settings: PacingSettings
) -> ThreadView | None:
    """The quiet live thread to name on the next beat, or None (D-95). `recent` is the
    branch's last `settings.window` beats, oldest first."""
    if settings.quiet_after <= 0 or len(recent) < settings.quiet_after:
        return None  # off, or too few beats played for anything to have gone quiet
    if _named_lately(recent, settings):
        return None
    quiet = [p for p in thread_pacing(threads, recent) if p.quiet_beats >= settings.quiet_after]
    if not quiet:
        return None
    # Quiet longest first (unseen in the whole window counts as longest); among equals the first
    # in the store's order (by id), so the pick is deterministic.
    best = max(quiet, key=lambda p: (p.at_least, p.quiet_beats))
    return next(t for t in threads if t.thread_id == best.thread_id)


def _named_lately(recent: list[BeatResolvedPayload], settings: PacingSettings) -> bool:
    """A thread was named in the last `every` beats (the spacing)."""
    return any(b.paced_thread for b in recent[max(0, len(recent) - (settings.every - 1)) :])


async def pacing_report(
    store: EngineStore, branch_id: str, *, settings: PacingSettings | None = None
) -> PacingReport:
    """What the director sees on a branch: each live thread's quiet beats and the thread the next
    beat would name (the CLI's `uro campaign pacing` and the server's `/pacing`, D-95). `settings`
    overrides the world's, as an Engine built with `pacing=` does."""
    if settings is None:
        settings = await world_settings(store, branch_id)
    threads = await store.list_threads(branch_id)
    recent = await store.recent_beats(branch_id, settings.window)
    paced = quiet_thread(threads, recent, settings)
    return PacingReport(
        settings=settings,
        beats_read=len(recent),
        threads=thread_pacing(threads, recent),
        next_paced=paced.thread_id if paced else "",
    )
