"""Ruleset registry — resolve a world's declared ruleset to a bound `Ruleset` (docs/06, D-30).

This lives in the ruleset PORT package, NOT the core ring: it is a COMPOSITION concern (it must
know the concrete built-ins), so the import-linter contract permits it to import `uro_basic`/
`uro_pbta`/`uro_pool` while the ring (`pipeline`/`domain`/…) still imports only `rulesets.base`. The
composition roots (CLI wiring, server deps) call `resolve`; the pipeline never touches it.

Rulesets come from three places: the three built-ins, PLUGINS — installed packages that declare an
entry point in the `uro.rulesets` group (D-65) — and `register()` for an embedder's in-process one.
An entry point's NAME is the ruleset id, and it points at a factory taking the world's settings
(a ruleset class whose `__init__(config)` validates them works). Discovery runs once, lazily. A
plugin that fails to load, or doesn't meet the port's contract, is reported with its error by
`broken()` and refused by `resolve()`, never silently dropped; it can't replace a built-in.
Installing a plugin is trusting its code, like any package — world packs still never ship code
(D-33).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

from pydantic import ValidationError

from uro_core.ports.projections import EngineStore
from uro_core.rulesets.base import Ruleset, ruleset_contract_problem
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.rulesets.uro_pbta import UroPbtA
from uro_core.rulesets.uro_pool import UroPool
from uro_core.timeline.models import Campaign, RulesetPin

logger = logging.getLogger(__name__)

DEFAULT_RULESET_ID = "uro-basic"
ENTRY_POINT_GROUP = "uro.rulesets"

# id → a factory taking the world's settings (`[ruleset.config]`, D-62). A world pack selects one
# by id; a campaign records the resolved id + version so a later play/fork rebinds the SAME ruleset.
Factory = Callable[[dict[str, Any]], Ruleset]
_BUILTINS: dict[str, Factory] = {
    "uro-basic": UroBasic,
    "uro-pbta": UroPbtA,
    "uro-pool": UroPool,
}
_FACTORIES: dict[str, Factory] = dict(_BUILTINS)
_BROKEN: dict[str, str] = {}  # plugin id → why it can't be used
_discovered = False


def _discover() -> None:
    """Load the installed `uro.rulesets` plugins, once. A plugin whose factory is a CLASS is checked
    on the class (no instance built, so a plugin with a required setting is fine); any other
    factory is built once with empty settings to check it."""
    global _discovered
    if _discovered:
        return
    _discovered = True
    claims: dict[str, list[str]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        claims.setdefault(ep.name, []).append(ep.value)
        if ep.name in _BUILTINS:
            # keyed apart from the id, so the (working) built-in isn't reported unusable
            _BROKEN[f"{ep.name} [{ep.value}]"] = f"can't replace the built-in {ep.name!r}"
            continue
        if len(claims[ep.name]) > 1:  # two plugins claim one id: neither wins by install order
            _FACTORIES.pop(ep.name, None)
            _BROKEN[ep.name] = f"claimed by more than one plugin: {', '.join(claims[ep.name])}"
            continue
        try:
            factory = ep.load()
            subject = factory if isinstance(factory, type) else factory({})
            problem = ruleset_contract_problem(subject, ep.name)
        except Exception as exc:  # a plugin's own fault: report it, don't take the engine down
            # only the exception TYPE leaves the process (it reaches GET /rulesets and validate);
            # the detail goes to the log
            logger.warning("ruleset plugin %r failed to load", ep.name, exc_info=exc)
            problem = f"{ep.value} failed to load ({type(exc).__name__}; see the server log)"
        if problem is None:
            _FACTORIES[ep.name] = factory
        else:
            _BROKEN[ep.name] = problem
            logger.warning("ruleset plugin %r can't be used: %s", ep.name, problem)


def register(ruleset_id: str, factory: Factory) -> None:
    """Register a ruleset factory under an id — an embedder's in-process ruleset."""
    _discover()
    _FACTORIES[ruleset_id] = factory
    _BROKEN.pop(ruleset_id, None)


def broken() -> dict[str, str]:
    """Installed plugins that can't be used, with the reason."""
    _discover()
    return dict(_BROKEN)


def available() -> list[str]:
    _discover()
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
    _discover()
    rid = ruleset_id or DEFAULT_RULESET_ID
    factory = _FACTORIES.get(rid)
    if factory is None and rid in _BROKEN:  # (a plugin claiming a built-in's id leaves it intact)
        raise ValueError(f"ruleset {rid!r} is installed but can't be used: {_BROKEN[rid]}")
    if factory is None:
        raise KeyError(f"unknown ruleset {rid!r} (available: {available()})")
    try:
        ruleset = factory(dict(config or {}))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(f"invalid [ruleset.config] for {rid}: {problems}") from exc
    except Exception as exc:  # a plugin's or an embedder's own fault (e.g. a pre-D-62 factory)
        # only the exception TYPE leaves the process — this message reaches validate endpoints and
        # listings (an info-exposure path otherwise, CodeQL py/stack-trace-exposure); detail → log
        logger.warning("ruleset %r failed to build", rid, exc_info=exc)
        raise ValueError(
            f"ruleset {rid!r} failed to build ({type(exc).__name__}; see the server log)"
        ) from exc
    problem = ruleset_contract_problem(ruleset, rid)  # every built ruleset, register()'s too
    if problem is not None:
        raise ValueError(f"ruleset {rid!r} can't be used: {problem}")
    return ruleset


async def resolve_for_branch(store: EngineStore, branch_id: str) -> Ruleset:
    """The ruleset a branch's world declares, built with the world's settings."""
    pin = await store.world_ruleset(branch_id)
    return resolve(pin.id, pin.version, pin.config)


async def campaign_pin(store: EngineStore, campaign: Campaign) -> RulesetPin:
    """What a campaign plays: its pinned id + version, with its WORLD's settings. (A legacy campaign
    pinned to a different ruleset than its world declares gets that ruleset's defaults.) The id is
    "" when neither the campaign nor its world names one — `resolve` then picks the default, and a
    server can apply its own default (`uro serve --ruleset`)."""
    world = await store.world_ruleset(campaign.branch_id)
    rid = campaign.ruleset_id or world.id
    same = (rid or DEFAULT_RULESET_ID) == (world.id or DEFAULT_RULESET_ID)
    return RulesetPin(
        id=rid,
        version=campaign.ruleset_version or world.version,
        config=world.config if same else {},
    )


async def resolve_for_campaign(store: EngineStore, campaign: Campaign) -> Ruleset:
    """The ruleset a campaign plays, built with its world's settings."""
    pin = await campaign_pin(store, campaign)
    return resolve(pin.id, pin.version, pin.config)
