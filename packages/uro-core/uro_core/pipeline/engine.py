"""Phase 0 degenerate beat pipeline (docs/05, 10).

context (recency only) → narrate → commit raw beat log. No planner, no extraction,
no mechanics — those arrive in Phase 1+. The point of this slice is to prove the
shape end-to-end: a beat reads prior beats *from the event log* and appends one
`BeatResolved` commit, so a resumed session continues from Postgres, not memory.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator

from pydantic import BaseModel

from uro_core.domain.events import beat_resolved
from uro_core.domain.ids import new_id
from uro_core.errors import EmptyNarrationError
from uro_core.metering import LLMCall
from uro_core.ports.event_store import EventStore
from uro_core.providers.base import Message
from uro_core.providers.router import ProviderRouter
from uro_core.timeline.models import Campaign

_SYSTEM_PROMPT = (
    "You are the narrator of a text RPG set in a tavern. Continue the scene in two to "
    "four sentences of vivid second-person prose. Never speak or decide for the player; "
    "narrate only what they perceive and how the world responds."
)


class BeatResult(BaseModel):
    beat_id: str
    narration: str
    commit_id: str


def _hash_messages(messages: list[Message]) -> str:
    payload = json.dumps([m.model_dump() for m in messages], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Engine:
    """Embeddable engine entry point. Wired with concrete adapters by the CLI/server."""

    def __init__(self, store: EventStore, router: ProviderRouter, *, recency: int = 8) -> None:
        self._store = store
        self._router = router
        self._recency = recency

    async def _build_messages(self, branch_id: str, intent_text: str) -> list[Message]:
        history = await self._store.recent_beats(branch_id, self._recency)
        messages = [Message(role="system", content=_SYSTEM_PROMPT)]
        for beat in history:
            # Defensive: never re-emit an empty turn (a past bad row must not wedge
            # the reconstructed prompt against strict providers).
            if not beat.intent_text or not beat.narration:
                continue
            messages.append(Message(role="user", content=beat.intent_text))
            messages.append(Message(role="assistant", content=beat.narration))
        messages.append(Message(role="user", content=intent_text))
        return messages

    async def run_beat(
        self, campaign: Campaign, participant_id: str, intent_text: str
    ) -> BeatResult:
        """Resolve one beat and commit it. Returns the full narration."""
        messages = await self._build_messages(campaign.branch_id, intent_text)
        started = time.perf_counter()
        chunks = [chunk async for chunk in self._router.stream("narrator", messages)]
        await self._meter("narrator", messages, started)
        return await self._commit(campaign, participant_id, intent_text, "".join(chunks).strip())

    async def run_beat_stream(
        self, campaign: Campaign, participant_id: str, intent_text: str
    ) -> AsyncIterator[str]:
        """Stream narration chunks to the caller, then commit once the stream ends.

        A beat commits only after the stream completes. If the consumer stops early
        (e.g. Ctrl-C mid-stream) the commit is intentionally skipped: nothing partial
        enters the append-only log, so a resumed session simply never saw that beat.
        """
        messages = await self._build_messages(campaign.branch_id, intent_text)
        started = time.perf_counter()
        collected: list[str] = []
        async for chunk in self._router.stream("narrator", messages):
            collected.append(chunk)
            yield chunk
        await self._meter("narrator", messages, started)
        await self._commit(campaign, participant_id, intent_text, "".join(collected).strip())

    async def _meter(self, stage_tag: str, messages: list[Message], started: float) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        await self._store.record_llm_call(
            LLMCall(
                stage_tag=stage_tag, prompt_hash=_hash_messages(messages), latency_ms=latency_ms
            )
        )

    async def _commit(
        self, campaign: Campaign, participant_id: str, intent_text: str, narration: str
    ) -> BeatResult:
        if not narration:
            # An empty completion must never become a permanent no-op beat that
            # poisons the resume prompt. Surface it; the caller can retry.
            raise EmptyNarrationError(
                f"provider produced no narration for a beat by {participant_id}"
            )
        beat_id = new_id()
        event = beat_resolved(
            beat_id=beat_id,
            participant_id=participant_id,
            intent_text=intent_text,
            narration=narration,
        )
        commit = await self._store.append_beat(campaign.branch_id, [event])
        return BeatResult(beat_id=beat_id, narration=narration, commit_id=commit.commit_id)
