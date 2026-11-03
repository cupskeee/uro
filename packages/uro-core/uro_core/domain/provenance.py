"""Provenance integrity for the two closed trust tiers (D-89).

Two causes have closed producers: `external` events come only from distilling a game's outcome
bundle (`_distill_core`, shared by the untrusted Chronicler route and the trusted embedder tier),
and `module` events come only from the rules gauntlet (`engines/rules_gauntlet.py`). So an event
stamped with either cause must be one those producers can make. This check runs where events are
committed (`EventStore._append`) and imported, so a closed-tier label is limited to the event
shapes its producer can make. It doesn't prove the event came through distillation or the
gauntlet: an `external` ActorDied an embedder commits itself passes, unchecked against the D-32
ceiling (which lives in distillation).

What it is not: an authority fence. An embedder holds root and can commit anything under its own
`system` cause; the D-32 protection ceiling stays enforced at distillation. The open causes
(`system`, `player_action`, `ruleset`, `history`, `agenda`) aren't checked.

The sets come from the producers' code, not from history: `tests/test_provenance.py` runs every
rule action kind through the gauntlet and every outcome shape through both distillers, so a new
action that emits another event type fails a test instead of silently dropping its beat.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from uro_core.domain.events import DomainEvent

# event type → the payload condition it must meet under that cause (None = any payload)
_EXTERNAL: dict[str, tuple[str, str] | None] = {
    "ActorDied": None,
    "ItemTransferred": None,
    "ClaimRecorded": ("truth", "unknown"),  # a feat or a downgraded casualty: testimony
}
_MODULE: dict[str, tuple[str, str] | None] = {
    "ClaimRecorded": ("truth", "unknown"),  # record_rumor: never canon
    "ClaimTruthChanged": ("truth", "false"),  # expire_claims retracts, nothing else
    "ThreadCreated": ("provenance", "module"),  # create_thread
    "ThreadStateChanged": None,
    "EdgeAdded": None,
    "EdgeRemoved": None,
    "CounterChanged": None,
    "BeliefChanged": None,  # spread_belief
}
CLOSED_TIERS: dict[str, dict[str, tuple[str, str] | None]] = {
    "external": _EXTERNAL,
    "module": _MODULE,
}


class ProvenanceError(ValueError):
    """An event carries a closed-tier cause its producer can't make."""


def provenance_problem(event_type: str, kind: str | None, payload: dict[str, Any]) -> str | None:
    """Why an event can't carry its cause, or None if it can (or the cause is an open one)."""
    allowed = CLOSED_TIERS.get(kind or "")
    if allowed is None:
        return None
    if event_type not in allowed:
        return f"a {event_type} can't be caused by {kind!r}: only {sorted(allowed)} can"
    rule = allowed[event_type]
    if rule is not None and payload.get(rule[0]) != rule[1]:
        return (
            f"a {kind!r} {event_type} must have {rule[0]}={rule[1]!r}, not {payload.get(rule[0])!r}"
        )
    return None


def check_provenance(events: Iterable[DomainEvent]) -> None:
    """Raise ProvenanceError on the first event whose closed-tier cause its producer can't make."""
    for event in events:
        problem = provenance_problem(event.event_type, event.caused_by.kind, event.payload)
        if problem is not None:
            raise ProvenanceError(problem)
