"""Phase 4 inc 4.4: AI backfill + capability probes (docs/09, 04, D-24). No live calls — a
scripted provider stands in for the model (the live pass is the operator's, docs/10).

Backfill fills a thin pack's declared gaps with provenance-tagged seeds; probes report whether
the bound models can deliver what the world declares (a compatibility signal, not enforcement).
"""

from collections.abc import AsyncIterator
from pathlib import Path

from uro_core.engines.probe import run_probes
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.worldpack.backfill import backfill_gaps
from uro_core.worldpack.parse import parse_pack
from uro_core.worldpack.sufficiency import check_sufficiency

WORLDS = Path(__file__).resolve().parents[3] / "worlds"


class _Provider:
    """Routes by role (req.stage_tag): compliant JSON for planner/worldsmith, prose for the
    narrator, and a configurable judge verdict."""

    def __init__(self, *, narrator: str = "A grim tide rises over Vel.", judge: str = "engaged"):
        self._narrator = narrator
        self._judge = judge

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield self._narrator

    async def complete(self, req: CompletionRequest) -> str:
        return {
            "planner": '{"ok": true}',
            "worldsmith": '{"stakes": "The Council hides a blight that will starve the vale.", '
            '"state": "dormant"}',
            "narrator": self._narrator,
            "judge": self._judge,
        }.get(req.stage_tag, "{}")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _router(**kw: str) -> ProviderRouter:
    return ProviderRouter(bindings={}, default=_Provider(**kw))


async def test_backfill_fills_conflict_gap() -> None:
    pack = parse_pack(WORLDS / "thornwood")
    assert check_sufficiency(pack).grade == "thin"
    augmented, added = await backfill_gaps(pack, _router())
    assert check_sufficiency(augmented).grade == "runnable"  # the gap is filled
    assert len(added) == 1 and "ai_backfill" in added[0]
    generated = [t for t in augmented.threads if t.provenance == "ai_backfill"]
    assert len(generated) == 1 and generated[0].stakes  # tagged for author review


async def test_backfill_is_noop_when_runnable() -> None:
    pack = parse_pack(WORLDS / "ashfall")
    augmented, added = await backfill_gaps(pack, _router())
    assert added == [] and augmented.threads == pack.threads


async def test_probe_passes_with_a_compliant_model() -> None:
    manifest = parse_pack(WORLDS / "ashfall").manifest  # enables violence, horror at "mature"
    report = await run_probes(manifest, _router(), tries=3)
    assert report.ok
    by_name = {r.name: r for r in report.results}
    assert by_name["structured_output"].status == "pass"
    assert by_name["content_rating"].status == "pass"
    assert by_name["content_rating"].transcripts  # raw transcripts attached (D-24)


async def test_probe_warns_on_refusal_but_does_not_fail() -> None:
    manifest = parse_pack(WORLDS / "ashfall").manifest
    report = await run_probes(
        manifest, _router(narrator="I cannot write that.", judge="refused"), tries=3
    )
    rating = next(r for r in report.results if r.name == "content_rating")
    assert rating.status == "warn"  # a refusing narrator → loud warning...
    assert report.ok  # ...but not a hard fail: the engine still runs (signal, not gate — D-5/D-24)
