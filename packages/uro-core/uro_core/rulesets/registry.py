"""Ruleset registry — resolve a world pack's `ruleset = "id@version"` declaration to a bound
`Ruleset` (docs/06, D-30).

This lives in the ruleset PORT package, NOT the core ring: it is a COMPOSITION concern (it must
know the concrete built-ins), so the import-linter contract permits it to import `uro_basic`/
`uro_pbta` while the ring (`pipeline`/`domain`/…) still imports only `rulesets.base`. The
composition roots (CLI wiring, server deps) call `resolve`; the pipeline never touches it.

The PoC ships an explicit registry of the two built-ins. Real entry-point discovery
(`entry_points(group="uro.rulesets")`, so an external pip package can register its own ruleset)
is the documented extension seam — `register()` is the same hook such discovery would call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from uro_core.rulesets.base import Ruleset
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.rulesets.uro_pbta import UroPbtA

DEFAULT_RULESET_ID = "uro-basic"

# id → a zero-arg factory. A world pack selects one by id; a campaign records the resolved
# id + version so a later play/fork rebinds the SAME ruleset (docs/06).
_FACTORIES: dict[str, Callable[[], Ruleset]] = {
    "uro-basic": UroBasic,
    "uro-pbta": UroPbtA,
}


def register(ruleset_id: str, factory: Callable[[], Ruleset]) -> None:
    """Register a ruleset factory under an id (the seam an entry-point scan would use)."""
    _FACTORIES[ruleset_id] = factory


def available() -> list[str]:
    return sorted(_FACTORIES)


def resolve(
    ruleset_id: str = "", version: str = "", config: dict[str, Any] | None = None
) -> Ruleset:
    """Resolve a declared ruleset to a bound instance. An empty id → the default (uro-basic), so
    a world created without a pack still plays. An UNKNOWN id fails loudly (KeyError) rather than
    silently falling back — a campaign pinned to a ruleset the build no longer has is a real error.

    `version` is the campaign's pinned version; the PoC records it (so a fork rebinds the same
    ruleset) but does not hard-enforce compatibility — version gating is future work (docs/06).
    `config` is reserved for ruleset-specific knobs (the manifest's `[ruleset.config]`); the two
    built-ins take none yet.
    """
    rid = ruleset_id or DEFAULT_RULESET_ID
    factory = _FACTORIES.get(rid)
    if factory is None:
        raise KeyError(f"unknown ruleset {rid!r} (available: {available()})")
    return factory()
