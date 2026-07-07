"""AI backfill (docs/09): the *assisted* sufficiency policy. LLM-generated, opt-in, tagged.

When a pack grades `thin`/`insufficient`, the World service can offer to fill the declared gaps
with a generation pass — every generated element tagged `provenance="ai_backfill"` so authors
review it and platforms show what the machine invented. Silent invention is never the default:
backfill runs only when the operator asks. CI never calls a live model — a scripted provider
drives the deterministic test; the live pass is the operator's (docs/10 thesis split).
"""

from __future__ import annotations

import json

from uro_core.domain.ids import new_id
from uro_core.providers.base import Message
from uro_core.providers.router import ProviderRouter
from uro_core.worldpack.models import ThreadSeed, WorldPack
from uro_core.worldpack.sufficiency import SufficiencyReport, check_sufficiency

BACKFILL_ROLE = "worldsmith"  # generation of world seeds (docs/04 role set is a living list)

_CONFLICT_SYSTEM = (
    "You are a world-builder filling a GAP in an author's setting. Generate ONE conflict seed — "
    "a live tension the players can act on — that fits the world's tone, factions, and places. "
    "Ground it in what already exists; invent no new proper nouns the author didn't provide. "
    'Output ONLY JSON: {"stakes": "one or two sentences", "state": "dormant|offered|active"}.'
)


async def backfill_gaps(
    pack: WorldPack, router: ProviderRouter, *, report: SufficiencyReport | None = None
) -> tuple[WorldPack, list[str]]:
    """Fill a pack's sufficiency gaps with tagged, AI-generated seeds. Returns the augmented
    pack + a human-readable list of what was added. Currently fills the `conflict` gap (the
    common thin-pack case); other dimensions extend the same ask-generate-tag pattern."""
    report = report or check_sufficiency(pack)
    threads = list(pack.threads)
    added: list[str] = []
    for dim in report.dimensions:
        if dim.ok or dim.name != "conflict":
            continue
        seed = await _generate_conflict(pack, router)
        if seed is not None:
            threads.append(seed)
            added.append(f"conflict seed (ai_backfill): {seed.stakes}")
    augmented = pack.model_copy(update={"threads": threads})
    return augmented, added


async def _generate_conflict(pack: WorldPack, router: ProviderRouter) -> ThreadSeed | None:
    factions = ", ".join(f.name for f in pack.factions) or "(none)"
    places = ", ".join(p.name for p in pack.places) or "(none)"
    user = (
        f"WORLD: {pack.manifest.name}\nTONE: {', '.join(pack.manifest.tone) or '(unspecified)'}\n"
        f"FACTIONS: {factions}\nPLACES: {places}\n\n"
        "Generate one conflict seed grounded in the above."
    )
    messages = [
        Message(role="system", content=_CONFLICT_SYSTEM),
        Message(role="user", content=user),
    ]
    raw = await router.complete(BACKFILL_ROLE, messages, json_mode=True, temperature=0.7)
    try:
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        stakes = str(data["stakes"]).strip()
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    if not stakes:
        return None
    state = data.get("state", "dormant")
    if state not in ("dormant", "offered", "active"):
        state = "dormant"
    return ThreadSeed(id=f"t:{new_id()}", stakes=stakes, state=state, provenance="ai_backfill")
