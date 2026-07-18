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
        "INSERT INTO proj_claims (branch_id, claim_id, statement, subject_refs, truth, origin, "
        "created_day) VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (branch_id, claim_id) DO UPDATE SET "
        "statement = EXCLUDED.statement, subject_refs = EXCLUDED.subject_refs, "
        # created_day is NOT updated on conflict — a re-minted rumor (same deterministic id each
        # cadence, RL-8) keeps its FIRST-recorded day so its age is stable (C5).
        "truth = EXCLUDED.truth, origin = EXCLUDED.origin",
        branch_id,
        p["claim_id"],
        p["statement"],
        p.get("subject_refs", []),
        p["truth"],
        p.get("origin", ""),
        p.get("created_day", 0),
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


async def _sheet_updated(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    # Whole-sheet replace (docs/06): the ruleset owns the sheet's shape; we store it opaquely.
    await conn.execute(
        "INSERT INTO proj_sheets (branch_id, actor_id, ruleset_id, sheet) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (branch_id, actor_id) DO UPDATE SET "
        "ruleset_id = EXCLUDED.ruleset_id, sheet = EXCLUDED.sheet",
        branch_id,
        p["actor_id"],
        p.get("ruleset_id", ""),
        p.get("sheet", {}),
    )


async def _actor_damaged(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    # LEGACY replay-compat ONLY (phase-6 review). The current encounter runner emits the ruleset's
    # opaque final SheetUpdated for harm (game-agnostic, D-30) and NEVER emits ActorDamaged. This
    # handler exists solely so a PRE-Phase-6 d20 log — which recorded a fight's HP purely as
    # accumulated ActorDamaged reductions, with no closing SheetUpdated — still rebuilds
    # byte-identically by replay (the non-negotiable "projections are rebuildable read-models"
    # invariant, docs/01/07). It only ever fires on the legacy event, which only the old d20 runner
    # emitted, so it touches only hp sheets and adds no shared hp assumption for new/other rulesets.
    await conn.execute(
        "UPDATE proj_sheets SET sheet = jsonb_set(sheet, '{hp}', "
        "  to_jsonb(GREATEST(0, ((sheet->>'hp')::int) - $3))) "
        "WHERE branch_id = $1 AND actor_id = $2 AND sheet ? 'hp'",
        branch_id,
        p["actor_id"],
        int(p.get("amount", 0)),
    )


async def _actor_died(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    # A ruleset-AGNOSTIC lifecycle trace on proj_actors (D-30): a sheet-less casualty (History NPC,
    # Chronicler death) is recorded dead and drops off recall's on-stage set. proj_actors.status is
    # the AUTHORITATIVE death trace (recall reads it, not the sheet). The projector NEVER touches
    # sheet internals — the encounter runner already committed the ruleset's final opaque sheet
    # (an hp system zeroes hp; a harm-clock fills the clock; the projector can't assume which).
    # Forcing sheet.hp=0 here was a d20 leak, now removed. Consequence (phase-6 review, accepted):
    # an actor killed by a BARE ActorDied (Chronicler/History death, no accompanying SheetUpdated)
    # keeps its last sheet values; status='dead' is the death of record, the stale sheet is inert.
    await conn.execute(
        "UPDATE proj_actors SET status = 'dead' WHERE branch_id = $1 AND actor_id = $2",
        branch_id,
        p["actor_id"],
    )


async def _item_created(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO proj_items (branch_id, item_id, name, kind, owner_ref) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (branch_id, item_id) DO UPDATE SET "
        "name = EXCLUDED.name, kind = EXCLUDED.kind, owner_ref = EXCLUDED.owner_ref",
        branch_id,
        p["item_id"],
        p.get("name", ""),
        p.get("kind", ""),
        p.get("owner_ref", ""),
    )


async def _item_transferred(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    # Ownership-guarded (D-32): when `from_ref` is given, the move applies only if it is the CURRENT
    # owner — so a stale or REPLAYED transfer (e.g. a re-POSTed Chronicler bundle) no-ops instead of
    # re-assigning. An empty from_ref keeps the legacy unconditional behavior (a transfer that
    # doesn't declare a source).
    await conn.execute(
        "UPDATE proj_items SET owner_ref = $3 "
        "WHERE branch_id = $1 AND item_id = $2 AND ($4 = '' OR owner_ref = $4)",
        branch_id,
        p["item_id"],
        p.get("to_ref", ""),
        p.get("from_ref", ""),
    )


async def _faction_created(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO proj_factions (branch_id, faction_id, name, kind, description) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (branch_id, faction_id) DO UPDATE SET "
        "name = EXCLUDED.name, kind = EXCLUDED.kind, description = EXCLUDED.description",
        branch_id,
        p["faction_id"],
        p["name"],
        p.get("kind", "faction"),
        p.get("description", ""),
    )


async def _edge_added(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    # EdgeAdded and EdgeUpdated share this upsert (set weight/attrs on the (src,rel,dst) key).
    await conn.execute(
        "INSERT INTO proj_edges (branch_id, src, rel_type, dst, weight, attrs) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (branch_id, src, rel_type, dst) DO UPDATE SET "
        "weight = EXCLUDED.weight, attrs = EXCLUDED.attrs",
        branch_id,
        p["src"],
        p["rel_type"],
        p["dst"],
        float(p.get("weight", 1.0)),
        p.get("attrs", {}),
    )


async def _edge_removed(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "DELETE FROM proj_edges WHERE branch_id = $1 AND src = $2 AND rel_type = $3 AND dst = $4",
        branch_id,
        p["src"],
        p["rel_type"],
        p["dst"],
    )


async def _thread_created(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO proj_threads (branch_id, thread_id, stakes, state, provenance) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (branch_id, thread_id) DO UPDATE SET "
        "stakes = EXCLUDED.stakes, state = EXCLUDED.state, provenance = EXCLUDED.provenance",
        branch_id,
        p["thread_id"],
        p["stakes"],
        p.get("state", "dormant"),
        p.get("provenance", "author"),
    )


async def _thread_state_changed(
    conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]
) -> None:
    # A no-op if the thread was never created (never mint a thread from a state change).
    await conn.execute(
        "UPDATE proj_threads SET state = $3 WHERE branch_id = $1 AND thread_id = $2",
        branch_id,
        p["thread_id"],
        p["to_state"],
    )


async def _counter_changed(conn: asyncpg.Connection, branch_id: str, p: dict[str, Any]) -> None:
    # Computation Layer (docs/19, D-34): UPSERT the absolute value; PRESERVE created_day on conflict
    # (only updated_day moves) so counter/claim age math works. Idempotent by (branch, scope, key).
    await conn.execute(
        "INSERT INTO proj_counters (branch_id, scope_ref, key, value, created_day, updated_day) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (branch_id, scope_ref, key) DO UPDATE SET "
        "value = EXCLUDED.value, updated_day = EXCLUDED.updated_day",
        branch_id,
        p["scope_ref"],
        p["key"],
        int(p["to_value"]),
        int(p.get("created_day", 0)),
        int(p.get("updated_day", 0)),
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
    "SheetUpdated": _sheet_updated,
    "ActorDamaged": _actor_damaged,  # legacy replay-compat only; new logs use SheetUpdated
    "ActorDied": _actor_died,
    "ItemCreated": _item_created,
    "ItemTransferred": _item_transferred,
    "ThreadCreated": _thread_created,
    "ThreadStateChanged": _thread_state_changed,
    "CounterChanged": _counter_changed,
    "FactionCreated": _faction_created,
    "EdgeAdded": _edge_added,
    "EdgeUpdated": _edge_added,
    "EdgeRemoved": _edge_removed,
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
    "actors": ("actor_id", "name", "tier", "role", "aliases", "status"),
    "claims": ("claim_id", "statement", "subject_refs", "truth", "origin", "created_day"),
    "beliefs": ("actor_id", "claim_id", "confidence", "learned_from"),
    "places": ("place_id", "name", "kind", "status", "description"),
    "pcs": ("campaign_id", "actor_id", "participant_id", "active"),
    "sheets": ("actor_id", "ruleset_id", "sheet"),
    "items": ("item_id", "name", "kind", "owner_ref"),
    "factions": ("faction_id", "name", "kind", "description"),
    "edges": ("src", "rel_type", "dst", "weight", "attrs"),
    "threads": ("thread_id", "stakes", "state", "provenance"),
    "counters": ("scope_ref", "key", "value", "created_day", "updated_day"),
}


async def snapshot_state(conn: asyncpg.Connection, branch_id: str) -> dict[str, Any]:
    """Serialize a branch's current projection rows to a plain, deterministically-ordered
    dict (stable across runs, so its state_hash is reproducible)."""
    state: dict[str, Any] = {"v": 1}
    for section, columns in _SNAPSHOT_TABLES.items():
        # Order by ALL columns for a TOTAL order — ordering by only the first two leaves rows
        # that share them (e.g. edges with the same src+rel_type but different dst) in an
        # undefined order, which would make the state_hash non-reproducible.
        rows = await conn.fetch(
            f"SELECT {', '.join(columns)} FROM proj_{section} "
            f"WHERE branch_id = $1 ORDER BY {', '.join(columns)}",
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
