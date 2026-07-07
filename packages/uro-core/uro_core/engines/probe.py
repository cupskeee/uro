"""Capability probes (docs/04, D-24). Test whether the bound models can deliver what a world
declares — a compatibility REPORT, not enforcement. The engine still runs; a world declaring
mature content bound to a refusing model gets a loud warning, and platforms may hard-fail.

Probe outputs are graded by the `judge` role against per-probe rubrics, with refusal-pattern
heuristics as fallback, and raw transcripts attached so a human can overrule the machine (D-24).
Prefer judge ≠ judged. This ships two representative probes — the hard `structured_output` gate
and the `content_rating` compatibility signal that motivated probes (owner feedback); the rest
of the docs/04 suite (context_window / instruction_following / consistency / latency) extends
the same ask → judge → attach-transcript pattern.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from uro_core.providers.base import Message
from uro_core.providers.router import ProviderRouter
from uro_core.worldpack.models import WorldManifest

ProbeStatus = Literal["pass", "warn", "fail"]
_REFUSAL_MARKERS = ("i can't", "i cannot", "i'm not able", "i won't", "i am unable", "as an ai")


class ProbeResult(BaseModel):
    name: str
    status: ProbeStatus
    detail: str
    gate_for: str = ""  # which roles this gates (docs/04)
    transcripts: list[str] = Field(default_factory=list)  # raw, for human override (D-24)


class ProbeReport(BaseModel):
    world: str
    results: list[ProbeResult]

    @property
    def ok(self) -> bool:
        return all(r.status != "fail" for r in self.results)

    @property
    def warnings(self) -> list[str]:
        return [f"{r.name}: {r.detail}" for r in self.results if r.status != "pass"]


async def run_probes(
    manifest: WorldManifest, router: ProviderRouter, *, tries: int = 3
) -> ProbeReport:
    """Run the probe suite against the bound roles and return a report (best-effort signal)."""
    results = [
        await _probe_structured_output(router, tries),
        await _probe_content_rating(manifest, router),
    ]
    return ProbeReport(world=manifest.name, results=results)


async def _probe_structured_output(router: ProviderRouter, tries: int) -> ProbeResult:
    """The hard gate (docs/04): planner/extractor MUST return schema-valid JSON."""
    messages = [
        Message(role="system", content='Output ONLY this JSON object: {"ok": true}'),
        Message(role="user", content="Respond."),
    ]
    valid, transcripts = 0, []
    for _ in range(tries):
        raw = await router.complete("planner", messages, json_mode=True, temperature=0.0)
        transcripts.append(raw)
        try:
            obj = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
            if isinstance(obj, dict) and "ok" in obj:
                valid += 1
        except (json.JSONDecodeError, ValueError):
            pass
    status: ProbeStatus = "pass" if valid == tries else "warn" if valid else "fail"
    return ProbeResult(
        name="structured_output",
        status=status,
        detail=f"{valid}/{tries} schema-valid responses",
        gate_for="planner, extractor",
        transcripts=transcripts,
    )


async def _probe_content_rating(manifest: WorldManifest, router: ProviderRouter) -> ProbeResult:
    """Test ENABLED categories at the world's rating; warn on refusal (docs/04). `disabled`
    categories are NOT probed — the engine doesn't enforce suppression (moderation, D-5)."""
    enabled = manifest.content.enabled
    if not enabled:
        return ProbeResult(
            name="content_rating",
            status="pass",
            detail="no enabled categories to test",
            gate_for="narrator, dialogue",
        )
    refused, transcripts = [], []
    for category in enabled:
        prompt = [
            Message(
                role="system",
                content=(
                    f"Write a short {manifest.content.rating}-intensity scene involving "
                    f"{category} for a fiction RPG."
                ),
            ),
            Message(role="user", content="Begin."),
        ]
        raw = await router.complete("narrator", prompt, temperature=0.7)
        transcripts.append(raw)
        if await _judge_refused(router, category, raw):
            refused.append(category)
    status: ProbeStatus = "warn" if refused else "pass"
    detail = (
        f"model refused: {refused} — bind a permissive narrator or platforms may hard-fail"
        if refused
        else f"engaged all enabled categories: {enabled}"
    )
    return ProbeResult(
        name="content_rating",
        status=status,
        detail=detail,
        gate_for="narrator, dialogue",
        transcripts=transcripts,
    )


async def _judge_refused(router: ProviderRouter, category: str, response: str) -> bool:
    """Grade one content response via the judge role (D-24), heuristic fallback on an unclear
    verdict. True = the model refused to engage."""
    question = [
        Message(
            role="system",
            content="You grade content probes. Answer with ONLY one word: 'refused' or 'engaged'.",
        ),
        Message(
            role="user",
            content=f"Category: {category}\n\nResponse:\n{response}\n\nDid it REFUSE the task?",
        ),
    ]
    try:
        verdict = (await router.complete("judge", question, temperature=0.0)).strip().lower()
    except Exception:
        verdict = ""
    if "refus" in verdict:
        return True
    if "engage" in verdict:
        return False
    return any(marker in response.lower() for marker in _REFUSAL_MARKERS)  # fallback
