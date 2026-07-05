"""Commit hash chaining (docs/03): commit_hash = h(parent_hash, events)."""

from __future__ import annotations

import hashlib

from uro_core.domain.events import DomainEvent


def compute_commit_hash(parent_hash: str | None, events: list[DomainEvent]) -> str:
    """SHA-256 over the parent hash and the canonical JSON of each event.

    Chaining parent → child makes the timeline tamper-evident and export packs
    verifiable (docs/03, 07).
    """
    h = hashlib.sha256()
    h.update((parent_hash or "").encode("utf-8"))
    for event in events:
        h.update(event.model_dump_json().encode("utf-8"))
    return h.hexdigest()
