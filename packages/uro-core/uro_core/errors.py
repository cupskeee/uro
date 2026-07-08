"""Engine error types.

Kept minimal in Phase 0; the pipeline's full failure taxonomy is docs/13.
"""

from __future__ import annotations


class UroError(Exception):
    """Base for all engine errors."""


class ProviderError(UroError):
    """An LLM provider failed — transport error, non-200, or an in-band error frame."""


class EmptyNarrationError(UroError):
    """A beat produced no narration; it must not enter the append-only log."""


class PlannerError(UroError):
    """The planner could not produce a valid plan after re-asks; the beat is unrunnable
    (docs/13: a beat without a plan fails, retryable as a fresh beat)."""


class UnboundParticipantError(UroError):
    """A participant with no PC tried to take a beat in a PARTY campaign (OQ-7/D-31). In solo the
    beat falls back to the one PC; in a party it must NOT silently drive another player's PC — the
    participant must `uro campaign join` first. Fails the beat cleanly instead of misattributing."""


class PackError(UroError):
    """A world pack is malformed — bad TOML/YAML, a missing manifest, or a schema violation
    (docs/09). Raised by the parser with a message an author can act on."""


class ExportError(UroError):
    """An export bundle failed hash-chain verification — a commit's recomputed hash does not
    match the bundle's stored hash, i.e. the bundle was altered in transit (docs/03, 07)."""
