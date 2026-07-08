"""The planner stage: schema, prompt, and deterministic plan validation (docs/05, 13, D-28).

The planner (an LLM role) classifies the player's intent and decides which ruleset
AFFORDANCES it invokes — without knowing the math (docs/06). Its output is a `BeatPlan`.
Between plan and mechanics sits deterministic **plan validation** (no LLM): affordances are
fenced to the ruleset's vocabulary, referenced actors must exist, and any trigger category
the planner recognizes MUST be invoked (D-21) — a check can't be dodged by phrasing. This is
the ONLY replanning point (nothing has streamed yet); a violation re-asks the planner.

Honest caveat (docs/13): validation sees only the planner's OWN structured paraphrase, so a
MISCLASSIFIED intent (planner omits the trigger) still slips through — consequence gating is
the intended backstop and is not built yet. So triggers are enforced *given honest
classification*, not by construction.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.pipeline.recall import RecallBundle
from uro_core.providers.base import Message
from uro_core.rulesets.base import Affordance

IntentClass = Literal["dialogue", "action", "movement", "examine", "meta"]
Mode = Literal["freeroam", "encounter", "downtime"]


class PlanMechanic(BaseModel):
    affordance: str
    actor: str = ""  # actor id performing the check; "" → the player character
    target: str = ""
    context: str = ""


class ModeTransition(BaseModel):
    to: Mode
    cause: str = ""


class BeatPlan(BaseModel):
    """Planner output (docs/13 BeatPlan v0, + `triggers` for deterministic D-21 enforcement)."""

    intent_class: IntentClass = "action"
    targets: list[str] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)  # trigger categories the intent falls into
    mechanics: list[PlanMechanic] = Field(default_factory=list)
    # Advisory only — the pipeline decides the encounter transition from the invoked
    # `starts_encounter` affordance (mechanics-gated, D-21/D-26), NOT from this field.
    mode_transition: ModeTransition | None = None
    time_cost: int = 0
    narration_directives: str = ""
    suggestions: list[str] = Field(default_factory=list)


def build_planner_messages(
    affordances: list[Affordance],
    recall: RecallBundle,
    pc_actor_id: str,
    intent_text: str,
    *,
    env: PromptEnv | None = None,
) -> list[Message]:
    aff_lines = "\n".join(
        f"- {a.id} (ability {a.ability}, triggers {a.trigger_categories}"
        f"{', STARTS AN ENCOUNTER' if a.starts_encounter else ''}): {a.description}"
        for a in affordances
    )
    present = (
        "\n".join(f"- {a.name} [{a.actor_id}] ({a.role})" for a in recall.actors)
        or "(no named NPCs on stage)"
    )
    user = (
        f"YOU (the player character) ARE actor {pc_actor_id or '(unknown)'}.\n\n"
        f"AFFORDANCES:\n{aff_lines}\n\nON STAGE:\n{present}\n\n"
        f"PLAYER INTENT: {intent_text}\n\n"
        'Return JSON: {"intent_class": "dialogue|action|movement|examine|meta", '
        '"targets": [ids], "triggers": [categories], '
        '"mechanics": [{"affordance","actor","target","context"}], '
        '"mode_transition": null or {"to":"encounter","cause":""}, '
        '"time_cost": 0, "narration_directives": "one line of pacing", "suggestions": ["..."]}'
    )
    system = (env or DEFAULT_ENV).render("planner.system.j2")
    return [Message(role="system", content=system), Message(role="user", content=user)]


_INTENT_CLASSES = frozenset(("dialogue", "action", "movement", "examine", "meta"))
_MODES = frozenset(("freeroam", "encounter", "downtime"))


def _coerce_plan(data: dict[str, Any]) -> dict[str, Any]:
    """Salvage a near-miss planner output before validation. A small model (found live: gpt-4o-mini
    failing ~half of freeform beats) often emits an out-of-vocab `intent_class` ("conversation"),
    a stringified `mode_transition`, or a mechanics entry missing `affordance` — each of which trips
    a strict Literal/required-field and fails the WHOLE beat. The planner is a best-effort structure
    hint; the real enforcement is validate_plan + the mechanics gate, so coerce these rather than
    lose the beat. An unrecoverable value falls back to a safe default (the plan then simply invokes
    no mechanics)."""
    d = dict(data)
    if d.get("intent_class") not in _INTENT_CLASSES:
        d["intent_class"] = "action"
    mt = d.get("mode_transition")
    if not (isinstance(mt, dict) and mt.get("to") in _MODES):
        d["mode_transition"] = None
    mech = d.get("mechanics")
    d["mechanics"] = [
        m
        for m in (mech if isinstance(mech, list) else [])
        if isinstance(m, dict) and isinstance(m.get("affordance"), str) and m["affordance"]
    ]
    for key in ("targets", "speakers", "triggers", "suggestions"):
        v = d.get(key)
        d[key] = [v] if isinstance(v, str) else v if isinstance(v, list) else []
    return d


def parse_plan(raw: str) -> BeatPlan | None:
    """Parse a planner response into a validated BeatPlan, or None if unusable. Tolerant of a
    small model's near-misses (see _coerce_plan) — the planner's structure is advisory."""
    text = raw.strip()
    for candidate in (text, _slice_json_object(text)):
        if candidate is None:
            continue
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        try:
            return BeatPlan.model_validate(_coerce_plan(data))
        except Exception:
            continue
    return None


def _slice_json_object(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


def _is_actor_ref(ref: str) -> bool:
    return ref.startswith("a:") or ref.startswith("actor:")


def validate_plan(
    plan: BeatPlan, affordances: list[Affordance], known_actor_ids: set[str]
) -> list[str]:
    """Deterministic plan validation (docs/13). Returns a list of errors (empty = valid); a
    non-empty list drives a planner re-ask. Enforces the affordance-vocabulary fence, ACTOR
    existence, and D-21 trigger coverage. Scope (honest): only actor-typed refs are checked —
    there is no place/item registry yet, and docs/05's 'presupposed facts must not be false'
    check is deferred."""
    aff_by_id = {a.id: a for a in affordances}
    errors: list[str] = []

    for m in plan.mechanics:
        if m.affordance not in aff_by_id:
            errors.append(f"unknown affordance {m.affordance!r} (not declared by the ruleset)")
        for ref in (m.actor, m.target):
            if ref and _is_actor_ref(ref) and ref not in known_actor_ids:
                errors.append(f"mechanic references unknown actor {ref!r}")
    for t in plan.targets:
        if _is_actor_ref(t) and t not in known_actor_ids:
            errors.append(f"target references unknown actor {t!r}")

    # D-21: every trigger category the planner recognized must be invoked by some affordance.
    # ONLY the ruleset's DECLARED trigger vocabulary is governed — a small model routinely invents
    # non-mechanical categories ("social", "movement"; found live). Those can't dodge a check (no
    # affordance, no check), so ignoring them preserves D-21 for real triggers while not failing a
    # beat over the planner's mislabelling.
    declared = {t for a in affordances for t in a.trigger_categories}
    for trig in plan.triggers:
        if trig not in declared:
            continue
        if not any(
            trig in aff_by_id[m.affordance].trigger_categories
            for m in plan.mechanics
            if m.affordance in aff_by_id
        ):
            errors.append(f"intent triggers {trig!r} but no affordance invokes it (D-21)")

    return errors
