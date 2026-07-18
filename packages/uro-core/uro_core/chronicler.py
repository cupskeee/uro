"""Chronicler mode (docs/05, D-25, D-32): distill an EXTERNAL game's outcome into world memory.

In Chronicler mode Uro does NOT run the fight — an external game does ("can I hit it, for how
much?") and reports an `OutcomeBundle`. Uro answers "who knows what, and how does it change the
story?": TRUST-SCOPED rule-based distillation turns the bundle into committed events (emitter E,
external trust tier), then belief-propagates each notable feat to the surviving witnesses (docs/02)
— so feats become witness beliefs become rumors. No LLM.

This module is the UNTRUSTED path — an external game reporting over the network. It is a thin
wrapper over `_distill_core._distill` with `protect=_is_protected`, so the FULL D-32 ceiling holds:

- **Protection ceiling.** A PROTECTED actor — a PC (`is_pc`) or a T2+ named canon figure — can't be
  killed, looted, or seeded with a first-hand belief by a bundle. A protected IN-CAST casualty
  DOWNGRADES to `truth=unknown` testimony ("X is said to have fallen"); an OUT-OF-CAST casualty is
  DROPPED (scope, like feats/loot — D-41). Only an unprotected (T0/T1) declared combatant commits an
  `ActorDied`. The E-tier analogue of the gauntlet's tier ceiling (docs/13).
- **Participant scope.** Casualties, loot refs, feat.actor, and witnesses must be in the bundle's
  declared `participants` — a bundle can only touch actors in the fight it declared.
- **Existence + ownership.** A casualty must exist and not already be dead; a loot transfer requires
  the item to exist AND `from_ref` to be its CURRENT owner; feat.actor is entity-resolved.
- **A feat is TESTIMONY, not canon** — `truth="unknown"`, `origin="external"`. A bundle can never
  assert protected (`truth=true`) canon. `OutcomeBundle` is `extra=forbid` + version-pinned (D-41),
  so a forged `{trust:...}` field or an unknown `v` is a loud 400, not a silent drop.
- **Anti-abuse.** Bundle list sizes are capped; claim ids are DETERMINISTIC in (encounter_id, index)
  so a replayed bundle upserts the same rows (idempotent).

TRUSTED counterpart (D-41): a Posture-A embedder that holds root via `store.append_beat` reuses the
SAME distillation with NO ceiling via `uro_core.authored.distill_authored_outcome` — a protected
death becomes real canon (unblocking authored succession/assassination rules). Trust is which MODULE
you import, not a wire flag: `uro_server` is import-linter-forbidden from importing `authored` /
`_distill_core`, so the network endpoint only ever reaches THIS ceiling-on path (D-41, the outcome
twin of D-37's `plan=`).

Deferred (OQ-12, "the full contract waits for a real external game"): a persisted parked-encounter
registry (Uro pre-declaring the authorized roster/nonce, making participant-scope non-self-attested,
so an UNTRUSTED network game could kill a named boss) + fine endpoint→campaign authority beyond the
outcome endpoint. The protection ceiling already contains the DAMAGE without them.
"""

from __future__ import annotations

from uro_core._distill_core import (
    DistillResult,
    Feat,
    LootTransfer,
    OutcomeBundle,
    ReceiptEntry,
    _distill,
    _is_protected,
)
from uro_core.domain.events import DomainEvent
from uro_core.ports.projections import ProjectionQueries

# Re-export the public outcome-ingestion surface (models live in the internal core so the trusted
# and untrusted callers share one definition without a circular import). Importers unchanged.
__all__ = [
    "DistillResult",
    "Feat",
    "LootTransfer",
    "OutcomeBundle",
    "ReceiptEntry",
    "distill_outcome",
    "distill_outcome_with_receipt",
]


async def distill_outcome(
    store: ProjectionQueries, branch_id: str, bundle: OutcomeBundle
) -> list[DomainEvent]:
    """Distill an outcome bundle into committable events (emitter E) — the ergonomic wrapper that
    returns just the events (unchanged contract). Use `distill_outcome_with_receipt` for the per-ref
    disposition receipt (docs/18 B6). UNTRUSTED: the full D-32 ceiling applies."""
    return (await distill_outcome_with_receipt(store, branch_id, bundle)).events


async def distill_outcome_with_receipt(
    store: ProjectionQueries, branch_id: str, bundle: OutcomeBundle
) -> DistillResult:
    """Distill an outcome bundle into committable events + the witness rumor cascade, TRUST-SCOPED
    (D-32 — the UNTRUSTED path: `protect=_is_protected`), AND a per-ref receipt (docs/18 B6). The
    caller commits `.events`; `.receipt` says what was applied/downgraded/dropped per ref
    and why. Protected or out-of-scope effects are dropped or downgraded to testimony, never canon.

    NOTE: this takes NO trust parameter — trust is a separate module (`authored.py`), not an
    argument, so the wire layer that calls this has no trust-shaped knob to forward (D-41)."""
    return await _distill(store, branch_id, bundle, protect=_is_protected)
