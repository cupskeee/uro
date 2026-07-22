"""Instance-level extraction policy (D-49): which EMERGENT world state play may create.

The extractor turns narration into proposed state; this policy gates which categories the gauntlet
actually commits. Instance-level config, OFF the event/branch axis (like the model-connection
registry, D-47) — NOT world-state, never forked or exported, one policy per instance.

Covers every category the extractor can propose: actors, places, factions, threads, and claims
(beliefs ride claims). Emergent world is RELATIONAL (D-50) — an actor cascade-creates the faction
it's `member_of` and the place it's `located_in` — so `extract_factions`/`extract_places` also gate
those cascades. Claims/beliefs are toggleable but the engine NEEDS them (recall degrades without
them), so a client (uro-loom) must disclaim that when offering the toggle. Default all-on.
"""

from __future__ import annotations

from pydantic import BaseModel


class ExtractionPolicy(BaseModel):
    extract_actors: bool = True
    extract_places: bool = True
    extract_factions: bool = True  # also gates the actor→member_of cascade (D-50)
    extract_threads: bool = True  # emergent plots (D-50; never deduped)
    extract_claims: bool = True  # includes beliefs; the engine relies on these for recall
