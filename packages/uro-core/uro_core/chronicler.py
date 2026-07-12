"""Chronicler mode (docs/05, D-25, D-32): distill an EXTERNAL game's outcome into world memory.

In Chronicler mode Uro does NOT run the fight — an external game does ("can I hit it, for how
much?") and reports an `OutcomeBundle`. Uro answers "who knows what, and how does it change the
story?": TRUST-SCOPED rule-based distillation turns the bundle into committed events (emitter E,
external trust tier), then belief-propagates each notable feat to the surviving witnesses (docs/02)
— so feats become witness beliefs become rumors. No LLM.

TRUST MODEL — the scope is ENFORCED, not just claimed (D-32, OQ-12 hardening). An external game is
UNTRUSTED beyond its own encounter, so `distill_outcome` fences every effect against the existing
projection ports before minting anything:

- **Protection ceiling.** A PROTECTED actor — a PC (`is_pc`) or a T2+ named canon figure — can't be
  killed, looted, or seeded with a first-hand belief by a bundle. A protected (or out-of-cast)
  casualty DOWNGRADES to `truth=unknown` testimony ("X is said to have fallen") — the world hears
  the death as a rumor, it does not become canon. Only an unprotected (T0/T1) declared combatant
  commits an `ActorDied`. This is the E-tier analogue of the gauntlet's tier ceiling (docs/13).
- **Participant scope.** Casualties, loot refs, feat.actor, and witnesses must be in the bundle's
  declared `participants` — a bundle can only touch actors in the fight it declared, never a
  bystander it merely names in a casualty/loot list.
- **Existence + ownership.** A casualty must exist and not already be dead; a loot transfer requires
  the item to exist AND `from_ref` to be its CURRENT owner (no looting an item the loser never
  held); feat.actor is entity-resolved (`canonical_name`/`find_actor_by_name`), never a raw string.
- **A feat is TESTIMONY, not canon** — `truth="unknown"`, `origin="external"`, believed by its
  witnesses. An external bundle can never assert protected (`truth=true`) canon.
- **Anti-abuse.** Bundle list sizes are capped at the schema edge; claim ids are DETERMINISTIC in
  (encounter_id, index) so a replayed bundle upserts the same rows (idempotent — no double kills,
  duplicate rumors, or re-looting).

Deferred (OQ-12, "the full contract waits for a real external game"): a persisted parked-encounter
registry (Uro pre-declaring the authorized roster/nonce, making participant-scope non-self-attested)
+ fine endpoint→campaign authority. The protection ceiling already contains the DAMAGE without them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from uro_core.domain.events import (
    DomainEvent,
    actor_died,
    claim_recorded,
    external_cause,
    item_transferred,
)
from uro_core.engines.actor import propagate_belief
from uro_core.ports.projections import ProjectionQueries

_MAX_LIST = 64  # cap on feats / casualties / loot per bundle (anti-abuse; docs/13, OQ-12)
_MAX_WITNESSES = 256


class Feat(BaseModel):
    actor: str  # the actor who performed the deed
    description: str  # "a lone wizard split the warband's champion in two"


class LootTransfer(BaseModel):
    item_id: str
    from_ref: str = ""
    to_ref: str = ""


class OutcomeBundle(BaseModel):
    """Outcome bundle v0 (docs/05, D-25) — what an external game reports after resolving an
    encounter in its own domain. List sizes are capped (D-32 anti-abuse): a buggy/malicious game
    can't submit an unbounded bundle that would balloon one commit + the belief cascade."""

    v: int = 1
    encounter_id: str
    participants: list[str] = Field(default_factory=list, max_length=_MAX_WITNESSES)
    witnesses: list[str] = Field(
        default_factory=list, max_length=_MAX_WITNESSES
    )  # SURVIVORS who saw it
    casualties: list[str] = Field(default_factory=list, max_length=_MAX_LIST)
    feats: list[Feat] = Field(default_factory=list, max_length=_MAX_LIST)
    loot: list[LootTransfer] = Field(default_factory=list, max_length=_MAX_LIST)
    duration_rounds: int = 0


async def _is_protected(store: ProjectionQueries, branch_id: str, ref: str) -> bool:
    """A PC or a T2+ named actor is protected canon — an external bundle may not kill/loot/
    first-hand-witness it (D-32)."""
    if await store.is_pc(branch_id, ref):
        return True
    actor = await store.get_actor(branch_id, ref)
    return actor is not None and actor.tier >= 2


