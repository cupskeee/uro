"""The mechanics gate (docs/05 stage [3], 06). Deterministic — no LLM.

Given a validated `BeatPlan`, resolve each free-roam affordance the planner invoked into a
`CheckResult` via the bound ruleset, using the acting actor's character sheet and the beat's
seeded `Rng`. The results feed the narrator (whose prose weaves the roll traces) but are not
themselves committed events — a free-roam check's *consequences* become canon only if the
extractor canonicalizes the narrated outcome. Encounter-starting affordances (attack) are
detected here but resolved by the encounter loop (inc 3.3), not as a single check.
"""

from __future__ import annotations

from typing import Any

from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.rulesets.base import CheckRequest, CheckResult, Ruleset, Sheet
from uro_core.rulesets.rng import Rng


def encounter_trigger(ruleset: Ruleset, plan: BeatPlan) -> PlanMechanic | None:
    """The first plan mechanic that invokes an encounter-starting affordance, or None. Inc 3.2
    DEFINES this detector; the encounter loop (inc 3.3) is what calls it and acts on it."""
    starters = {a.id for a in ruleset.affordances() if a.starts_encounter}
    return next((m for m in plan.mechanics if m.affordance in starters), None)


def resolve_mechanics(
    ruleset: Ruleset,
    plan: BeatPlan,
    sheets: dict[str, dict[str, Any]],
    pc_actor_id: str,
    rng: Rng,
) -> list[CheckResult]:
    """Resolve the plan's NON-encounter affordance checks. `sheets` maps actor_id → raw sheet;
    a mechanic on an unsheeted actor is skipped (nothing to roll). Deterministic: every draw is
    from `rng`, so the same seed + plan reproduces the same results."""
    aff_by_id = {a.id: a for a in ruleset.affordances()}
    results: list[CheckResult] = []
    for m in plan.mechanics:
        aff = aff_by_id.get(m.affordance)
        if aff is None or aff.starts_encounter:
            continue  # unknown (validation should have caught) or encounter-starting (inc 3.3)
        actor_id = m.actor or pc_actor_id
        sheet_dict = sheets.get(actor_id)
        if sheet_dict is None:
            continue  # unsheeted actor — no check to make
        sheet = Sheet.model_validate(sheet_dict)
        req = CheckRequest(sheet=sheet, ability=aff.ability, dc=ruleset.dc_for(aff.difficulty))
        results.append(ruleset.resolve_check(req, rng))
    return results
