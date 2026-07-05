"""ULID identifiers. Every entity/event/commit id is a ULID string (docs/02, 14)."""

from ulid import ULID


def new_id() -> str:
    """A fresh, lexicographically-sortable ULID as a 26-char string."""
    return str(ULID())