async def distill_outcome(
    store: ProjectionQueries, branch_id: str, bundle: OutcomeBundle
) -> list[DomainEvent]:
    """Distill an outcome bundle into committable events (emitter E) + the witness rumor cascade,
    TRUST-SCOPED (D-32 — see the module docstring). The caller commits them. Out-of-scope or
    protected effects are dropped or downgraded to testimony, never committed as canon."""
    cause = external_cause(bundle.encounter_id)
    participants = set(bundle.participants)
    events: list[DomainEvent] = []

    async def resolve(ref: str) -> str | None:
        """Entity-resolve a bundle ref to a KNOWN actor id (never mint one — an external game
        cannot create actors), or None. Pass the RAW name to find_actor_by_name (it canonicalizes
        internally): passing a pre-canonicalized value would defeat its exact-name tiebreak and
        could attribute a feat to a different duplicate than the extractor resolves to (P8-P1)."""
        if not ref:
            return None
        if await store.get_actor(branch_id, ref) is not None:
            return ref
        match = await store.find_actor_by_name(branch_id, ref)
        return match.actor_id if match is not None else None

    # --- feats → testimony + a witness rumor cascade (deterministic ids → idempotent replay) ---
    for i, feat in enumerate(bundle.feats):
        actor = await resolve(feat.actor)
        if actor is None or actor not in participants:
            continue  # a feat is about a DECLARED combatant; skip out-of-cast / unknown attribution
        claim_id = f"c:{bundle.encounter_id}:feat:{i}"
        events.append(
            claim_recorded(
                claim_id=claim_id,
                statement=feat.description,
                subject_refs=[actor],
                truth="unknown",  # testimony, not canon — witnesses believe it, the world doesn't
                origin="external",
                caused_by=cause,
            )
        )
        # Witnesses must be real, in the declared cast, alive (not a casualty), and UNPROTECTED —
        # an external game can't conscript a PC or a named canon figure as its eyewitness (D-32).
        witnesses: list[str] = []
        for w in bundle.witnesses:
            if (
                w in participants
                and w not in bundle.casualties
                and await store.get_actor(branch_id, w) is not None
                and not await _is_protected(store, branch_id, w)
            ):
                witnesses.append(w)
        events.extend(
            await propagate_belief(store, branch_id, claim_id=claim_id, witnesses=witnesses)
        )

    # --- casualties: unprotected declared combatants die; protected/out-of-cast → testimony ---
    for casualty in bundle.casualties:
        victim = await store.get_actor(branch_id, casualty)
        if victim is None or victim.status == "dead":
            continue  # unknown target or already dead — nothing to commit
        if casualty in participants and not await _is_protected(store, branch_id, casualty):
            events.append(
                actor_died(actor_id=casualty, cause="fell in the battle", caused_by=cause)
            )
        else:
            # protected canon / a bystander the bundle named — the world hears a rumor of the fall,
            # it does not become a committed death (a protected death needs Uro's own mechanics).
            events.append(
                claim_recorded(
                    claim_id=f"c:{bundle.encounter_id}:fell:{casualty}",
                    statement=f"{victim.name} is said to have fallen at {bundle.encounter_id}.",
                    subject_refs=[casualty],
                    truth="unknown",
                    origin="external",
                    caused_by=cause,
                )
            )

    # --- loot: only a real item, actually held by an in-cast, unprotected loser, moves ---
    for loot in bundle.loot:
        item = await store.get_item(branch_id, loot.item_id)
        if item is None or item.get("owner_ref") != loot.from_ref:
            continue  # nonexistent item, or from_ref is not the current owner (forged transfer)
        if loot.from_ref not in participants or loot.to_ref not in participants:
            continue  # both sides must be declared combatants (scope)
        if loot.to_ref in bundle.casualties:
            continue  # gap Ironwake: a recipient reported as fallen can't carry loot off the field
        if await _is_protected(store, branch_id, loot.from_ref):
            continue  # a PC's / named actor's gear is not looted out-of-band by an external game
        events.append(
            item_transferred(
                item_id=loot.item_id,
                from_ref=loot.from_ref,
                to_ref=loot.to_ref,
                caused_by=cause,
            )
        )

    return events
