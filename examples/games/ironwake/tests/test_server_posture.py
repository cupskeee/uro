"""Inc-7 / acceptance F.4 evidence: the SAME season plays over the server posture (HTTP
OutcomeBundle POSTs + WS town beats) and lands on the SAME deterministic arc as embed.

This is the slow test (it boots `uro serve` as a subprocess and runs two full seasons); it
skips with the rest when Postgres is down. The server binds an ephemeral free port so it never
collides with a dev server or a parallel run."""

from __future__ import annotations

import socket

from uro_core.adapters.postgres.store import PostgresEventStore

from ironwake.cli.season import run_season


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_server_posture_plays_the_same_season(
    store: PostgresEventStore,
    monkeypatch,
) -> None:
    import ironwake.world.uro as uro_mod

    monkeypatch.setattr(uro_mod, "SERVER_PORT", _free_port())
    over_wire = await run_season(seed=7, posture="server")
    embedded = await run_season(seed=7, posture="embed")
    # the transport must not change the world: same casualties, feats, pays, ending
    assert over_wire.digest() == embedded.digest()
    assert all(ok for _, ok in over_wire.checks), [
        label for label, ok in over_wire.checks if not ok
    ]
