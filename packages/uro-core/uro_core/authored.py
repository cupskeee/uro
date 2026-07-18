"""Authored-outcome distillation (D-41) — the TRUSTED, in-process outcome path.

A Posture-A library embedder already holds root over its world (it constructs the store + Engine and
can commit arbitrary canon via `store.append_beat` / `engine.append_and_react`). Three dogfood games
(Sable G-7, Ironwake 2-3-7, Seventh) hit the D-32 ceiling only because they routed their own
outcomes through `chronicler.distill_outcome` to reuse its distillation SERVICES (witness-rumor
cascade, casualty→`ActorDied`, loot→`ItemTransferred`) — and got fenced as if they were an untrusted
external reporter, so an authored succession-on-death / assassination could never fire.

`distill_authored_outcome` reuses the SAME `_distill` body with `protect=_never_protected`: a
protected (PC / T2+) casualty commits as a real `ActorDied`, protected loot transfers, a named
figure
may witness. It grants NO NEW authority — the embedder could already `append_beat` a death — it only
lets it reuse distillation. Usage:

    from uro_core.authored import distill_authored_outcome
    result = await distill_authored_outcome(store, campaign.branch_id, bundle)
    await engine.append_and_react(campaign, result.events)  # fires ActorDied pack rules (D-33)

**Not a security fence — hygiene.** The trusted tier's safety is that you own the consequences (you
hold root). The real fence is the MODULE boundary: `uro_server` is import-linter-forbidden from
importing this module, so the untrusted network endpoint cannot reach the ceiling-off path (D-41).
Existence/scope still hold (a bundle can't kill a nonexistent or out-of-cast actor) — only the
PROTECTION ceiling is relaxed.

RESIDUAL (named, D-41 review): `_never_protected` relaxes the ceiling for a PC too, so an authored
bundle CAN kill a PC-bound actor — leaving `proj_actors.status='dead'` while the `proj_pcs` binding
stays active (an active-but-dead PC would miscount the party gate / drive a corpse). This is not a
new hole (the embedder could `append_beat` a PC death directly and hit the same inconsistency — a PC
death is a lifecycle event, `end_campaign`/release, not an outcome-bundle casualty): if you author a
PC's death here, release its binding. Making the trusted tier still PC-protect (relaxing only the
T2+ NPC ceiling — the evidenced need) is a reserved one-line refinement.
"""

from __future__ import annotations

from uro_core._distill_core import DistillResult, OutcomeBundle, _distill, _never_protected
from uro_core.ports.projections import ProjectionQueries


async def distill_authored_outcome(
    store: ProjectionQueries, branch_id: str, bundle: OutcomeBundle
) -> DistillResult:
    """Distill a TRUSTED embedder's own outcome — reuse the full distillation (rumor cascade,
    receipt, deterministic ids) with the D-32 protection ceiling OFF (`protect=_never_protected`),
    a protected actor's authored death/loot becomes real canon. Commit `.events` via
    `engine.append_and_react` so authored `ActorDied` pack rules fire (D-41)."""
    return await _distill(store, branch_id, bundle, protect=_never_protected)
