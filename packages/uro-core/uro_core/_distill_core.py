"""Outcome distillation core (D-25/D-32/D-41) — the parameterized engine behind Chronicler mode.

INTERNAL (underscore module): the outcome-bundle models + the single `_distill` body, parameterized
by a `protect` predicate that decides which actors are shielded from a bundle's canon-minting. Two
callers inject it:

- `uro_core.chronicler.distill_outcome*` → `protect=_is_protected` — the UNTRUSTED path (an external
  game reporting over the network). The full D-32 ceiling holds: a protected actor can't be killed,
  looted, or conscripted as a witness by a bundle.
- `uro_core.authored.distill_authored_outcome` → `protect=_never_protected` — the TRUSTED,
  in-process
  path (a Posture-A embedder that already holds root via `store.append_beat`). It reuses the
  distillation services (witness-rumor cascade, casualty→death, loot) with NO ceiling.

**Trust is which MODULE a caller imports, never a flag on the wire (D-41, the outcome twin of D-37's
`plan=`).** `uro_server` is import-linter-forbidden from importing this module / `uro_core.authored`
directly, so the wire layer cannot even NAME the lethal path — the untrusted endpoint only ever
reaches `chronicler` (ceiling on). The guarantee against the wire is structural + CI-enforced; the
residual (a deliberate in-core edit importing `_distill` with a false predicate) is not
language-preventable and is backstopped by a behavioral server test + review (same posture as D-37).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
_SUPPORTED_BUNDLE_VERSIONS = frozenset({1})  # schema-v1 pin (D-41); a v2 bundle is rejected LOUDLY


class Feat(BaseModel):
    model_config = ConfigDict(extra="forbid")  # D-41: an unknown field (e.g. a forged trust)→400
    actor: str  # the actor who performed the deed
    description: str  # "a lone wizard split the warband's champion in two"


class LootTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    from_ref: str = ""
    to_ref: str = ""


class OutcomeBundle(BaseModel):
    """Outcome bundle v1 (docs/05, D-25) — what a game reports after resolving an encounter in its
    own domain. List sizes are capped (D-32 anti-abuse) and `extra='forbid'` + a version pin (D-41)
    make a forged `{trust:...}` field or an unknown v a loud 400, not a silently-dropped extra."""

    model_config = ConfigDict(extra="forbid")
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

    @field_validator("v")
    @classmethod
    def _pin_version(cls, v: int) -> int:
        if v not in _SUPPORTED_BUNDLE_VERSIONS:
            raise ValueError(
                f"OutcomeBundle v={v} unsupported; this engine supports "
                f"{sorted(_SUPPORTED_BUNDLE_VERSIONS)}"
            )
        return v


# A protection predicate: (store, branch_id, ref) → is this actor shielded from bundle canon?
Protect = Callable[[ProjectionQueries, str, str], Awaitable[bool]]


async def _is_protected(store: ProjectionQueries, branch_id: str, ref: str) -> bool:
    """A PC or a T2+ named actor is protected canon — an UNTRUSTED bundle may not kill/loot/
    first-hand-witness it (D-32). The predicate the network path injects."""
    if await store.is_pc(branch_id, ref):
        return True
    actor = await store.get_actor(branch_id, ref)
    return actor is not None and actor.tier >= 2


async def _never_protected(store: ProjectionQueries, branch_id: str, ref: str) -> bool:
    """No actor is shielded — the TRUSTED, in-process path (D-41). A Posture-A embedder holds root
    via `append_beat` anyway, so `authored.py` reuses distillation with the ceiling off."""
    return False


class ReceiptEntry(BaseModel):
    """One line of an ingestion receipt (docs/18 B6): what distillation did with a bundle ref, so a
    consumer learns whether its report was applied, downgraded to a rumor, or dropped — and why
    (Ironwake rows 1-2, Seventh G-22). Purely informational; it commits nothing."""

    kind: str  # feat | witness | casualty | loot
    ref: str  # the actor / item ref the entry is about
    disposition: str  # applied | downgraded | dropped
    reason: str = ""


@dataclass
class DistillResult:
    """The events to commit + the per-ref ingestion receipt (docs/18 B6)."""

    events: list[DomainEvent] = field(default_factory=list)
    receipt: list[ReceiptEntry] = field(default_factory=list)


async def _distill(
    store: ProjectionQueries, branch_id: str, bundle: OutcomeBundle, *, protect: Protect
) -> DistillResult:
    """Distill an outcome bundle into committable events (emitter E) + the witness rumor cascade +
    a per-ref receipt. `protect` decides which actors are shielded from canon: `_is_protected` (the
    untrusted D-32 ceiling) or `_never_protected` (a trusted embedder). Every protection site —
    casualty-death, witness-conscript, loot from_ref, loot to_ref — routes through it, so the two
    tiers differ ONLY in that predicate. Out-of-scope / nonexistent effects are dropped regardless
    of tier (scope + existence are not trust-relaxable — a bundle still only touches its declared
    cast). Deterministic ids (encounter_id, index) → idempotent replay."""
    cause = external_cause(bundle.encounter_id)
    participants = set(bundle.participants)
    events: list[DomainEvent] = []
    receipt: list[ReceiptEntry] = []

    def note(kind: str, ref: str, disposition: str, reason: str = "") -> None:
        receipt.append(ReceiptEntry(kind=kind, ref=ref, disposition=disposition, reason=reason))

    async def resolve(ref: str) -> str | None:
        """Entity-resolve a bundle ref to a KNOWN actor id (never mint one — a bundle cannot create
        actors), or None. Pass the RAW name to find_actor_by_name (it canonicalizes internally):
        passing a pre-canonicalized value would defeat its exact-name tiebreak and could attribute a
        feat to a different duplicate than the extractor resolves to (P8-P1)."""
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
            note("feat", feat.actor, "dropped", "actor unknown or not a declared participant")
            continue  # a feat is about a DECLARED combatant; skip out-of-cast / unknown attribution
        note("feat", actor, "applied", "recorded as truth=unknown testimony")
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
        # Witnesses must be real, in the declared cast, alive (not a casualty), and (untrusted) not
        # protected — an external game can't conscript a PC / named canon figure as its eyewitness.
        witnesses: list[str] = []
        for w in bundle.witnesses:
            if w not in participants:
                note("witness", w, "dropped", "not a declared participant")
            elif w in bundle.casualties:
                note("witness", w, "dropped", "reported as a casualty")
            elif await store.get_actor(branch_id, w) is None:
                note("witness", w, "dropped", "unknown actor")
            elif await protect(store, branch_id, w):
                note("witness", w, "dropped", "protected (PC or T2+) — can't be conscripted")
            else:
                witnesses.append(w)
                note("witness", w, "applied", "carries the rumor")
        events.extend(
            await propagate_belief(store, branch_id, claim_id=claim_id, witnesses=witnesses)
        )

    # --- casualties: an in-cast, unprotected combatant dies; an out-of-cast one DROPS (scope, like
    # feats/loot — D-41, Ironwake row-7); an in-cast PROTECTED one downgrades to testimony ---
    for casualty in bundle.casualties:
        victim = await store.get_actor(branch_id, casualty)
        if victim is None or victim.status == "dead":
            note("casualty", casualty, "dropped", "unknown actor or already dead")
            continue
        if casualty not in participants:
            note("casualty", casualty, "dropped", "not a declared participant")
            continue  # out-of-cast: dropped, not rumored (was a public rumor leak on the wire)
        if not await protect(store, branch_id, casualty):
            note("casualty", casualty, "applied", "committed as a death")
            events.append(
                actor_died(actor_id=casualty, cause="fell in the battle", caused_by=cause)
            )
        else:
            note(
                "casualty",
                casualty,
                "downgraded",
                "protected canon — a rumored fall, not a committed death (untrusted path)",
            )
            # protected canon on the UNTRUSTED path — the world hears a rumor of the fall, it does
            # not become a committed death (a protected death needs Uro's own mechanics, or the
            # trusted authored path). On the trusted path `protect` is false, so this is dead.
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

    # --- loot: only a real item, actually held by an in-cast, unprotected loser, moves to an
    # in-cast, unprotected recipient (D-41: both ends route through `protect`) ---
    for loot in bundle.loot:
        item = await store.get_item(branch_id, loot.item_id)
        if item is None or item.get("owner_ref") != loot.from_ref:
            note("loot", loot.item_id, "dropped", "nonexistent item or from_ref is not the owner")
            continue  # nonexistent item, or from_ref is not the current owner (forged transfer)
        if loot.from_ref not in participants or loot.to_ref not in participants:
            note("loot", loot.item_id, "dropped", "from_ref or to_ref not a declared participant")
            continue  # both sides must be declared combatants (scope)
        if loot.to_ref in bundle.casualties:
            note("loot", loot.item_id, "dropped", "recipient reported as fallen")
            continue  # a recipient reported as fallen can't carry loot off the field
        if await protect(store, branch_id, loot.from_ref):
            note("loot", loot.item_id, "dropped", "owner is protected (PC or T2+)")
            continue  # a PC's / named actor's gear is not looted out-of-band by an external game
        if await protect(store, branch_id, loot.to_ref):
            note("loot", loot.item_id, "dropped", "recipient is protected (PC or T2+)")
            continue  # nor can a bundle mint canon making a PC/named actor the RECIPIENT (D-41)
        note("loot", loot.item_id, "applied", f"transferred {loot.from_ref} → {loot.to_ref}")
        events.append(
            item_transferred(
                item_id=loot.item_id,
                from_ref=loot.from_ref,
                to_ref=loot.to_ref,
                caused_by=cause,
            )
        )

    return DistillResult(events=events, receipt=receipt)
