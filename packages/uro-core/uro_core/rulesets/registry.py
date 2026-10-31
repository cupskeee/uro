"""Ruleset registry — resolve a world pack's `ruleset = "id@version"` declaration to a bound
`Ruleset` (docs/06, D-30).

This lives in the ruleset PORT package, NOT the core ring: it is a COMPOSITION concern (it must
know the concrete built-ins), so the import-linter contract permits it to import `uro_basic`/
`uro_pbta`/`uro_pool` while the ring (`pipeline`/`domain`/…) still imports only `rulesets.base`. The
composition roots (CLI wiring, server deps) call `resolve`; the pipeline never touches it.

The PoC ships an explicit registry of the three built-ins. Real entry-point discovery
(`entry_points(group="uro.rulesets")`, so an external pip package can register its own ruleset)
is the documented extension seam — `register()` is the same hook such discovery would call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from uro_core.ports.projections import EngineStore
from uro_core.rulesets.base import Ruleset
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.rulesets.uro_pbta import UroPbtA
from uro_core.rulesets.uro_pool import UroPool
from uro_core.timeline.models import Campaign, RulesetPin

DEFAULT_RULESET_ID = "uro-basic"

# id → a factory taking the world's settings (`[ruleset.config]`, D-62). A world pack selects one
# by id; a campaign records the resolved id + version so a later play/fork rebinds the SAME ruleset.
Factory = Callable[[dict[str, Any]], Ruleset]
_FACTORIES: dict[str, Factory] = {
    "uro-basic": UroBasic,
    "uro-pbta": UroPbtA,
    "uro-pool": UroPool,
}


def register(ruleset_id: str, factory: Factory) -> None:
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
    `config` is the world's settings (`[ruleset.config]`, D-62). The ruleset validates them: an
    unknown or invalid key is a ValueError naming the ruleset, never silently ignored.
    """
    rid = ruleset_id or DEFAULT_RULESET_ID
    factory = _FACTORIES.get(rid)
    if factory is None:
        raise KeyError(f"unknown ruleset {rid!r} (available: {available()})")
    try:
        return factory(dict(config or {}))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(f"invalid [ruleset.config] for {rid}: {problems}") from exc


async def resolve_for_branch(store: EngineStore, branch_id: str) -> Ruleset:
    """The ruleset a branch's world declares, built with the world's settings."""
    pin = await store.world_ruleset(branch_id)
    return resolve(pin.id, pin.version, pin.config)


async def campaign_pin(store: EngineStore, campaign: Campaign) -> RulesetPin:
    """What a campaign plays: its pinned id + version, with its WORLD's settings. (A legacy campaign
    pinned to a different ruleset than its world declares gets that ruleset's defaults.)"""
    world = await store.world_ruleset(campaign.branch_id)
    rid = campaign.ruleset_id or world.id or DEFAULT_RULESET_ID
    same = rid == (world.id or DEFAULT_RULESET_ID)
    return RulesetPin(
        id=rid,
        version=campaign.ruleset_version or world.version,
        config=world.config if same else {},
    )


async def resolve_for_campaign(store: EngineStore, campaign: Campaign) -> Ruleset:
    """The ruleset a campaign plays, built with its world's settings."""
    pin = await campaign_pin(store, campaign)
    return resolve(pin.id, pin.version, pin.config)
