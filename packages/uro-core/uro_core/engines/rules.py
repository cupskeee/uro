"""Reaction-Layer interpreter (docs/17, D-33) — evaluate pack rules into ACTION proposals.

Pure and in-ring (imports only ports + the pure rule grammar): reads the projection view via
`ProjectionQueries`, evaluates the closed/total condition tree, and returns the matched rules'
ACTIONs for the gauntlet (`engines.rules_gauntlet`) to turn into events. It never writes, and it
runs once at live-play time — its whole effect is committed as events, so replay never re-runs it.
A deterministic node budget bounds cost (fail-closed: the pass is dropped, the beat still commits).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from uro_core.ports.projections import ProjectionQueries
from uro_core.worldpack.rules import (
    Action,
    AgendaRule,
    CmpOp,
    Condition,
    Rule,
    Scope,
    Trigger,
)

# Deterministic fuel: total condition-node evaluations allowed per pass (a DoS guard for the
# shared multi-campaign process; NOT wall-clock, so it trips identically on every machine).
_MAX_NODES = 512


class RuleBudgetExceeded(Exception):
    """A rule pass exhausted its evaluation budget — fail closed (drop the pass)."""


@dataclass(frozen=True)
class FiredAction:
    """A rule whose trigger + condition matched, paired with one of its actions (carrying the
    rule's id + scope for the gauntlet, and the action's index for a deterministic id).
    `trigger_payload` is the matching trigger event's payload — the `$trigger.<field>` source for
    a `when` condition (RL-6) and for for_each (C3); empty for agenda rules. `event_key` is set only
    for a `per_event` rule (one FiredAction-set per matching trigger event) — it namespaces the
    gauntlet id_path so per-event claim/counter ids stay distinct + idempotent; empty (the default)
    keeps the id_path byte-identical to a fire-once rule."""

    rule_id: str
    scope: Scope
    action: Action
    index: int
    trigger_payload: dict[str, Any] = field(default_factory=dict)
    event_key: str = ""


class _UnboundTrigger(Exception):
    """A `$trigger.<field>` ref in a `when` condition had no value in the trigger payload — the
    whole `when` fails CLOSED (caught at the rule level), so a `not`-wrapped unbound ref can't
    fail open."""


def _resolve_trigger(ref: str, payload: dict[str, Any]) -> str | None:
    """Resolve a `$trigger.<field>` reference against the triggering event's payload (RL-6/C3), or
    return a literal ref unchanged. None if the field is absent OR present-but-null (a nullable
    payload field like BeliefChanged.learned_from on a first-hand witness) - so the caller drops it
    / fails the condition closed rather than binding the literal "None" (which would fail OPEN under
    a `not`)."""
    if ref.startswith("$trigger."):
        value = payload.get(ref[len("$trigger.") :])
        return str(value) if value is not None else None
    return ref


def _bind(ref: str, payload: dict[str, Any]) -> str:
    """Resolve a condition ref slot, raising `_UnboundTrigger` (→ whole-`when` fail-closed) if a
    `$trigger.<field>` has no payload value. A literal ref passes through unchanged."""
    resolved = _resolve_trigger(ref, payload)
    if resolved is None:
        raise _UnboundTrigger()
    return resolved


def _cmp(a: int, op: CmpOp, b: int) -> bool:
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a < b  # "<"


def _matching_triggers(trigger: Trigger, trigger_events: list):  # type: ignore[no-untyped-def, type-arg]
    """ALL beat events of the trigger type whose payload matches every `where` field, in beat order
    (RL-6; string-compared - the grammar is int/string/bool, all stringifiable). `when` is evaluated
    per matching event so a `$trigger`-bound predicate is an EXISTENTIAL over them ("ANY member
    died"), not just a test of the first event."""
    return [
        e
        for e in trigger_events
        if e.event_type == trigger.event
        and all(str(e.payload.get(k)) == v for k, v in trigger.where.items())
    ]


def _uses_trigger(cond: Condition | None) -> bool:
    """True if any slot in the condition tree is a `$trigger.<field>` — i.e. the `when` result
    depends on WHICH trigger event bound it. A `$trigger`-free `when` is constant across a beat's
    matching events, so evaluate_rules evaluates it ONCE instead of once per event (bounding the
    shared node budget to its pre-RL-6 O(rules) cost). Parse guarantees $trigger only in ref slots,
    so scanning every string field is safe."""
    if cond is None:
        return False
    kind = cond.kind
    if kind == "all":
        return any(_uses_trigger(c) for c in cond.all)  # type: ignore[union-attr]
    if kind == "any":
        return any(_uses_trigger(c) for c in cond.any)  # type: ignore[union-attr]
    if kind == "not":
        return _uses_trigger(cond.cond)  # type: ignore[union-attr]
    if kind == "counter_compare":
        return cond.left.scope_ref.startswith("$trigger.") or cond.right.scope_ref.startswith(  # type: ignore[union-attr]
            "$trigger."
        )
    return any(
        isinstance(v, str) and v.startswith("$trigger.")
        for v in (getattr(cond, n) for n in type(cond).model_fields)
    )


async def _eval(
    store: ProjectionQueries,
    branch_id: str,
    cond: Condition,
    world_day: int,
    budget: list[int],
    trigger_payload: dict[str, Any],
) -> bool:
    """Evaluate a condition. Entity-ref slots (actor/src/dst/scope_ref/thread) may be a
    `$trigger.<field>` bound from `trigger_payload` (RL-6) — resolved via `_bind`, which raises
    `_UnboundTrigger` on an absent field so the WHOLE `when` fails closed (caught by the caller).
    Literal refs and non-`$trigger` conditions behave exactly as before (an empty payload is a no-op
    for a literal ref), so pre-RL-6 packs are byte-identical."""
    budget[0] -= 1
    if budget[0] < 0:
        raise RuleBudgetExceeded("condition tree exceeded the evaluation budget")
    kind = cond.kind
    if kind == "all":
        for c in cond.all:  # type: ignore[union-attr]
            if not await _eval(store, branch_id, c, world_day, budget, trigger_payload):
                return False
        return True
    if kind == "any":
        for c in cond.any:  # type: ignore[union-attr]
            if await _eval(store, branch_id, c, world_day, budget, trigger_payload):
                return True
        return False
    if kind == "not":
        return not await _eval(store, branch_id, cond.cond, world_day, budget, trigger_payload)  # type: ignore[union-attr]
    if kind == "thread_state":
        thread = _bind(cond.thread, trigger_payload)  # type: ignore[union-attr]
        threads = await store.list_threads(branch_id)
        return any(t.thread_id == thread and t.state == cond.state for t in threads)  # type: ignore[union-attr]
    if kind == "actor_tier":
        actor = await store.get_actor(branch_id, _bind(cond.actor, trigger_payload))  # type: ignore[union-attr]
        return actor is not None and _cmp(actor.tier, cond.op, cond.value)  # type: ignore[union-attr]
    if kind == "actor_is_pc":
        return await store.is_pc(branch_id, _bind(cond.actor, trigger_payload))  # type: ignore[union-attr]
    if kind == "edge_exists":
        src = _bind(cond.src, trigger_payload)  # type: ignore[union-attr]
        dst = _bind(cond.dst, trigger_payload)  # type: ignore[union-attr]
        edges = await store.edges_from(branch_id, src)
        return any(e.rel_type == cond.rel and e.dst == dst for e in edges)  # type: ignore[union-attr]
    if kind == "world_day":
        return _cmp(world_day, cond.op, cond.value)  # type: ignore[union-attr]
    if kind == "counter":
        scope = _bind(cond.scope_ref, trigger_payload)  # type: ignore[union-attr]
        value = await store.get_counter(branch_id, scope, cond.key)  # type: ignore[union-attr]
        return _cmp(value, cond.op, cond.value)  # type: ignore[union-attr]
    if kind == "counter_compare":
        lscope = _bind(cond.left.scope_ref, trigger_payload)  # type: ignore[union-attr]
        rscope = _bind(cond.right.scope_ref, trigger_payload)  # type: ignore[union-attr]
        left = await store.get_counter(branch_id, lscope, cond.left.key)  # type: ignore[union-attr]
        right = await store.get_counter(branch_id, rscope, cond.right.key)  # type: ignore[union-attr]
        # integer cross-multiply — left * left_mul OP right * right_mul (no float)
        return _cmp(left * cond.left_mul, cond.op, right * cond.right_mul)  # type: ignore[union-attr]
    if kind == "count_edges":
        edges = await store.edges_from(branch_id, _bind(cond.src, trigger_payload))  # type: ignore[union-attr]
        n = len([e for e in edges if e.rel_type == cond.rel])  # type: ignore[union-attr]
        return _cmp(n, cond.op, cond.value)  # type: ignore[union-attr]
    return False  # unreachable — the union is closed


async def evaluate_rules(
    store: ProjectionQueries,
    branch_id: str,
    *,
    rules: list[Rule],
    trigger_events: list,  # type: ignore[type-arg]
    world_day: int,
) -> list[FiredAction]:
    """Fire every rule whose trigger event is in the beat AND whose condition holds. Rules are
    evaluated in total order by id (decided-OQ #3 — deterministic; single-pack, so no jurisdiction
    contention yet). Returns the fired actions in that order; the gauntlet validates + commits.

    `when` is evaluated PER matching trigger event, bound to that event's payload (RL-6), so a
    `$trigger.<field>` predicate quantifies over the beat's events. A rule fires ONCE bound to the
    first event satisfying trigger-and-when (the existential default); with `trigger.per_event`,
    once per such event (count-each) - each keyed by `event_key` for distinct, idempotent ids."""
    budget = [_MAX_NODES]
    fired: list[FiredAction] = []
    for rule in sorted(rules, key=lambda r: r.id):
        matches = _matching_triggers(rule.trigger, trigger_events)
        if not matches:
            continue
        per_event = rule.trigger.per_event
        satisfying: list[dict[str, object]] = []
        if rule.when is not None and not _uses_trigger(rule.when):
            # A $trigger-free `when` is constant across the matching events: evaluate it ONCE (not
            # once per event) so a many-event beat can't exhaust the shared node budget - keeping a
            # pre-RL-6 pack's cost O(rules), not O(rules x events). No $trigger = no unbound raise.
            if await _eval(store, branch_id, rule.when, world_day, budget, {}):
                payloads = [dict(e.payload) for e in matches]
                satisfying = payloads if per_event else payloads[:1]
        else:
            for ev in matches:
                payload = dict(ev.payload)
                if rule.when is not None:
                    try:
                        held = await _eval(store, branch_id, rule.when, world_day, budget, payload)
                    except _UnboundTrigger:  # unbound $trigger -> the WHOLE `when` fails closed
                        held = False
                    if not held:
                        continue
                satisfying.append(payload)
                if not per_event:
                    break  # fire-once: the first satisfying event is enough
        for ev_i, payload in enumerate(satisfying):
            event_key = str(ev_i) if per_event else ""
            for i, action in enumerate(rule.then):
                fired.append(
                    FiredAction(
                        rule_id=rule.id,
                        scope=rule.scope,
                        action=action,
                        index=i,
                        trigger_payload=payload,  # $trigger.<field> source for `when` + for_each
                        event_key=event_key,
                    )
                )
    return fired


async def evaluate_agendas(
    store: ProjectionQueries,
    branch_id: str,
    *,
    agendas: list[AgendaRule],
    from_day: int,
    to_day: int,
) -> list[FiredAction]:
    """Fire agenda rules whose cadence boundary was crossed by a time advance from `from_day` to
    `to_day` AND whose condition holds at the post-skip state (INC-4 downtime tick). Deterministic:
    a rule fires once per tick if `to_day // every_days > from_day // every_days` (at least one
    boundary crossed) — bounded regardless of skip size. Total order by id."""
    budget = [_MAX_NODES]
    fired: list[FiredAction] = []
    for rule in sorted(agendas, key=lambda r: r.id):
        if to_day // rule.every_days <= from_day // rule.every_days:
            continue  # no cadence boundary crossed in this skip
        # Agenda rules have no trigger event, so `$trigger` refs are parse-rejected → empty payload.
        if rule.when is not None and not await _eval(
            store, branch_id, rule.when, to_day, budget, {}
        ):
            continue
        for i, action in enumerate(rule.then):
            fired.append(FiredAction(rule_id=rule.id, scope=rule.scope, action=action, index=i))
    return fired
