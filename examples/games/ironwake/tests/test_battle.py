"""Inc-1 assertions: the tactics engine is a pure function of its seed (no Uro, no DB)."""

from __future__ import annotations

from ironwake.game.scenarios import GRANARY, SILENT_MILL, build_battle
from ironwake.world.setup import STARTING_MERCS

DEMO_ENEMIES = [f"a:rb-demo-{i}" for i in range(len(GRANARY.enemies))]


def _granary(seed: int):
    return build_battle(GRANARY, list(STARTING_MERCS), DEMO_ENEMIES, seed).run()


def test_same_seed_is_byte_stable() -> None:
    a, b = _granary(7), _granary(7)
    assert a.digest() == b.digest()
    assert a.log == b.log  # every roll identical, not just the summary


def test_different_seed_diverges() -> None:
    digests = {_granary(seed).digest() for seed in (7, 8, 9, 10)}
    assert len(digests) > 1


def test_permadeath_and_kill_credit_are_tracked() -> None:
    report = _granary(7)
    assert report.outcome == "win"
    assert set(report.casualties) == set(DEMO_ENEMIES)  # a cull kills tier-0 raiders
    for victim, killer in report.killing_blows.items():
        assert victim in report.casualties
        assert killer.startswith("a:merc-")


def test_desperate_stand_can_wipe_witnessless() -> None:
    """The Silent Mill's collapse makes a total wipe live (TASK inc 5): both sides emptied,
    survivors NONE — the input that must produce Uro's silence."""
    watch = [("a:merc-wren", "Wren", "Crossbow"), ("a:merc-aldo", "Aldo", "Sergeant")]
    mill_enemies = ["a:rb-mill-0", "a:rb-mill-1"]
    report = build_battle(SILENT_MILL, watch, mill_enemies, 7005).run()
    assert report.outcome == "wipe"
    assert report.survivors == ()
    assert set(report.casualties) == {"a:merc-wren", "a:merc-aldo", *mill_enemies}
