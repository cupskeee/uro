"""Create a world from a pack, and seed its history (docs/09) — the one path behind
`uro world create|seed` and `POST /worlds/create`, `POST /worlds/{w}/seed`, so the CLI and the
server can't drift on what an import checks or commits.
"""

from __future__ import annotations

from pydantic import BaseModel

from uro_core.engines.history import seed_history
from uro_core.errors import PackError
from uro_core.ports.projections import EngineStore
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets import registry
from uro_core.rulesets.rng import Rng
from uro_core.timeline.models import World
from uro_core.worldpack.backfill import backfill_gaps
from uro_core.worldpack.importer import pack_to_events
from uro_core.worldpack.models import WorldPack
from uro_core.worldpack.sufficiency import check_sufficiency


class PackImport(BaseModel):
    world: World
    grade: str  # the sufficiency grade the world was created at (after any backfill)
    backfilled: list[str]  # what AI backfill added and committed (tagged ai_backfill)


class SeedResult(BaseModel):
    commit_id: str
    dynasties: int
    wars: int


def ruleset_problem(pack: WorldPack) -> str | None:
    """Why this build can't play the pack's `[ruleset]` — an unknown id, or settings the ruleset
    refuses (D-62) — or None. Shared by validate and import, so both judge a pack the same way."""
    decl = pack.manifest.ruleset
    try:
        registry.resolve(decl.id, decl.version, decl.config)
    except KeyError as exc:
        return str(exc.args[0])
    except ValueError as exc:
        return str(exc)
    return None


async def import_pack(
    store: EngineStore, pack: WorldPack, *, backfill_router: ProviderRouter | None = None
) -> PackImport:
    """Commit a parsed pack as a new world. Refuses (PackError) a pack whose ruleset this build
    can't play (an unknown id, or bad `[ruleset.config]` settings) — else the world commits fine
    and only fails at the first campaign — or one graded insufficient. With `backfill_router`, a
    thin pack's gaps are AI-filled first and the fills are committed, tagged
    `provenance=ai_backfill` (a live LLM call)."""
    problem = ruleset_problem(pack)
    if problem is not None:
        raise PackError(f"pack declares a ruleset this build can't play: {problem}")
    report = check_sufficiency(pack)
    added: list[str] = []
    if backfill_router is not None and report.grade != "runnable":
        pack, added = await backfill_gaps(pack, backfill_router, report=report)
        report = check_sufficiency(pack)
    if report.grade == "insufficient":
        raise PackError(f"pack is INSUFFICIENT to run: {'; '.join(report.gaps)}")
    world = await store.create_world(
        pack.manifest.name,
        tone=pack.manifest.tone,
        prompt_overrides=pack.prompts,
        ruleset_id=pack.manifest.ruleset.id,
        ruleset_version=pack.manifest.ruleset.version,
        ruleset_config=pack.manifest.ruleset.config,
        rule_pack=pack.rule_pack.model_dump() if pack.rule_pack else {},
        extra_events=pack_to_events(pack),
    )
    return PackImport(world=world, grade=report.grade, backfilled=added)


async def seed_world(store: EngineStore, world: World, pack: WorldPack, seed: int) -> SeedResult:
    """Layer seed-dependent dynasties and wars (History, emitter H) onto a world's main branch.
    The pack must be the one the world was created from: the world doesn't persist the manifest's
    `[history]`, so the pack is re-supplied — and a different pack would seed another world's
    history here. Same pack + a different seed → a different history on the same geography."""
    if pack.manifest.name != world.name:
        raise PackError(f"this pack is {pack.manifest.name!r}, but the world is {world.name!r}")
    events = seed_history(pack.manifest, Rng(seed))
    commit = await store.append_beat(world.main_branch_id, events)
    wars = [e for e in events if e.event_type == "EdgeAdded"]
    return SeedResult(
        commit_id=commit.commit_id,
        dynasties=sum(1 for e in events if e.event_type == "FactionCreated"),
        wars=sum(1 for e in wars if e.payload.get("rel_type") == "at_war_with"),
    )
