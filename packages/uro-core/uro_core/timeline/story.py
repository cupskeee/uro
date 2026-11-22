"""A branch's state as its story has shown it (D-117): the scene-safe read a player's client
needs, so it doesn't have to hold the whole world and hide most of it.

The `actors`, `places`, `factions` and `threads` sections list everything on the branch, including
people and places no beat has reached. This narrows them to what the told story contains:

- a person, place or faction once a beat's narration has named it (its name or an alias, as whole
  words, in any case), or a beat created it;
- every actor with a player binding on the branch, released ones included (a table knows who
  is at it, and an ended story who its character was);
- a thread unless it is dormant: a dormant thread hasn't entered play.

Nothing is recorded for this. It is a pure function of the branch's saved beats and its rows, so a
fork, a replay or an import tells the same story the same way. Matching is by name: a person the
narration only ever calls "the stranger" is not shown until a beat names them or an alias says so
(best-effort by construction of prose, not a guarantee). Rows keep their shape; other sections
pass through untouched."""

from __future__ import annotations

import re
from typing import Any

from uro_core.ports.projections import EngineStore
from uro_core.timeline.beats import BeatRecord

SCOPES: tuple[str, ...] = ("world", "story")
_ALL_BEATS = 1_000_000  # every beat of the branch's history
_NAMED = {"actors": "actor_id", "places": "place_id", "factions": "faction_id"}
_CREATED_KIND = {"actors": "actor", "places": "place", "factions": "faction"}


def _told(text: str, names: list[str]) -> bool:
    """One of `names` appears in `text` (already casefolded) as whole words."""
    for name in names:
        folded = name.strip().casefold()
        if folded and re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", text):
            return True
    return False


def story_scope(
    state: dict[str, list[dict[str, Any]]],
    records: list[BeatRecord],
    pc_actor_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """`state` narrowed to what `records` (the branch's saved beats) have shown."""
    text = "\n".join(r.beat.narration for r in records).casefold()
    made = {(c.kind, c.id) for r in records for c in r.created}
    out = dict(state)
    for section, id_key in _NAMED.items():
        if section not in state:
            continue
        kind = _CREATED_KIND[section]
        out[section] = [
            row
            for row in state[section]
            if (kind, row[id_key]) in made
            or (section == "actors" and row[id_key] in pc_actor_ids)
            or _told(text, [row.get("name") or "", *(row.get("aliases") or [])])
        ]
    if "threads" in state:
        out["threads"] = [t for t in state["threads"] if t.get("state") != "dormant"]
    return out


async def story_state(
    store: EngineStore, branch_id: str, sections: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """A branch's `sections` in story scope (`uro state --story`, `GET …/state?scope=story`)."""
    wanted = list(dict.fromkeys([*sections, "pcs"]))
    rows = (await store.query_across([branch_id], wanted)).get(branch_id, {})
    pcs = {row["actor_id"] for row in rows.get("pcs", [])}
    records = await store.beat_records(branch_id, _ALL_BEATS)
    scoped = story_scope({s: rows.get(s, []) for s in sections}, records, pcs)
    return {s: scoped[s] for s in sections}
