"""Actor service: off-screen belief / rumor propagation (docs/02, 04, OQ-4). Deterministic — no LLM.

simulate-on-observation: when something happens with WITNESSES, belief fans out along `knows`
contact edges with per-hop confidence decay. It is **tier-agnostic** — an ordinary downstream
NPC (any tier) can acquire and later retell a rumor. Each acquired `BeliefChanged` records
`learned_from` = whom the NPC heard it from, so the chain is traceable back to the original
witnesses (the war-story ripple, docs/10 Phase 5). No witnesses → nothing propagates → nobody
ever mentions it.

Distortion is modelled as CONFIDENCE decay: a belief acquired third-hand at confidence 0.27 is
a vague, garbled rumor, and the narrator (given a low-confidence belief) retells it as one — not
as settled fact. A garbled-STATEMENT model (a fresh claim per hop) is a later refinement.
"""

from __future__ import annotations

from uro_core.domain.events import CausedBy, DomainEvent, belief_changed
from uro_core.ports.projections import ProjectionQueries

# The Actor service is emitter A (docs/12): off-screen agency. Its BeliefChanged fan-out carries
# an agenda cause, distinct from the extractor's (X) in-scene belief updates.
_ACTOR_CAUSE = CausedBy(kind="agenda")


async def propagate_belief(
    store: ProjectionQueries,
    branch_id: str,
    *,
    claim_id: str,
    witnesses: list[str],
    base_confidence: float = 0.9,
    decay: float = 0.55,
    floor: float = 0.2,
    max_hops: int = 4,
) -> list[DomainEvent]:
    """Fan a belief about `claim_id` out from `witnesses` along `knows` edges. Returns the
    `BeliefChanged` events (the caller commits them). Confidence decays each hop; propagation
    stops below `floor` or past `max_hops`. First-hearer wins (a belief isn't overwritten by a
    later, weaker path), which keeps the cascade finite and the `learned_from` chain a tree."""
    events: list[DomainEvent] = []
    confidence: dict[str, float] = {}
    for witness in witnesses:  # direct observation → a strong, first-hand belief
        if witness in confidence:
            continue
        confidence[witness] = base_confidence
        events.append(
            belief_changed(
                actor_id=witness,
                claim_id=claim_id,
                confidence=base_confidence,
                learned_from=None,
                caused_by=_ACTOR_CAUSE,
            )
        )
    frontier = list(confidence)
    for _ in range(max_hops):
        if not frontier:
            break
        next_frontier: list[str] = []
        for src in frontier:
            heard_confidence = round(confidence[src] * decay, 3)
            if heard_confidence < floor:
                continue
            for edge in await store.edges_from(branch_id, src):
                if edge.rel_type != "knows" or edge.dst in confidence:
                    continue
                confidence[edge.dst] = heard_confidence
                events.append(
                    belief_changed(
                        actor_id=edge.dst,
                        claim_id=claim_id,
                        confidence=heard_confidence,
                        learned_from=src,
                        caused_by=_ACTOR_CAUSE,
                    )
                )
                next_frontier.append(edge.dst)
        frontier = next_frontier
    return events
