"""Phase 0 degenerate beat pipeline (docs/05, 10).

context (recency only) → narrate → commit raw beat log. No planner, no extraction,
no mechanics — those arrive in Phase 1+. The point of this slice is to prove the
shape end-to-end: a beat reads prior beats *from the event log* and appends one
`BeatResolved` commit, so a resumed session continues from Postgres, not memory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import BaseModel

from uro_core.domain.events import beat_resolved
from uro_core.domain.ids import new_id
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
            messages.append(Message(role="user", content=beat.intent_text))
            messages.append(Message(role="assistant", content=beat.narration))
        messages.append(Message(role="user", content=intent_text))
        return messages

    async def run_beat(
        self, campaign: Campaign, participant_id: str, intent_text: str
    ) -> BeatResult:
        """Resolve one beat and commit it. Returns the full narration."""
        messages = await self._build_messages(campaign.branch_id, intent_text)
        chunks = [chunk async for chunk in self._router.stream("narrator", messages)]
        return await self._commit(campaign, participant_id, intent_text, "".join(chunks).strip())

    async def run_beat_stream(
        self, campaign: Campaign, participant_id: str, intent_text: str
    ) -> AsyncIterator[str]:
        """Stream narration chunks to the caller, then commit once the stream ends."""
        messages = await self._build_messages(campaign.branch_id, intent_text)
        collected: list[str] = []
        async for chunk in self._router.stream("narrator", messages):
            collected.append(chunk)
            yield chunk
        await self._commit(campaign, participant_id, intent_text, "".join(collected).strip())

    async def _commit(
        self, campaign: Campaign, participant_id: str, intent_text: str, narration: str
    ) -> BeatResult:
        beat_id = new_id()
        event = beat_resolved(
            beat_id=beat_id,
            participant_id=participant_id,
            intent_text=intent_text,
            narration=narration,
        )
        commit = await self._store.append_beat(campaign.branch_id, [event])
        return BeatResult(beat_id=beat_id, narration=narration, commit_id=commit.commit_id)
