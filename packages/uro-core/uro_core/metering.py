"""Usage metering record (docs/01 rule 5, docs/07, D-14).

Stage-tagged LLM usage, captured from Phase 0 — the docs mandate this from the
start precisely because retrofitting observability is misery. Phase 0 records
stage_tag + latency + prompt hash; token counts arrive when the provider port
grows a usage channel (Phase 1). This data is operational and prunable — never
world truth, never on the timeline.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from uro_core.domain.ids import new_id


class LLMCall(BaseModel):
    call_id: str = Field(default_factory=new_id)
    stage_tag: str  # engine role: narrator, dialogue, planner, …
    prompt_hash: str
    latency_ms: int
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
