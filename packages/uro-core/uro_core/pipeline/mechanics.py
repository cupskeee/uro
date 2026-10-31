"""The mechanics gate (docs/05 stage [3], 06). Deterministic — no LLM.

Given a validated `BeatPlan`, resolve each free-roam affordance the planner invoked into a
`CheckResult` via the bound ruleset, using the acting actor's OPAQUE character sheet and the
beat's seeded `Rng`. The gate is game-agnostic: it hands the ruleset the affordance's stat +
difficulty hint + the raw sheet dict and lets `resolve_check` own all resolution (the ruleset
decides DC-vs-2d6, binary-vs-graded). Each result's `trace` feeds the narrator; a check that
changed its actor's sheet (a push's strain, XP on a miss) returns the new sheet, which the gate
threads into that actor's next check and hands back for the engine to commit as `SheetUpdated`
(D-61). What the narration says happened beyond that becomes canon only through the extractor.
Encounter-starting affordances are detected here but resolved by the encounter loop (docs/06,
D-29), not as a single check.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.rulesets.base import CheckRequest, CheckResult, Ruleset
from uro_core.rulesets.rng import Rng


class MechanicsResult(BaseModel):
    checks: list[CheckResult] = Field(default_factory=list)
    # actor_id → that actor's sheet after this beat's checks, for every actor a check changed
    sheets: dict[str, dict[str, Any]] = Field(default_factory=dict)


def encounter_trigger(ruleset: Ruleset, plan: BeatPlan) -> PlanMechanic | None:
    """The first plan mechanic that invokes an encounter-starting affordance, or None. The
    encounter loop calls this and acts on it (docs/06, D-29)."""
    starters = {a.id for a in ruleset.affordances() if a.starts_encounter}
    return next((m for m in plan.mechanics if m.affordance in starters), None)


def resolve_mechanics(
    ruleset: Ruleset,
    plan: BeatPlan,
    sheets: dict[str, dict[str, Any]],
    pc_actor_id: str,
    rng: Rng,
) -> MechanicsResult:
    """Resolve the plan's NON-encounter affordance checks. `sheets` maps actor_id → opaque sheet
    dict (not modified); a mechanic on an unsheeted actor is skipped (nothing to roll). A check
    that changed its actor's sheet is threaded into that actor's NEXT check — a second push sees
    the first one's strain. Deterministic: every draw is from `rng`, so the same seed + plan
    reproduces the same results."""
    aff_by_id = {a.id: a for a in ruleset.affordances()}
    current = dict(sheets)
    out = MechanicsResult()
    for m in plan.mechanics:
        aff = aff_by_id.get(m.affordance)
        if aff is None or aff.starts_encounter:
            continue  # unknown (validation should have caught) or encounter-starting (the loop)
        actor_id = m.actor or pc_actor_id
        sheet = current.get(actor_id)
        if sheet is None:
            continue  # unsheeted actor — no check to make
        req = CheckRequest(sheet=sheet, stat=aff.stat, difficulty=aff.difficulty)
        result = ruleset.resolve_check(req, rng)
        out.checks.append(result)
        if result.sheet is not None and result.sheet != sheet:
            current[actor_id] = out.sheets[actor_id] = result.sheet
    return out
