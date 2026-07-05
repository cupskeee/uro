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
