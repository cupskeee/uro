"""Instance-level extraction policy (D-49): which EMERGENT world state play may create.

The extractor turns narration into proposed state; this policy gates which categories the gauntlet
actually commits. Instance-level config, OFF the event/branch axis (like the model-connection
registry, D-47) — NOT world-state, never forked or exported, one policy per instance.

Only the categories the extractor CAN propose are here: actors, places, claims (beliefs ride
claims). Threads + factions are authored-only (no emergent extractor yet). Claims/beliefs are
toggleable but the engine NEEDS them — recall/continuity degrades without them — so a client
(uro-loom) must disclaim that when offering the toggle.
"""

from __future__ import annotations

from pydantic import BaseModel


class ExtractionPolicy(BaseModel):
    extract_actors: bool = True
    extract_places: bool = True
    extract_claims: bool = True  # includes beliefs; the engine relies on these for recall
