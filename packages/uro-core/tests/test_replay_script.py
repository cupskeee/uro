"""`scripts/extraction_replay.py` — the harness that replays recorded narration through the current
extractor. It replays onto a fork of the source world, so a second run must not mistake the first
run's fork for the original (review)."""

import sys
from pathlib import Path

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import beat_resolved
from uro_core.domain.ids import new_id

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from extraction_replay import _beat_branch, _before_first_beat


async def test_a_previous_replay_is_never_the_source(store: PostgresEventStore) -> None:
    world = await store.create_world(f"replayed-{new_id()}")
    main = world.main_branch_id
    start = (await store.get_branch(main)).head_commit  # type: ignore[union-attr]

    def beat(n: int) -> list:  # type: ignore[type-arg]
        return [
            beat_resolved(
                beat_id=new_id(), participant_id="p1", intent_text=f"i{n}", narration=f"n{n}"
            )
        ]

    for n in range(2):
        await store.append_beat(main, beat(n))
    await store.time_skip(main, 1)  # the original has a non-beat commit; a replay doesn't
    assert await _before_first_beat(store, main) == start

    replay = await store.fork_branch(world.world_id, start, f"replay-{new_id()}")
    for n in range(2):
        await store.append_beat(replay.branch_id, beat(n))
    assert await _beat_branch(store, world.world_id) == (main, 2)
