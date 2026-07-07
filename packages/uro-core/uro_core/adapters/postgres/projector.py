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


async def _place_created(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO proj_places (branch_id, place_id, name, kind, status, description) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (branch_id, place_id) DO UPDATE SET "
        "name = EXCLUDED.name, kind = EXCLUDED.kind, status = EXCLUDED.status, "
        "description = EXCLUDED.description",
        branch_id,
        p["place_id"],
        p["name"],
        p.get("kind", "site"),
        p.get("status", "active"),
        p.get("description", ""),
    )


# Columns a PlaceStateChanged `changes{}` may touch — a whitelist, so an errant key
# can never widen the write surface beyond the projection's own columns.
_PLACE_MUTABLE = ("name", "kind", "status", "description")


async def _place_state_changed(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    changes = {k: v for k, v in p.get("changes", {}).items() if k in _PLACE_MUTABLE}
    for column, value in changes.items():
        await conn.execute(
            f"UPDATE proj_places SET {column} = $3 WHERE branch_id = $1 AND place_id = $2",
            branch_id,
            p["place_id"],
            value,
        )


async def _terrain_changed(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "UPDATE proj_places SET description = $3 WHERE branch_id = $1 AND place_id = $2",
        branch_id,
        p["place_id"],
        p["description"],
    )


async def _place_destroyed(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "UPDATE proj_places SET status = 'destroyed' WHERE branch_id = $1 AND place_id = $2",
        branch_id,
        p["place_id"],
    )


async def _pc_bound(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO proj_pcs (branch_id, campaign_id, actor_id, participant_id, active) "
        "VALUES ($1, $2, $3, $4, true) "
        "ON CONFLICT (branch_id, campaign_id, actor_id) DO UPDATE SET "
        "participant_id = EXCLUDED.participant_id, active = true",
        branch_id,
        p["campaign_id"],
        p["actor_id"],
        p["participant_id"],
    )


async def _pc_released(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    # Retire to NPC: the binding row stays as history, `active` flips false.
    await conn.execute(
        "UPDATE proj_pcs SET active = false "
        "WHERE branch_id = $1 AND campaign_id = $2 AND actor_id = $3",
        branch_id,
        p["campaign_id"],
        p["actor_id"],
    )


_HANDLERS = {
    "ActorCreated": _actor_created,
    "ActorPromoted": _actor_promoted,
    "ClaimRecorded": _claim_recorded,
    "ClaimTruthChanged": _claim_truth_changed,
    "BeliefChanged": _belief_changed,
    "PlaceCreated": _place_created,
    "PlaceStateChanged": _place_state_changed,
    "TerrainChanged": _terrain_changed,
    "PlaceDestroyed": _place_destroyed,
    "PCBound": _pc_bound,
    "PCReleased": _pc_released,
}


async def apply_raw(
    conn: asyncpg.Connection, branch_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    """Apply one event's payload to the projections. The single entry point used by
    both the commit path (append_beat) and replay/materialization — so a forked branch
    is projected by exactly the same code that projected the original."""
    handler = _HANDLERS.get(event_type)
    if handler is not None:
        await handler(conn, branch_id, payload)


async def apply_event(conn: asyncpg.Connection, branch_id: str, event: DomainEvent) -> None:
    await apply_raw(conn, branch_id, event.event_type, event.payload)


# --- Snapshots: serialize / restore the full projection state of a branch (docs/03, 07) ---
#
# The projector stays the SOLE writer of the proj_* tables: a snapshot is projector
# output captured verbatim, and restoring it is replay's equal (same rows the handlers
# above would have produced). Keeping both here preserves that invariant end-to-end.

_SNAPSHOT_TABLES: dict[str, tuple[str, ...]] = {
    "actors": ("actor_id", "name", "tier", "role", "aliases"),
    "claims": ("claim_id", "statement", "subject_refs", "truth", "origin"),
    "beliefs": ("actor_id", "claim_id", "confidence", "learned_from"),
    "places": ("place_id", "name", "kind", "status", "description"),
    "pcs": ("campaign_id", "actor_id", "participant_id", "active"),
}


async def snapshot_state(conn: asyncpg.Connection, branch_id: str) -> dict[str, Any]:
    """Serialize a branch's current projection rows to a plain, deterministically-ordered
    dict (stable across runs, so its state_hash is reproducible)."""
    state: dict[str, Any] = {"v": 1}
    for section, columns in _SNAPSHOT_TABLES.items():
        rows = await conn.fetch(
            f"SELECT {', '.join(columns)} FROM proj_{section} "
            f"WHERE branch_id = $1 ORDER BY {columns[0]}, {columns[1]}",
            branch_id,
        )
        state[section] = [dict(r) for r in rows]
    return state


async def restore_snapshot(conn: asyncpg.Connection, branch_id: str, state: dict[str, Any]) -> None:
    """Write a serialized snapshot into a (fresh) branch's projection rows — the O(1)-ish
    seed that replay then advances forward. Tolerant of older snapshot versions that
    predate a section (rebuildable cache; a missing key just means no rows)."""
    for section, columns in _SNAPSHOT_TABLES.items():
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 2))
        insert = (
            f"INSERT INTO proj_{section} (branch_id, {', '.join(columns)}) VALUES ({placeholders})"
        )
        for row in state.get(section, []):
            await conn.execute(insert, branch_id, *(row[c] for c in columns))
