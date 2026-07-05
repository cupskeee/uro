"""Deterministic stub provider — the test/dev double (docs/14).

No network, no randomness: the same request always yields the same narration, so
integration tests assert on real engine behavior (persistence, resume) without a
live model. Its prose is intentionally flat; coherence is a real-LLM concern.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from uro_core.providers.base import CompletionRequest


class StubProvider:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        last_user = next(
            (m.content for m in reversed(req.messages) if m.role == "user"),
            "",
        )
        prior = sum(1 for m in req.messages if m.role == "assistant")
        sentences = [
            "[stub] The Rusty Tankard's fire pops and settles.",
            f'In answer to "{last_user}", the barkeep wipes a mug and grunts.',
            f"(This is beat {prior + 1} on record.)",
        ]
        for sentence in sentences:
            yield sentence + " "
