"""Deterministic stub provider — the test/dev double (docs/14).

No network, no randomness: the same request always yields the same narration, so
integration tests assert on real engine behavior (persistence, resume) without a
live model. Its prose is intentionally flat; coherence is a real-LLM concern.

`hashing_embedding` is a deterministic bag-of-words vectorizer: texts that share
words get real (nonzero) cosine similarity, so semantic recall can be tested
offline — a query about "the Duke" ranks a memory mentioning the Duke first.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import AsyncIterator

from uro_core.providers.base import CompletionRequest

_EMBED_DIM = 256


def hashing_embedding(text: str, dim: int = _EMBED_DIM) -> list[float]:
    """L2-normalized hashing bag-of-words vector (deterministic across runs)."""
    vec = [0.0] * dim
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % dim
        vec[bucket] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


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

    async def complete(self, req: CompletionRequest) -> str:
        # The stub extracts nothing — `uro play --provider stub` narrates without
        # building state, so the offline dev loop still works end-to-end.
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]
