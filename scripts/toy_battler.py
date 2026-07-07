"""A tiny TOY external auto-battler — the "game" Uro wraps in Chronicler mode (docs/10, D-25).

This is NOT part of the engine. It knows nothing about Uro beyond emitting an `OutcomeBundle`:
it resolves a fight in its OWN domain ("can I hit it, for how much?") and reports what happened.
Uro's Chronicler distillation then answers "who knows what, and how does it change the story?".

Deterministic (seeded), ~40 lines. Run standalone: `python scripts/toy_battler.py`.
"""

from __future__ import annotations

from uro_core.chronicler import Feat, LootTransfer, OutcomeBundle
from uro_core.rulesets.rng import Rng


def fight(pc: str, warband: list[str], *, seed: int, survivors: int) -> OutcomeBundle:
    """Resolve a one-sided legend: the PC fells the warband's champion with a spectacular blow.
    `survivors` of the rank-and-file live to witness it (and carry the tale); the rest fall."""
    rng = Rng(seed)
    champion, rank_and_file = warband[0], warband[1:]
    survivors = max(0, min(survivors, len(rank_and_file)))
    witnesses = rank_and_file[:survivors]
    casualties = [champion, *rank_and_file[survivors:]]
    blow = rng.choice(["split in two", "hurled from the wall", "unmade with a word"])
    return OutcomeBundle(
        encounter_id=f"e:battle-{seed}",
        participants=[pc, *warband],
        witnesses=witnesses,
        casualties=casualties,
        feats=[Feat(actor=pc, description=f"a lone wizard {blow} the warband's champion")],
        loot=[LootTransfer(item_id=f"i:champion-blade-{seed}", from_ref=champion, to_ref=pc)],
        duration_rounds=1 + rng.die(4),
    )


if __name__ == "__main__":
    outcome = fight("a:hero", ["a:champion", "a:raider1", "a:raider2"], seed=7, survivors=1)
    print(outcome.model_dump_json(indent=2))
