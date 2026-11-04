"""A playthrough on its own branch (D-100): the one call a client makes for "start a new game".

A campaign started on a world's `main` shares that timeline with every other campaign on it, and
can't be removed on its own (D-99). A playthrough forks the source branch (`main` by default) at
its head, or at a marker or commit, into a fresh branch and starts the campaign there. If starting
fails, each surface removes the fork, so nothing half-made is left. The one helper both the CLI
(`uro campaign new --own-branch`) and the server (`POST /worlds/{w}/campaigns` with `own_branch`)
call, so the surfaces can't drift.
"""

from __future__ import annotations

from uro_core.domain.ids import new_id
from uro_core.ports.projections import EngineStore
from uro_core.timeline.models import BranchInfo


async def playthrough_branch(
    store: EngineStore,
    world_id: str,
    source: BranchInfo,
    *,
    from_ref: str | None = None,
    name: str | None = None,
) -> BranchInfo:
    """Fork `source` (at its head, or at `from_ref`: a marker name or a commit id) into a new
    branch for one playthrough. ValueError if there is nothing to fork from."""
    commit = await store.resolve_ref(world_id, from_ref) if from_ref else source.head_commit
    if not commit:
        raise ValueError(f"branch {source.name!r} has no commit to start a playthrough from")
    fork = await store.fork_branch(world_id, commit, name or f"play-{new_id()}")
    branch = await store.get_branch(fork.branch_id)
    if branch is None:  # just made it; a store that loses it is broken
        raise KeyError(fork.branch_id)
    return branch
