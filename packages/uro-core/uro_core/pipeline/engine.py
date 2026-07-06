"""Phase 1 beat pipeline (docs/05, 10).

context (recency + structured recall) → narrate → extract → gauntlet → commit. The
planner and mechanics stages are still ahead (Phase 3, D-28); the epistemic loop is
here: recall feeds established facts to the narrator so characters can contradict
lies, and the extractor turns the resulting prose into committed state through the
validation gauntlet.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator

from pydantic import BaseModel

from uro_core.domain.events import DomainEvent, beat_resolved
from uro_core.domain.ids import new_id
from uro_core.errors import EmptyNarrationError, ProviderError
from uro_core.metering import LLMCall
from uro_core.pipeline.extraction import (
    build_extractor_messages,
    parse_extraction,
    run_gauntlet,
)
from uro_core.pipeline.recall import RecallBundle, assemble_recall, build_narrator_messages
from uro_core.ports.projections import EngineStore
from uro_core.providers.base import Message
from uro_core.providers.router import ProviderRouter
from uro_core.timeline.models import Campaign


class BeatResult(BaseModel):
    beat_id: str
    narration: str
    commit_id: str
    extracted: int = 0  # number of state events canonicalized from the prose


def _hash_messages(messages: list[Message]) -> str:
    payload = json.dumps([m.model_dump() for m in messages], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Engine:
    """Embeddable engine entry point. Wired with concrete adapters by the CLI/server."""

    def __init__(
        self,
        store: EngineStore,
        router: ProviderRouter,
        *,
        recency: int = 8,
        semantic_k: int = 4,
    ) -> None:
        self._store = store
        self._router = router
        self._recency = recency
        self._semantic_k = semantic_k

    async def _recall(self, branch_id: str, intent_text: str) -> RecallBundle:
        """Structured recall + semantic recall of older beats (docs/04)."""
        recall = await assemble_recall(self._store, branch_id, intent_text, self._recency)
        recent_texts = {b.narration for b in recall.recent_beats}
        started = time.perf_counter()
        try:
            vectors = await self._router.embed("embedder", [intent_text])
        except ProviderError:
            return recall  # semantic recall is best-effort aux; structured recall stands
        await self._meter("embedder", [Message(role="user", content=intent_text)], started)
        hits = await self._store.search(branch_id, vectors[0], self._semantic_k)
        # Drop memories already in the recency window — semantic recall is for OLD beats.
        recall.memories = [h.text for h in hits if h.text not in recent_texts]
        return recall

    async def run_beat(
        self, campaign: Campaign, participant_id: str, intent_text: str
    ) -> BeatResult:
        """Resolve one beat and commit it (narration + extracted state)."""
        recall = await self._recall(campaign.branch_id, intent_text)
        messages = build_narrator_messages(recall, intent_text)
        started = time.perf_counter()
        chunks = [chunk async for chunk in self._router.stream("narrator", messages)]
        await self._meter("narrator", messages, started)
        narration = "".join(chunks).strip()
        return await self._finish(campaign, participant_id, intent_text, narration, recall)

    async def run_beat_stream(
        self, campaign: Campaign, participant_id: str, intent_text: str
    ) -> AsyncIterator[str]:
        """Stream narration to the caller, then extract + commit once the stream ends.

        A beat commits only after the stream completes. If the consumer stops early
        (e.g. Ctrl-C mid-stream) the commit is intentionally skipped: nothing partial
        enters the append-only log, so a resumed session simply never saw that beat.
        """
        recall = await self._recall(campaign.branch_id, intent_text)
        messages = build_narrator_messages(recall, intent_text)
        started = time.perf_counter()
        collected: list[str] = []
        async for chunk in self._router.stream("narrator", messages):
            collected.append(chunk)
            yield chunk
        await self._meter("narrator", messages, started)
        await self._finish(
            campaign, participant_id, intent_text, "".join(collected).strip(), recall
        )

    async def _finish(
        self,
        campaign: Campaign,
        participant_id: str,
        intent_text: str,
        narration: str,
        recall: RecallBundle,
    ) -> BeatResult:
        if not narration:
            raise EmptyNarrationError(
                f"provider produced no narration for a beat by {participant_id}"
            )
        extracted = await self._extract(campaign.branch_id, recall, narration)
        beat_id = new_id()
        events: list[DomainEvent] = [
            beat_resolved(
                beat_id=beat_id,
                participant_id=participant_id,
                intent_text=intent_text,
                narration=narration,
            ),
            *extracted,
        ]
        commit = await self._store.append_beat(campaign.branch_id, events)
        await self._remember(
            campaign.branch_id, commit.commit_id, narration, [a.actor_id for a in recall.actors]
        )
        return BeatResult(
            beat_id=beat_id,
            narration=narration,
            commit_id=commit.commit_id,
            extracted=len(extracted),
        )

    async def _remember(
        self, branch_id: str, commit_id: str, text: str, entity_refs: list[str]
    ) -> None:
        """Embed the beat's narration and index it for later semantic recall.

        Post-commit and best-effort: the memory index is a rebuildable aux cache, so
        an embedding failure never rolls back or fails a committed beat.
        """
        started = time.perf_counter()
        try:
            vectors = await self._router.embed("embedder", [text])
        except ProviderError:
            return
        await self._meter("embedder", [Message(role="user", content=text)], started)
        await self._store.add_memory(
            branch_id=branch_id,
            commit_id=commit_id,
            kind="beat",
            text=text,
            vector=vectors[0],
            entity_refs=entity_refs,
        )

    async def _extract(
        self, branch_id: str, recall: RecallBundle, narration: str
    ) -> list[DomainEvent]:
        """Extract state from prose through the gauntlet. Failure → narration-only beat
        (docs/13: state integrity is never sacrificed to keep prose, and prose is never
        lost to keep state)."""
        messages = build_extractor_messages(recall, narration)
        started = time.perf_counter()
        raw: str | None = None
        try:
            raw = await self._router.complete(
                "extractor", messages, json_mode=True, temperature=0.1
            )
        except ProviderError:
            raw = None
        await self._meter("extractor", messages, started)  # meter even a failed call
        if raw is None:
            return []
        extraction = parse_extraction(raw)
        if extraction is None:
            return []
        return await run_gauntlet(self._store, branch_id, extraction)

    async def _meter(self, stage_tag: str, messages: list[Message], started: float) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        await self._store.record_llm_call(
            LLMCall(
                stage_tag=stage_tag, prompt_hash=_hash_messages(messages), latency_ms=latency_ms
            )
        )
