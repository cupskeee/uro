"""Inc 6-7 assertions: the SEASON is deterministic under a fixed seed (same casualties, same
feats, same ending — acceptance F.1) and the what-if fork genuinely diverges (F.5).

These run the real thing (two full seasons + a fork each), so they are the slow tests."""

from __future__ import annotations

from uro_core.adapters.postgres.store import PostgresEventStore

from ironwake.cli.season import run_season
from ironwake.world.setup import VORLUND


async def test_same_seed_same_season(store: PostgresEventStore) -> None:
    # `store` proves the DB is up (and skips otherwise); run_season manages its own session.
    a = await run_season(seed=7)
    b = await run_season(seed=7)
    assert a.digest() == b.digest()
    assert a.casualties == b.casualties
    assert a.feats == b.feats
    assert a.ending == b.ending
    assert a.fork_diff == b.fork_diff


async def test_seed7_arc_hits_every_stress_target(store: PostgresEventStore) -> None:
    r = await run_season(seed=7)
    assert all(ok for _, ok in r.checks), [label for label, ok in r.checks if not ok]
    # the ceiling: Vorlund is never a canon casualty, however many times the grid kills him
    assert VORLUND not in r.casualties
    assert any("cut down Captain Vorlund" in f for f in r.feats)  # ...but the STORY exists
    # the silence: the mill contract resolves as a wipe on this seed
    assert any(o.startswith("c6-mill:wipe") for o in r.outcomes)
    # the fork diverged from the same chronicle
    assert r.fork_diff["dead_only_main"] != r.fork_diff["dead_only_fork"]
    # permadeath: at least one merc died in canon during the season
    assert any(c.startswith("a:merc-") for c in r.casualties)
