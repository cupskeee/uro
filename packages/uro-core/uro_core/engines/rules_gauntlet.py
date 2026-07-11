"""Reaction-Layer gauntlet (docs/17, D-33) — validate ACTION proposals into a safe event set.

The trusted counterpart to the interpreter, sibling to `run_gauntlet` / `distill_outcome`: pure,
in-ring, reads via `ProjectionQueries`, returns `list[DomainEvent]` for the caller to commit. It
enforces the Phase-8/D-32 bar on untrusted pack rules:

- STRUCTURAL (already done by the grammar): the ACTION union cannot name a mechanical/lethal/canon
  event — so there is nothing here that kills, loots, mutates a sheet, or mints an actor. The
  protection ceiling (`chronicler._is_protected`) is therefore VACUOUSLY satisfied for Stage A's
  action set: no action is destructive to a PC/T2+ actor. (A rumor ABOUT or an edge BETWEEN
  powerful actors is allowed — the critic's per-action point; blanket-blocking them was wrong.)
- SCOPE FENCE: every entity a rule touches must lie in the rule's declared jurisdiction (its
  thread / a faction's members / a place's occupants) — the D-32 participant-scoping generalized.
- EXISTENCE, NEVER-MINT: a state change targets an existing thread; a rumor/belief resolves to
  known entities. A thread MAY be created (a thread is not an actor); actors never are.
- FORCED TESTIMONY: a rumor commits `truth=unknown`, `origin=module`; belief spread carries
  `caused_by=module`. Untrusted rules cannot assert canon.
- CAPS + DETERMINISTIC IDS: bounded actions/witnesses per pass; claim ids keyed on the trigger
  commit → idempotent on replay.
"""

from __future__ import annotations

import logging

from uro_core.domain.events import (
    DomainEvent,
    claim_recorded,
    counter_changed,
    edge_added,
    edge_removed,
    module_cause,
    thread_created,
    thread_state_changed,
)
from uro_core.engines.actor import propagate_belief
from uro_core.engines.rules import FiredAction
from uro_core.ports.projections import ProjectionQueries
from uro_core.worldpack.rules import Scope

logger = logging.getLogger(__name__)

_MAX_ACTIONS = 32  # per pass — a bundle cap (multi-campaign DoS guard)
_MAX_WITNESSES = 64  # per spread_belief
_MAX_COUNTER = (
    1_000_000_000  # magnitude cap (docs/19 D-34): unbounded accumulation is the DoS vector
)


async def _scope_refs(store: ProjectionQueries, branch_id: str, scope: Scope) -> set[str]:
    """The set of entity refs a rule may touch — its jurisdiction. A thread scope is just that
    thread; a faction scope is the faction + its members; a place scope is the place + its
    occupants. Any emitted ref outside this set is dropped."""
    if scope.thread is not None:
        return {scope.thread}
    if scope.faction is not None:
        edges = await store.list_edges(branch_id, "member_of")
        return {scope.faction} | {e.src for e in edges if e.dst == scope.faction}
    if scope.place is not None:
        edges = await store.list_edges(branch_id, "located_in")
        return {scope.place} | {e.src for e in edges if e.dst == scope.place}
    return set()


def _clamp(v: int) -> int:
    return max(-_MAX_COUNTER, min(_MAX_COUNTER, v))


async def _translate(
    store: ProjectionQueries,
    branch_id: str,
    fired: FiredAction,
    allowed: set[str],
    trigger_commit: str,
    pending: dict[tuple[str, str], int],
    world_day: int,
) -> list[DomainEvent]:
    a = fired.action
    cause = module_cause(fired.rule_id)
    if a.do in ("set_counter", "adjust_counter", "reset_counter"):
        # Computation Layer (docs/19, D-34): scope-fence the write, accumulate within the pass
        # (read-your-writes so two adjusts to one key both count), clamp fail-closed, emit ABSOLUTE.
        if a.scope_ref not in allowed:
            logger.warning(
                "rule %r: counter write to %r dropped (out of scope)", fired.rule_id, a.scope_ref
            )
            return []
        k = (a.scope_ref, a.key)
        if k not in pending:  # seed the running value from committed state, once per pass
            pending[k] = await store.get_counter(branch_id, a.scope_ref, a.key)
        if a.do == "set_counter":
            new_value = a.value
        elif a.do == "reset_counter":
            new_value = 0
        else:  # adjust_counter
            new_value = pending[k] + a.delta
        clamped = _clamp(new_value)
        if clamped != new_value:
            logger.warning("rule %r: counter %s clamped to +/-_MAX_COUNTER", fired.rule_id, k)
        pending[k] = clamped
        return [
            counter_changed(
                scope_ref=a.scope_ref,
                key=a.key,
                to_value=clamped,
                created_day=world_day,
                updated_day=world_day,
                caused_by=cause,
            )
        ]
    if a.do == "set_thread_state":
        if a.thread not in allowed:
            return []
        exists = any(t.thread_id == a.thread for t in await store.list_threads(branch_id))
        if not exists:  # never mint a thread via a state change
            return []
        return [thread_state_changed(thread_id=a.thread, to_state=a.to, caused_by=cause)]
    if a.do == "create_thread":
        if a.thread not in allowed:
            return []
        if any(t.thread_id == a.thread for t in await store.list_threads(branch_id)):
            return []  # idempotent — already created
        return [
            thread_created(
                thread_id=a.thread,
                stakes=a.stakes,
                state="dormant",
                provenance="module",
                caused_by=cause,
            )
        ]
    if a.do in ("add_edge", "remove_edge"):
        if a.src not in allowed or a.dst not in allowed:  # both ends in jurisdiction
            return []
        ctor = edge_added if a.do == "add_edge" else edge_removed
        return [ctor(src=a.src, rel_type=a.rel, dst=a.dst, caused_by=cause)]
    if a.do == "record_rumor":
        subjects = [s for s in a.subjects if s in allowed]
        if a.subjects and not subjects:  # a rumor whose every subject is out of scope is dropped
            return []
        claim_id = f"m:{trigger_commit}:{fired.rule_id}:{fired.index}"  # deterministic → idempotent
        return [
            claim_recorded(
                claim_id=claim_id,
                statement=a.text,
                subject_refs=subjects,
                truth="unknown",
                origin="module",
                caused_by=cause,  # never canon
            )
        ]
    if a.do == "spread_belief":
        if await store.get_claim(branch_id, a.claim) is None:
            return []  # never spread a belief about a nonexistent claim
        witnesses = [w for w in a.witnesses[:_MAX_WITNESSES] if w in allowed]
        if not witnesses:
            return []
        return await propagate_belief(
            store, branch_id, claim_id=a.claim, witnesses=witnesses, caused_by=cause
        )
    return []  # unreachable — the union is closed


async def run_rules_gauntlet(
    store: ProjectionQueries,
    branch_id: str,
    fired: list[FiredAction],
    *,
    trigger_commit: str,
) -> list[DomainEvent]:
    """Turn fired ACTION proposals into a bounded, safe event set (dropped actions contribute
    nothing). Deterministic: preserves the interpreter's total order; ids key on trigger_commit."""
    events: list[DomainEvent] = []
    pending: dict[tuple[str, str], int] = {}  # in-pass counter accumulation (read-your-writes)
    world_day = await store.current_world_time(branch_id)
    for f in fired[:_MAX_ACTIONS]:
        allowed = await _scope_refs(store, branch_id, f.scope)
        events.extend(
            await _translate(store, branch_id, f, allowed, trigger_commit, pending, world_day)
        )
    return events
