"""Phase 6 inc 6.3 (D-30): the ruleset registry + world-pack binding. Deterministic.

Proves a world pack's `ruleset = "id@version"` declaration resolves to a bound ruleset — a PbtA
pack binds uro_pbta, a d20 pack binds uro_basic — and that the campaign PINS the choice so a
later play/fork rebinds the same ruleset. Before this, the play path hard-bound uro_basic and
ignored the declaration entirely.
"""

from pathlib import Path

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.rulesets import registry
from uro_core.rulesets.base import CharSpec
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.rulesets.uro_pbta import UroPbtA
from uro_core.worldpack.parse import parse_pack
from uro_core.worldpack.sufficiency import check_sufficiency

WORLDS = Path(__file__).resolve().parents[3] / "worlds"


# --- the registry (pure) ---


def test_resolve_default_and_builtins() -> None:
    assert isinstance(registry.resolve(), UroBasic)  # empty id → default
    assert isinstance(registry.resolve("uro-basic"), UroBasic)
    assert isinstance(registry.resolve("uro-pbta"), UroPbtA)
    assert {"uro-basic", "uro-pbta"} <= set(registry.available())


def test_resolve_unknown_fails_loudly() -> None:
    # A campaign pinned to a ruleset the build no longer has is a real error, not a silent default.
    with pytest.raises(KeyError, match="unknown ruleset 'srd51'"):
        registry.resolve("srd51")


def test_register_adds_a_factory() -> None:
    registry.register("test-only", UroPbtA)
    try:
        assert isinstance(registry.resolve("test-only"), UroPbtA)
        assert "test-only" in registry.available()
    finally:
        registry._FACTORIES.pop("test-only", None)  # keep global registry clean for other tests


# --- the alien world pack selects the alien ruleset ---


def test_emberfell_pack_declares_the_pbta_ruleset() -> None:
    pack = parse_pack(WORLDS / "emberfell")
    assert pack.manifest.ruleset.id == "uro-pbta"
    assert isinstance(registry.resolve(pack.manifest.ruleset.id), UroPbtA)
    # it is a real, runnable pack (3 actors, a conflict seed, tone, seeded history)
    assert check_sufficiency(pack).grade == "runnable"


# --- binding flows through world → campaign → get_campaign (DB) ---


async def test_world_and_campaign_bind_the_declared_ruleset(store: PostgresEventStore) -> None:
    # A world created with the PbtA ruleset records it on WorldGenesis; a campaign started on it
    # binds uro_pbta and pins the version — so play/fork rebinds the SAME ruleset (D-30).
    world = await store.create_world(
        f"Ember-{new_id()}", ruleset_id="uro-pbta", ruleset_version=">=0"
    )
    main = world.main_branch_id
    assert await store.world_ruleset(main) == ("uro-pbta", ">=0")

    pbta_sheet = registry.resolve("uro-pbta").new_character(CharSpec(), Rng(0))
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        new_pc_name="Ash",
        new_pc_id="a:ash",
        pc_sheet=pbta_sheet,
        ruleset_id="uro-pbta",
        ruleset_version=">=0",
    )
    assert campaign.ruleset_id == "uro-pbta" and campaign.ruleset_version == ">=0"
    # the pin survives a reload (get_campaign reads the campaigns projection columns)
    reloaded = await store.get_campaign(campaign.campaign_id)
    assert reloaded is not None
    assert reloaded.ruleset_id == "uro-pbta"
    assert isinstance(registry.resolve(reloaded.ruleset_id, reloaded.ruleset_version), UroPbtA)
    # the PC got a PbtA sheet (harm clock, no hp), not a d20 one
    sheet = await store.get_sheet(main, "a:ash")
    assert sheet is not None and "harm" in sheet and "hp" not in sheet


async def test_blank_world_defaults_to_uro_basic(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Blank-{new_id()}")
    assert await store.world_ruleset(world.main_branch_id) == ("", "")
    assert isinstance(registry.resolve(""), UroBasic)  # empty → default
