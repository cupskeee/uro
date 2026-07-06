"""VectorIndex port (docs/04, 07).

Semantic memory: vectors are stored once by content hash; per-branch membership
rows point at them (copied on fork, never re-embedded — later increment). The
Postgres store implements this over pgvector for Phase 1.
"""

from __future__ import annotations

from typing import Protocol

from uro_core.timeline.models import MemoryHit


class VectorIndex(Protocol):
    async def add_memory(
        self,
        *,
        branch_id: str,
        commit_id: str,
        kind: str,
        text: str,
        vector: list[float],
        entity_refs: list[str],
    ) -> None: ...

    async def search(self, branch_id: str, vector: list[float], k: int) -> list[MemoryHit]:
        """The k memories on the branch nearest the query vector (cosine)."""
        ...
