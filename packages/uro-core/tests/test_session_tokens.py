"""B10 (docs/18, D-39): the durable, OFF-BRANCH pieces of the session lifecycle — `pc_seats` (the
arbiter ring order recovered from the PCBound/PCReleased log, so it is reconnect/restart-stable
WITHOUT event-sourcing any turn state — D-31 kept) and the `session_tokens` registry (hashed,
revocable, fork-immune). Deterministic — no LLM. DB-backed (the `store` fixture).
"""

import hashlib

from uro_core.adapters.postgres.projector import _SNAPSHOT_TABLES
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, pc_released
from uro_core.domain.ids import new_id
from uro_core.rulesets.base import CharSpec
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic

RS = UroBasic()


def _sheet() -> dict:
    return RS.new_character(CharSpec(data={"abilities": {"STR": 12}}), Rng(0))


async def _campaign(store: PostgresEventStore, participant: str = "p1"):
    world = await store.create_world(f"b10-{new_id()}")
    campaign = await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id=participant,
        new_pc_name="Alice",
        new_pc_id="a:alice",
        pc_sheet=_sheet(),
        ruleset_id="uro-basic",
    )
    return world, campaign


async def _seat(store: PostgresEventStore, cid: str, participant: str, actor_id: str) -> None:
    await store.bind_pc(
        cid,
        participant,
        new_pc_name=participant,
        new_pc_id=actor_id,
        pc_sheet=_sheet(),
        ruleset_id="uro-basic",
    )


# --- pc_seats: the ring order, from the durable bind log ---


async def test_pc_seats_returns_participants_in_bind_order(store: PostgresEventStore) -> None:
    _, campaign = await _campaign(store, "p1")
    await _seat(store, campaign.campaign_id, "p2", "a:bob")
    await _seat(store, campaign.campaign_id, "p3", "a:cara")
    assert await store.pc_seats(campaign.campaign_id) == ["p1", "p2", "p3"]


async def test_pc_seats_uses_bind_order_not_actor_id_order_for_adopted_pcs(
    store: PostgresEventStore,
) -> None:
    # The reason pc_seats walks the LOG and not campaign_pcs (ORDER BY actor_id): an ADOPTED actor's
    # id predates its binding. Here p3 adopts an actor whose id sorts FIRST — actor_id order would
    # give [p3, p1, p2], but the true bind order is [p1, p2, p3].
    world, campaign = await _campaign(store, "p1")
    await store.append_beat(
        world.main_branch_id, [actor_created(actor_id="a:0000early", name="Early")]
    )
    await _seat(store, campaign.campaign_id, "p2", "a:zzz")
    await store.bind_pc(
        campaign.campaign_id,
        "p3",
        adopt_actor_id="a:0000early",
        pc_sheet=_sheet(),
        ruleset_id="uro-basic",
    )
    assert await store.pc_seats(campaign.campaign_id) == ["p1", "p2", "p3"]


async def test_pc_seats_drops_released_and_rebind_returns_to_original_seat(
    store: PostgresEventStore,
) -> None:
    world, campaign = await _campaign(store, "p1")
    await _seat(store, campaign.campaign_id, "p2", "a:bob")
    await _seat(store, campaign.campaign_id, "p3", "a:cara")
    # release p2 → dropped from the seats
    await store.append_beat(
        world.main_branch_id,
        [pc_released(actor_id="a:bob", participant_id="p2", campaign_id=campaign.campaign_id)],
    )
    assert await store.pc_seats(campaign.campaign_id) == ["p1", "p3"]
    # p2 re-binds (adopts their old actor) → returns to their ORIGINAL seat (index 1), not the tail
    await store.bind_pc(
        campaign.campaign_id,
        "p2",
        adopt_actor_id="a:bob",
        pc_sheet=_sheet(),
        ruleset_id="uro-basic",
    )
    assert await store.pc_seats(campaign.campaign_id) == ["p1", "p2", "p3"]


async def test_pc_seats_is_empty_for_an_unknown_campaign(store: PostgresEventStore) -> None:
    assert await store.pc_seats("c:nope") == []


# --- session_tokens: durable, hashed, revocable, off the branch axis ---


def test_session_tokens_is_not_a_fork_copied_projection() -> None:
    # Structural guard (the fork argument, D-39): a fork copies only _SNAPSHOT_TABLES rows, so the
    # token registry being ABSENT from it is fork-immunity by construction (like participant_notes).
    assert "session_tokens" not in _SNAPSHOT_TABLES


async def test_mint_resolve_revoke_at_the_store(store: PostgresEventStore) -> None:
    _, campaign = await _campaign(store)
    cid = campaign.campaign_id
    h = hashlib.sha256(b"secret-abc").hexdigest()
    await store.mint_token(h, "p1", cid)
    assert (h, "p1", cid) in await store.list_session_tokens()  # hash, participant, campaign scope
    assert await store.revoke_token(h) is True  # a live token → revoked
    assert await store.revoke_token(h) is False  # already revoked → a no-op, reported as such
    assert (h, "p1", cid) not in await store.list_session_tokens()  # revoked → not hydrated


async def test_mint_is_idempotent_and_re_mint_un_revokes(store: PostgresEventStore) -> None:
    _, campaign = await _campaign(store)
    cid = campaign.campaign_id
    h = hashlib.sha256(b"tok").hexdigest()
    await store.mint_token(h, "p1", cid)
    await store.revoke_token(h)
    await store.mint_token(h, "p1", cid)  # re-mint the same hash → live again
    assert (h, "p1", cid) in await store.list_session_tokens()


async def test_session_tokens_survive_a_fork_untouched(store: PostgresEventStore) -> None:
    world, campaign = await _campaign(store)
    cid = campaign.campaign_id
    h = hashlib.sha256(b"forky").hexdigest()
    await store.mint_token(h, "p1", cid)
    async with store.pool.acquire() as conn:
        head = await conn.fetchval(
            "SELECT head_commit FROM branches WHERE branch_id = $1", world.main_branch_id
        )
    fork = await store.fork_branch(world.world_id, head, "what-if")
    # the token is off the branch axis: a fork neither copies nor resets it
    assert (h, "p1", cid) in await store.list_session_tokens()
    assert fork.branch_id != world.main_branch_id
