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
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class DroppedAction:
    """An action the gauntlet REFUSED to apply, with why (docs/18 B11, D-40) — the dropped-action
    audit trail. Before this, a fenced/nonexistent action `return []`-ed SILENTLY, so a pack author
    (Sable/Ironwake/Seventh hit this) could not tell a working rule from a no-op one. Not canon and
    not an event — a pure diagnostic: the caller (react/agenda_tick) logs it, and it is returned in
    `GauntletResult.drops` for tests / a future creator-loop surface."""

    rule_id: str
    do: str  # the action verb (create_thread / add_edge / …); "*" for a whole-pass drop (the cap)
    ref: str  # the offending entity ref (or "" when not ref-specific)
    reason: str  # e.g. "out of scope", "thread does not exist"


@dataclass
class GauntletResult:
    """What a gauntlet pass produced: the safe events to commit, plus the dropped-action audit."""

    events: list[DomainEvent] = field(default_factory=list)
    drops: list[DroppedAction] = field(default_factory=list)


_MAX_ACTIONS = 32  # per pass — a bundle cap (multi-campaign DoS guard)
_MAX_WITNESSES = 64  # per spread_belief
_MAX_COUNTER = (
    1_000_000_000  # magnitude cap (docs/19 D-34): unbounded accumulation is the DoS vector
)


async def _scope_refs(store: ProjectionQueries, branch_id: str, scope: Scope) -> set[str] | None:
    """The set of entity refs a rule may touch — its jurisdiction. `None` means UNRESTRICTED (a
    `world` scope, docs/19 C2). A thread scope is those threads; a faction scope is the factions +
    their members; a place scope is the places + their occupants (MULTI-REF, D-40: the plural forms
    union across several entities of one category). Any emitted ref outside the set is dropped (the
    action fence still applies regardless — a world rule still cannot mint canon)."""
    if scope.world:
        return None  # whole-realm jurisdiction — takes precedence
    cats = scope.refs()  # singular folded into plural (Scope.refs); exactly one is non-empty
    if cats["thread"]:
        return set(cats["thread"])
    if cats["faction"]:
        factions = set(cats["faction"])
        edges = await store.list_edges(branch_id, "member_of")
        return factions | {e.src for e in edges if e.dst in factions}
    if cats["place"]:
        places = set(cats["place"])
        edges = await store.list_edges(branch_id, "located_in")
        return places | {e.src for e in edges if e.dst in places}
    return set()  # unreachable (the Scope validator requires a jurisdiction) — safe: drops all


def _in_scope(allowed: set[str] | None, ref: str) -> bool:
    """A ref is in jurisdiction if the scope is unrestricted (world → None) or names it."""
    return allowed is None or ref in allowed


def _drop(
    drops: list[DroppedAction], fired: FiredAction, ref: str, reason: str
) -> list[DomainEvent]:
    """Record a refused action, then return no events — the dropped-action audit (B11, D-40). The
    gauntlet stays PURE (no logging side-effect): the caller logs the batch (react/agenda_tick emit
    one summary line per pass), avoiding a double-log."""
    drops.append(DroppedAction(rule_id=fired.rule_id, do=fired.action.do, ref=ref, reason=reason))
    return []


def _clamp(v: int) -> int:
    return max(-_MAX_COUNTER, min(_MAX_COUNTER, v))


async def _translate(
    store: ProjectionQueries,
    branch_id: str,
    fired: FiredAction,
    allowed: set[str] | None,
    trigger_commit: str,
    pending: dict[tuple[str, str], int],
    world_day: int,
    drops: list[DroppedAction],
) -> list[DomainEvent]:
    a = fired.action
    cause = module_cause(fired.rule_id)
    if a.do in ("set_counter", "adjust_counter", "reset_counter"):
        # Computation Layer (docs/19, D-34): scope-fence the write, accumulate within the pass
        # (read-your-writes so two adjusts to one key both count), clamp fail-closed, emit ABSOLUTE.
        if not _in_scope(allowed, a.scope_ref):
            return _drop(drops, fired, a.scope_ref, "out of scope")
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
        if not _in_scope(allowed, a.thread):
            return _drop(drops, fired, a.thread, "out of scope")
        exists = any(t.thread_id == a.thread for t in await store.list_threads(branch_id))
        if not exists:  # never mint a thread via a state change
            return _drop(drops, fired, a.thread, "thread does not exist")
        return [thread_state_changed(thread_id=a.thread, to_state=a.to, caused_by=cause)]
    if a.do == "create_thread":
        if not _in_scope(allowed, a.thread):
            return _drop(drops, fired, a.thread, "out of scope")
        if any(t.thread_id == a.thread for t in await store.list_threads(branch_id)):
            return []  # idempotent — already created (a legitimate no-op, not a drop)
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
        if not _in_scope(allowed, a.src):
            return _drop(drops, fired, a.src, "edge endpoint out of scope")
        if not _in_scope(allowed, a.dst):
            return _drop(drops, fired, a.dst, "edge endpoint out of scope")
        ctor = edge_added if a.do == "add_edge" else edge_removed
        return [ctor(src=a.src, rel_type=a.rel, dst=a.dst, caused_by=cause)]
    if a.do == "record_rumor":
        subjects = [s for s in a.subjects if _in_scope(allowed, s)]
        if a.subjects and not subjects:  # a rumor whose every subject is out of scope is dropped
            return _drop(drops, fired, ",".join(a.subjects), "all subjects out of scope")
        filtered = [s for s in a.subjects if not _in_scope(allowed, s)]
        if filtered:  # a PARTIAL filter: the rumor still commits, but record the dropped subjects
            drops.append(
                DroppedAction(
                    rule_id=fired.rule_id,
                    do=a.do,
                    ref=",".join(filtered),
                    reason="subject out of scope (partial)",
                )
            )
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
            return _drop(drops, fired, a.claim, "claim does not exist")
        capped = a.witnesses[:_MAX_WITNESSES]
        witnesses = [w for w in capped if _in_scope(allowed, w)]
        if not witnesses:
            return _drop(drops, fired, ",".join(a.witnesses), "no in-scope witnesses")
        filtered_w = [w for w in capped if not _in_scope(allowed, w)]
        if filtered_w:  # a PARTIAL filter: belief still spreads to the in-scope witnesses
            drops.append(
                DroppedAction(
                    rule_id=fired.rule_id,
                    do=a.do,
                    ref=",".join(filtered_w),
                    reason="witness out of scope (partial)",
                )
            )
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
) -> GauntletResult:
    """Turn fired ACTION proposals into a bounded, safe event set + the dropped-action audit (B11,
    D-40): a refused action contributes no event but a `DroppedAction` record (why it dropped).
    Deterministic: preserves the interpreter's total order; ids key on trigger_commit."""
    result = GauntletResult()
    pending: dict[tuple[str, str], int] = {}  # in-pass counter accumulation (read-your-writes)
    world_day = await store.current_world_time(branch_id)
    if len(fired) > _MAX_ACTIONS:  # the DoS cap truncates the tail — record it, don't drop silently
        result.drops.append(
            DroppedAction(
                rule_id="*",
                do="*",
                ref="",
                reason=f"{len(fired) - _MAX_ACTIONS} action(s) over the {_MAX_ACTIONS}/pass cap",
            )
        )
    for f in fired[:_MAX_ACTIONS]:
        allowed = await _scope_refs(store, branch_id, f.scope)
        result.events.extend(
            await _translate(
                store, branch_id, f, allowed, trigger_commit, pending, world_day, result.drops
            )
        )
    return result
