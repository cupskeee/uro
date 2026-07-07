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
