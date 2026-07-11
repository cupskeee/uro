"""Reaction-Layer interpreter (docs/17, D-33) — evaluate pack rules into ACTION proposals.

Pure and in-ring (imports only ports + the pure rule grammar): reads the projection view via
`ProjectionQueries`, evaluates the closed/total condition tree, and returns the matched rules'
ACTIONs for the gauntlet (`engines.rules_gauntlet`) to turn into events. It never writes, and it
runs once at live-play time — its whole effect is committed as events, so replay never re-runs it.
A deterministic node budget bounds cost (fail-closed: the pass is dropped, the beat still commits).
"""

from __future__ import annotations

from dataclasses import dataclass

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
    rule's id + scope for the gauntlet, and the action's index for a deterministic id)."""

    rule_id: str
    scope: Scope
    action: Action
    index: int


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


def _trigger_matches(trigger: Trigger, trigger_events: list) -> bool:  # type: ignore[type-arg]
    """True if the beat committed an event of the trigger type whose payload matches every
    `where` field (string-compared — the grammar is int/string/bool, all stringifiable)."""
    for e in trigger_events:
        if e.event_type != trigger.event:
            continue
        if all(str(e.payload.get(k)) == v for k, v in trigger.where.items()):
            return True
    return False


async def _eval(
    store: ProjectionQueries, branch_id: str, cond: Condition, world_day: int, budget: list[int]
) -> bool:
    budget[0] -= 1
    if budget[0] < 0:
        raise RuleBudgetExceeded("condition tree exceeded the evaluation budget")
    kind = cond.kind
    if kind == "all":
        for c in cond.all:  # type: ignore[union-attr]
            if not await _eval(store, branch_id, c, world_day, budget):
                return False
        return True
    if kind == "any":
        for c in cond.any:  # type: ignore[union-attr]
            if await _eval(store, branch_id, c, world_day, budget):
                return True
        return False
    if kind == "not":
        return not await _eval(store, branch_id, cond.cond, world_day, budget)  # type: ignore[union-attr]
    if kind == "thread_state":
        threads = await store.list_threads(branch_id)
        return any(t.thread_id == cond.thread and t.state == cond.state for t in threads)  # type: ignore[union-attr]
    if kind == "actor_tier":
        actor = await store.get_actor(branch_id, cond.actor)  # type: ignore[union-attr]
        return actor is not None and _cmp(actor.tier, cond.op, cond.value)  # type: ignore[union-attr]
    if kind == "actor_is_pc":
        return await store.is_pc(branch_id, cond.actor)  # type: ignore[union-attr]
    if kind == "edge_exists":
        edges = await store.edges_from(branch_id, cond.src)  # type: ignore[union-attr]
        return any(e.rel_type == cond.rel and e.dst == cond.dst for e in edges)  # type: ignore[union-attr]
    if kind == "world_day":
        return _cmp(world_day, cond.op, cond.value)  # type: ignore[union-attr]
    if kind == "counter":
        value = await store.get_counter(branch_id, cond.scope_ref, cond.key)  # type: ignore[union-attr]
        return _cmp(value, cond.op, cond.value)  # type: ignore[union-attr]
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
    contention yet). Returns the fired actions in that order; the gauntlet validates + commits."""
    budget = [_MAX_NODES]
    fired: list[FiredAction] = []
    for rule in sorted(rules, key=lambda r: r.id):
        if not _trigger_matches(rule.trigger, trigger_events):
            continue
        if rule.when is not None and not await _eval(
            store, branch_id, rule.when, world_day, budget
        ):
            continue
        for i, action in enumerate(rule.then):
            fired.append(FiredAction(rule_id=rule.id, scope=rule.scope, action=action, index=i))
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
        if rule.when is not None and not await _eval(store, branch_id, rule.when, to_day, budget):
            continue
        for i, action in enumerate(rule.then):
            fired.append(FiredAction(rule_id=rule.id, scope=rule.scope, action=action, index=i))
    return fired
