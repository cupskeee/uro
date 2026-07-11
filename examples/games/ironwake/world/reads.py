"""Library read-backs — the town UI's data layer (TASK inc 4/6; stress goal 6).

EVERY read in this file goes through the embedded `uro_core` store, in BOTH postures. That is
the point: Uro's entire HTTP surface is WS /play + POST /outcome + /healthz, so a network
Chronicler game has no way to ask the server "who is on my roster? what does Mira believe?
what happened last season?" over the wire. In `--posture server` IRONWAKE writes over HTTP but
must keep a SECOND, in-process library connection open just to read its own world back
(world/uro.py logs that gap with the exact endpoints we needed).
"""

from __future__ import annotations

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.pipeline.recall import assemble_recall, build_narrator_messages

from ironwake import frictionlog


def certainty_phrase(confidence: float) -> str:
    """Mirror of the narrator's certainty rendering (uro_core.pipeline.recall._certainty —
    private, so the thresholds are replicated here): >=0.75 'is certain', >=0.45 'believes',
    below that 'has heard a rumor'. The town UI renders gossip with the same voice the
    narrator prompt uses — and the duplication itself is a gap (filed below): if the engine
    retunes its bands, this UI silently drifts out of voice."""
    frictionlog.gap(
        gap="render town gossip in the narrator's own certainty voice",
        happened=(
            "the confidence->phrasing mapping lives in a PRIVATE helper "
            "(uro_core.pipeline.recall._certainty); no public API exposes the bands, so the "
            "game replicates the 0.75/0.45 thresholds and will silently drift if Uro retunes"
        ),
        workaround="thresholds copied into world/reads.py certainty_phrase (this function)",
        severity="cosmetic",
        needs="a public certainty_phrase(confidence) (or expose the bands) on the recall module",
        evidence="world/reads.py certainty_phrase vs uro_core/pipeline/recall.py:110-117",
    )
    if confidence >= 0.75:
        return "is certain"
    if confidence >= 0.45:
        return "believes"
    return "has heard a rumor"


async def gossip_at(
    store: PostgresEventStore, branch: str, npc_id: str
) -> list[tuple[float, str, str]]:
    """What one town NPC believes: (confidence, certainty phrase, statement), strongest first.
    This IS the near/far demonstration — the same feat sits at different confidences down the
    knows-chain, and the phrasing hedges as it travels."""
    beliefs = await store.beliefs_of(branch, npc_id)
    out: list[tuple[float, str, str]] = []
    for b in beliefs:
        claim = await store.get_claim(branch, b.claim_id)
        if claim is not None:
            out.append((b.confidence, certainty_phrase(b.confidence), claim.statement))
    return sorted(out, key=lambda t: (-t[0], t[2]))


async def narrator_context(store: PostgresEventStore, branch: str, intent: str) -> list[str]:
    """The context block the narrator would actually be given for this intent (assemble_recall +
    build_narrator_messages — the engine's own recall path). With the stub provider the PROSE is
    canned, so honesty demands asserting/showing the narrator's INPUT: these are the belief
    lines ('Mira believes: ...' / 'Corin has heard a rumor: ...') that a real model would voice."""
    recall = await assemble_recall(store, branch, intent, 8)
    messages = build_narrator_messages(recall, intent)
    context = [m.content for m in messages if m.role == "system"]
    return context[1].splitlines() if len(context) > 1 else []


async def chronicle_summary(store: PostgresEventStore, branch: str) -> dict:
    """The season ledger as Uro remembers it — used for the summary and the fork diff."""
    actors = await store.list_actors(branch)
    dead = sorted(a.actor_id for a in actors if a.status == "dead")
    threads = {t.thread_id: t.state for t in await store.list_threads(branch)}
    wars = [(e.src, e.dst) for e in await store.list_edges(branch, "at_war_with")]
    claims = await store.list_claims(branch)
    rumors = sorted(c.statement for c in claims if c.truth == "unknown")
    return {
        "dead": dead,
        "threads": threads,
        "wars": sorted(wars),
        "rumor_count": len(rumors),
        "rumors": rumors,
        "world_day": await store.current_world_time(branch),
    }
