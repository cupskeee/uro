"""The Loopwalker's Codex — the ONE thing that legitimately crosses forks.

A clue discovered inside a loop is a durable Uro claim ON THAT LOOP'S BRANCH. The next loop is a
fresh `fork_branch(ORIGIN)`, so the world has never heard of it — that is the engine working
exactly as designed, and it is the whole fiction: the village forgets, you do not. The Codex is
the player's out-of-world meta-knowledge, and it is the game's only persistent state.

The brief says "a JSON file **or** a dedicated never-forked Uro branch — pick one and justify".
Both are implemented here so the justification rests on evidence, and both are exercised by the
test suite. What the comparison actually showed (see GAP_REPORT, target 3):

  FileCodex   — 1 fsync per discovery, zero Uro calls, trivially inspectable, and honest about
                what it is: OUT-of-world knowledge, stored outside the world.
  BranchCodex — the clue log becomes real Uro events on a branch that is never forked from and
                never forked into. It survives export/import with the world, it is queryable
                with the same projections as everything else, and — the sharp part — because it
                is HOST-AUTHORED via `append_beat`, it can carry the STABLE claim ids
                (`k:K1`...) that the extractor refuses to let a game choose (extraction.py:185
                mints `c:{ulid}`). So the Codex knows what the loop branches cannot say.
                It costs one commit per discovery and one extra branch per world, and it is a
                lie about ownership: it puts player meta-knowledge inside the world's event log,
                where a fork could in principle carry it.

Neither is wrong; the file is the honest boundary and the branch is the durable one. The real
finding is that Uro has NO concept for either — no cross-branch/player-scoped persistent
knowledge — so a time-loop consumer must invent this layer itself. That is a GAP_REPORT row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from frictionlog import gap
from uro_core.domain.events import claim_recorded
from world import CLUES, KEYSTONES

CODEX_BRANCH = "codex"  # the never-forked ledger branch (BranchCodex)


@dataclass(frozen=True)
class Entry:
    key: str  # K1..K4 — the game's clue key
    loop: str  # the loop branch that first discovered it
    segment: int  # the segment it was learned in


class Codex(Protocol):
    async def load(self) -> None: ...
    async def record(self, key: str, *, loop: str, segment: int) -> bool: ...
    def known(self) -> set[str]: ...
    def entries(self) -> list[Entry]: ...
    def complete(self) -> bool: ...


class _Base:
    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}

    def known(self) -> set[str]:
        return set(self._entries)

    def entries(self) -> list[Entry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def complete(self) -> bool:
        return set(KEYSTONES) <= self.known()

    def unlocked_by(self, key: str) -> bool:
        """Are this clue's prerequisites satisfied by what the Loopwalker already knows?"""
        return all(req in self._entries for req in CLUES[key]["requires"])


class FileCodex(_Base):
    """Game-side JSON. The honest boundary: out-of-world knowledge, stored out of world."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.kind = "file"

    async def load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self._entries = {e["key"]: Entry(**e) for e in raw.get("clues", [])}

    async def record(self, key: str, *, loop: str, segment: int) -> bool:
        if key in self._entries:
            return False
        self._entries[key] = Entry(key=key, loop=loop, segment=segment)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"clues": [vars(e) for e in self.entries()]},
                indent=2,
            )
        )
        return True


class BranchCodex(_Base):
    """A dedicated Uro branch that is NEVER forked from and never forked into. Clue discoveries
    are host-authored `ClaimRecorded` events with STABLE ids (`k:K1`) — the ids the extractor
    would not let the game choose."""

    def __init__(self, store: Any, world_id: str, branch_id: str) -> None:
        super().__init__()
        self.store = store
        self.world_id = world_id
        self.branch_id = branch_id
        self.kind = "branch"

    async def load(self) -> None:
        for claim in await self.store.list_claims(self.branch_id):
            if not claim.claim_id.startswith("k:"):
                continue
            key, loop, segment = _decode(claim.claim_id, claim.statement)
            self._entries[key] = Entry(key=key, loop=loop, segment=segment)

    async def record(self, key: str, *, loop: str, segment: int) -> bool:
        if key in self._entries:
            return False
        await self.store.append_beat(
            self.branch_id,
            [
                claim_recorded(
                    claim_id=f"k:{key}",  # <- a STABLE id, because the GAME authored it
                    statement=_encode(key, loop, segment),
                    subject_refs=[],
                    truth="true",
                    origin="codex",
                )
            ],
        )
        self._entries[key] = Entry(key=key, loop=loop, segment=segment)
        return True


def _encode(key: str, loop: str, segment: int) -> str:
    return f"The Loopwalker learned {key} ({CLUES[key]['title']}) in {loop} at segment {segment}."


def _decode(claim_id: str, statement: str) -> tuple[str, str, int]:
    key = claim_id.removeprefix("k:")
    loop, segment = "?", -1
    if " in " in statement and " at segment " in statement:
        tail = statement.split(" in ", 1)[1]
        loop = tail.split(" at segment ", 1)[0]
        segment = int(tail.rsplit(" at segment ", 1)[1].rstrip("."))
    return key, loop, segment


async def open_codex(kind: str, *, store: Any, world_id: str, out_dir: Path) -> Codex:
    """Open the Codex in the chosen backend. Logs the gap that forced this layer to exist."""
    gap(
        gap="Player knowledge that survives a fork (the whole premise of a time loop)",
        happened="Uro has NO cross-branch or player-scoped persistent memory. A fork is a full "
        "copy-on-write of world state at a commit and every loop is a fork from the same origin "
        "commit, so the world CORRECTLY forgets every clue — but nothing in the engine can "
        "remember on the player's behalf. `claims`/`beliefs`/`memory_index` are all strictly "
        "branch-scoped (store.search filters `WHERE m.branch_id = $1`, store.py:1419-1431)",
        workaround="The game invents the Loopwalker's Codex — implemented twice (a JSON file, "
        "and a dedicated never-forked Uro branch of host-authored k: claims) so the boundary "
        "could be compared with evidence rather than asserted",
        severity="major",
        needs="a player/participant-scoped knowledge store the engine owns (e.g. claims on a "
        "campaign-scoped 'player' lane that forks do not reset), or a documented statement that "
        "cross-fork memory is deliberately the consumer's job",
        evidence="codex.py open_codex; uro_core/adapters/postgres/store.py:1419-1431 (search is "
        "branch-scoped); every loop's list_claims proves the reset (game.py verify_reset)",
    )
    if kind == "branch":
        branch = await store.get_branch_by_name(world_id, CODEX_BRANCH)
        assert branch is not None, "the codex branch must be created at world genesis"
        codex: Codex = BranchCodex(store, world_id, branch.branch_id)
    else:
        codex = FileCodex(out_dir / "codex.json")
    await codex.load()
    return codex
