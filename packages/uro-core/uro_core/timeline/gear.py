"""A fresh PC's starting gear (Phase 3, D-97): real items, so a lost fight leaves something to
loot — and the WORLD's, not the engine's: `[world] starting_gear` in its pack, carried in
WorldGenesis. Every surface that seats a fresh PC (CLI `world new` / `campaign new|join`, the
server's campaign create/join) reads it here, so the surfaces can't drift; an adopted actor keeps
what it owns. A world that names none gives none."""

from __future__ import annotations


async def starting_gear(store: object, branch_id: str) -> list[str]:
    """The items a fresh PC carries on this branch's world. A store from before D-97 (no
    `world_starting_gear`) gives none."""
    read = getattr(store, "world_starting_gear", None)
    return list(await read(branch_id)) if read is not None else []
