"""Event → projection application (docs/03, 07).

The projector is the ONLY writer of the proj_* tables. It runs inside the same
transaction as the commit that produced the events (store.append_beat), so a
projection failure rolls the whole beat back — projections can never drift ahead
of, or behind, the log. Unknown event types (WorldGenesis, BeatResolved, …) are
no-ops: they carry no projectable epistemic state.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from uro_core.domain.events import DomainEvent


async def _actor_created(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO proj_actors (branch_id, actor_id, name, tier, role, aliases) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (branch_id, actor_id) DO UPDATE SET "
        "name = EXCLUDED.name, tier = EXCLUDED.tier, role = EXCLUDED.role, "
        "aliases = EXCLUDED.aliases",
        branch_id,
        p["actor_id"],
        p["name"],
        p["tier"],
        p.get("role", ""),
        p.get("aliases", []),
    )


async def _actor_promoted(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "UPDATE proj_actors SET tier = $3 WHERE branch_id = $1 AND actor_id = $2",
        branch_id,
        p["actor_id"],
        p["to_tier"],
    )


async def _claim_recorded(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO proj_claims (branch_id, claim_id, statement, subject_refs, truth, origin) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (branch_id, claim_id) DO UPDATE SET "
        "statement = EXCLUDED.statement, subject_refs = EXCLUDED.subject_refs, "
        "truth = EXCLUDED.truth, origin = EXCLUDED.origin",
        branch_id,
        p["claim_id"],
        p["statement"],
        p.get("subject_refs", []),
        p["truth"],
        p.get("origin", ""),
    )


async def _claim_truth_changed(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "UPDATE proj_claims SET truth = $3 WHERE branch_id = $1 AND claim_id = $2",
        branch_id,
        p["claim_id"],
        p["truth"],
    )


async def _belief_changed(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO proj_beliefs (branch_id, actor_id, claim_id, confidence, learned_from) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (branch_id, actor_id, claim_id) DO UPDATE SET "
        "confidence = EXCLUDED.confidence, learned_from = EXCLUDED.learned_from",
        branch_id,
        p["actor_id"],
        p["claim_id"],
        p["confidence"],
        p.get("learned_from"),
    )


_HANDLERS = {
    "ActorCreated": _actor_created,
    "ActorPromoted": _actor_promoted,
    "ClaimRecorded": _claim_recorded,
    "ClaimTruthChanged": _claim_truth_changed,
    "BeliefChanged": _belief_changed,
}


async def apply_event(conn: asyncpg.Connection, branch_id: str, event: DomainEvent) -> None:
    handler = _HANDLERS.get(event.event_type)
    if handler is not None:
        await handler(conn, branch_id, event.payload)
