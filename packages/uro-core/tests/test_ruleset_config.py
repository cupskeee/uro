"""Per-game ruleset settings (D-62): a world's `[ruleset.config]` is validated, persisted on its
WorldGenesis, and reaches every ruleset built for its campaigns — CLI and server alike.

Before D-62 a pack could write `[ruleset.config]` and it was parsed, then silently dropped.
"""

from pathlib import Path

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.errors import PackError
from uro_core.rulesets import registry
from uro_core.rulesets.base import Award, CharSpec, CheckRequest
from uro_core.rulesets.rng import Rng
from uro_core.timeline.models import Campaign

WORLDS = Path(__file__).resolve().parents[3] / "worlds"


class _Dice(Rng):
    def __init__(self, *faces: int) -> None:
        super().__init__(0)
        self._faces = list(faces)

    def die(self, sides: int) -> int:
        return self._faces.pop(0)

    def roll(self, n: int, sides: int) -> int:
        return self._faces.pop(0)


def test_defaults_are_the_rules_as_written() -> None:
    assert registry.resolve("uro-pool").config.model_dump() == {  # type: ignore[attr-defined]
        "push_strain": 1,
        "strain_limit": 5,
        "triumph_marks": 3,
        "milestones_per_advance": 3,
        "trait_cap": 5,
    }
    for rid in registry.available():
        assert registry.resolve(rid).config_schema()["properties"]  # every built-in has settings


def test_unknown_or_invalid_settings_are_refused_with_the_ruleset_named() -> None:
    with pytest.raises(ValueError, match=r"uro-pool.*strain_limt"):
        registry.resolve("uro-pool", "", {"strain_limt": 3})  # a typo, never silently ignored
    with pytest.raises(ValueError, match="uro-pbta"):
        registry.resolve("uro-pbta", "", {"xp_per_advance": 0})


def test_settings_change_the_rules() -> None:
    pool = registry.resolve("uro-pool", "", {"push_strain": 2})
    sheet = pool.new_character(CharSpec(data={"traits": {"nerve": 1}}), Rng(0))
    pushed = pool.resolve_check(CheckRequest(sheet=sheet, stat="nerve"), _Dice(1, 1))
    assert pushed.sheet is not None and pushed.sheet["strain"] == 2
    no_push = registry.resolve("uro-pool", "", {"strain_limit": 0})
    assert no_push.resolve_check(CheckRequest(sheet=sheet, stat="nerve"), _Dice(1)).sheet is None

    quiet = registry.resolve("uro-pbta", "", {"xp_on_miss": 0})
    pbta_sheet = quiet.new_character(CharSpec(), Rng(0))
    assert quiet.resolve_check(CheckRequest(sheet=pbta_sheet), _Dice(2)).sheet is None
    fast = registry.resolve("uro-pbta", "", {"xp_per_advance": 1})
    advanced = fast.resolve_check(CheckRequest(sheet=pbta_sheet, stat="quick"), _Dice(2)).sheet
    assert advanced is not None and advanced["stats"]["quick"] == pbta_sheet["stats"]["quick"] + 1

    capped = registry.resolve("uro-basic", "", {"max_level": 2})
    d20 = capped.new_character(CharSpec(data={"level": 5}), Rng(0))
    assert d20["level"] == 2
    assert capped.progression(d20, Award(data={"levels": 3}))["level"] == 2


async def test_a_worlds_settings_persist_and_reach_its_campaigns(store: PostgresEventStore) -> None:
    settings = {"push_strain": 2, "strain_limit": 3}
    world = await store.create_world(
        f"Tuned-{new_id()}", ruleset_id="uro-pool", ruleset_config=settings
    )
    pin = await store.world_ruleset(world.main_branch_id)
    assert (pin.id, pin.config) == ("uro-pool", settings)
    campaign = Campaign(
        campaign_id="c",
        world_id=world.world_id,
        branch_id=world.main_branch_id,
        ruleset_id="uro-pool",
    )
    played = await registry.resolve_for_campaign(store, campaign)
    assert played.config.push_strain == 2  # type: ignore[attr-defined]

    head = (await store.get_branch(world.main_branch_id)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(world.world_id, head, "what-if")  # type: ignore[arg-type]
    assert (await store.world_ruleset(fork.branch_id)).config == settings  # forks share it

    bundle = await store.export_world(world.world_id)
    imported = await store.import_world(bundle)
    assert (await store.world_ruleset(imported.main_branch_id)).config == settings


async def test_bad_settings_never_make_a_world(store: PostgresEventStore) -> None:
    name = f"Broken-{new_id()}"
    with pytest.raises(ValueError, match="uro-pool"):
        await store.create_world(name, ruleset_id="uro-pool", ruleset_config={"nope": 1})
    assert name not in [w.name for w in await store.list_worlds()]


async def test_a_pack_with_bad_settings_is_refused_at_validate_and_import(
    store: PostgresEventStore,
) -> None:
    from uro_core.worldpack.create import import_pack, ruleset_problem
    from uro_core.worldpack.parse import parse_pack

    pack = parse_pack(WORLDS / "brinehold")  # an uro-pool pack
    assert ruleset_problem(pack) is None
    pack.manifest.ruleset.config = {"strain_limt": 3}
    assert "strain_limt" in (ruleset_problem(pack) or "")
    with pytest.raises(PackError, match="strain_limt"):
        await import_pack(store, pack)


async def test_a_packs_settings_are_imported_with_the_world(store: PostgresEventStore) -> None:
    from uro_core.worldpack.create import import_pack
    from uro_core.worldpack.parse import parse_pack

    pack = parse_pack(WORLDS / "brinehold")
    pack.manifest.name = f"Brinehold-{new_id()}"
    pack.manifest.ruleset.config = {"push_strain": 2}
    result = await import_pack(store, pack)
    pin = await store.world_ruleset(result.world.main_branch_id)
    assert (pin.id, pin.config) == ("uro-pool", {"push_strain": 2})
