"""Phase 7 inc 7.1 (OQ-7): every beat runs as the ACTING participant's PC. Deterministic — no LLM.

The single-player leak the audit found: the pipeline resolved the PC via campaign_pc (the
campaign's FIRST PC) and ignored the submitting participant — so a party of N all planned/rolled
as one PC. Fixed by pc_for_participant + threading participant_id. These tests seat two
participants on their own PCs and assert each beat is planned/gated/attributed as ITS submitter's.
"""

from collections.abc import AsyncIterator

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, item_created, sheet_updated
from uro_core.domain.ids import new_id
from uro_core.errors import UnboundParticipantError
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import CharSpec
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.session import AdmitDecision, PartyArbiter, SoloArbiter

RS = UroBasic()


def _sheet(abilities: dict[str, int]) -> dict:
    return RS.new_character(CharSpec(data={"abilities": abilities}), Rng(0))


class _Recorder:
    """Records each planner prompt so a test can see WHICH PC the beat was planned as; serves a
    canned plan + fixed narration."""

    def __init__(self, plan_json: str = '{"intent_class":"examine","triggers":[],"mechanics":[]}'):
        self._plan = plan_json
        self.planner_prompts: list[str] = []

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "The scene holds."

    async def complete(self, req: CompletionRequest) -> str:
        if req.stage_tag == "planner":
            self.planner_prompts.append(" ".join(m.content for m in req.messages))
            return self._plan
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def _two_pc_campaign(store: PostgresEventStore):
    """A campaign with participant p1→a:alice (start) and p2→a:bob (bind_pc join)."""
    world = await store.create_world(f"party-{new_id()}")
    main = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        new_pc_name="Alice",
        new_pc_id="a:alice",
        pc_sheet=_sheet({"STR": 12}),
        ruleset_id="uro-basic",
    )
    bob = await store.bind_pc(
        campaign.campaign_id,
        "p2",
        new_pc_name="Bob",
        new_pc_id="a:bob",
        pc_sheet=_sheet({"STR": 12}),
        ruleset_id="uro-basic",
    )
    assert bob == "a:bob"
    return world, campaign


# --- the resolver: participant → their own PC ---


async def test_pc_for_participant_resolves_each_participants_own_pc(
    store: PostgresEventStore,
) -> None:
    _, campaign = await _two_pc_campaign(store)
    cid = campaign.campaign_id
    assert await store.pc_for_participant(cid, "p1") == "a:alice"
    assert await store.pc_for_participant(cid, "p2") == "a:bob"
    assert await store.pc_for_participant(cid, "p_ghost") is None  # unbound → None
    # bind_pc is idempotent: a re-join returns the existing PC, binds nothing new
    assert await store.bind_pc(cid, "p2", new_pc_name="Dup", new_pc_id="a:dup") == "a:bob"


# --- the beat is PLANNED as the acting participant's PC ---


async def test_beat_is_planned_as_the_submitting_participants_pc(store: PostgresEventStore) -> None:
    _, campaign = await _two_pc_campaign(store)
    rec = _Recorder()
    engine = Engine(store, ProviderRouter(bindings={}, default=rec), ruleset=RS)

    await engine.run_beat(campaign, "p1", "I look around")
    assert "a:alice" in rec.planner_prompts[-1] and "a:bob" not in rec.planner_prompts[-1]

    await engine.run_beat(campaign, "p2", "I look around")
    assert "a:bob" in rec.planner_prompts[-1] and "a:alice" not in rec.planner_prompts[-1]


async def test_unbound_participant_is_refused_in_a_party_but_falls_back_solo(
    store: PostgresEventStore,
) -> None:
    # In a PARTY, an unbound participant must NOT silently drive another player's PC — it is refused
    # (cross-phase review). In SOLO (one PC), an unbound submitter safely falls back to that one PC.
    _, party = await _two_pc_campaign(store)  # two PCs → a party
    engine = Engine(store, ProviderRouter(bindings={}, default=_Recorder()), ruleset=RS)
    with pytest.raises(UnboundParticipantError):
        await engine.run_beat(party, "p_ghost", "I look around")

    world = await store.create_world(f"solo-{new_id()}")  # one PC → solo
    solo = await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Solo",
        new_pc_id="a:solo",
        pc_sheet=_sheet({"STR": 12}),
        ruleset_id="uro-basic",
    )
    assert await store.campaign_pcs(solo.campaign_id) == ["a:solo"]  # solo → one PC
    assert len(await store.campaign_pcs(party.campaign_id)) == 2  # a party → multiple PCs
    rec = _Recorder()
    await Engine(store, ProviderRouter(bindings={}, default=rec), ruleset=RS).run_beat(
        solo, "some_other_id", "I look around"
    )
    assert "a:solo" in rec.planner_prompts[-1]  # solo fallback is safe (only one PC)


# --- the encounter is fought/attributed as the acting participant's PC ---


async def test_encounter_aggressor_is_the_acting_participants_pc(store: PostgresEventStore) -> None:
    world, campaign = await _two_pc_campaign(store)
    main = world.main_branch_id
    # a brute both PCs could attack; strong enough that whoever fights it LOSES.
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:grull", name="Grull", tier=2, role="brute"),
            sheet_updated(
                actor_id="a:grull",
                sheet=_sheet({"STR": 20, "DEX": 14, "CON": 20}),
                ruleset_id="uro-basic",
            ),
            item_created(item_id="i:club", name="club", owner_ref="a:grull"),
        ],
    )
    # p2 (Bob) attacks with NO explicit actor ref — the aggressor must default to Bob (p2's PC),
    # not Alice (the campaign's first PC). Bob loses → Bob is downed; Alice is untouched.
    plan = (
        '{"intent_class":"action","triggers":["violence"],'
        '"mechanics":[{"affordance":"attack","target":"a:grull"}]}'
    )

    async def _run(pid: str) -> None:
        rec = _Recorder(plan)
        await Engine(store, ProviderRouter(bindings={}, default=rec), ruleset=RS).run_beat(
            campaign, pid, "I swing at Grull"
        )

    await _run("p2")
    assert (await store.get_sheet(main, "a:bob"))["hp"] == 0  # Bob fought and lost
    assert (await store.get_sheet(main, "a:alice"))["hp"] > 0  # Alice never entered the fight


# --- the PartyArbiter: round-robin turn ownership (OQ-7 → D-31) ---


async def test_solo_arbiter_always_admits() -> None:
    a = SoloArbiter()
    assert await a.admit("c", "p1", "x") == AdmitDecision.ADMITTED
    await a.note_joined("c", "p1")  # no-ops, never raise
    await a.beat_committed("c", "p1", "b")
    assert await a.admit("c", "anyone", "x") == AdmitDecision.ADMITTED


async def test_party_arbiter_rotates_the_turn_on_each_committed_beat() -> None:
    a = PartyArbiter()
    for p in ("p1", "p2", "p3"):
        await a.note_joined("c", p)
    # p1 holds first (join order); the others must wait
    assert await a.admit("c", "p1", "x") == AdmitDecision.ADMITTED
    assert await a.admit("c", "p2", "x") == AdmitDecision.NOT_YOUR_TURN
    assert await a.admit("c", "p3", "x") == AdmitDecision.NOT_YOUR_TURN
    # a committed beat rotates the token p1 → p2 → p3 → p1
    order = []
    for _ in range(6):
        holder = None
        for p in ("p1", "p2", "p3"):
            if await a.admit("c", p, "x") == AdmitDecision.ADMITTED:
                holder = p
                break
        assert holder is not None
        order.append(holder)
        await a.beat_committed("c", holder, "b")
    assert order == ["p1", "p2", "p3", "p1", "p2", "p3"]  # deterministic round-robin


async def test_party_arbiter_isolates_campaigns() -> None:
    a = PartyArbiter()
    await a.note_joined("c1", "p1")
    await a.note_joined("c2", "p2")
    assert await a.admit("c1", "p1", "x") == AdmitDecision.ADMITTED
    assert await a.admit("c2", "p2", "x") == AdmitDecision.ADMITTED  # each campaign its own ring


async def test_party_arbiter_departure_passes_the_token() -> None:
    a = PartyArbiter()
    for p in ("p1", "p2", "p3"):
        await a.note_joined("c", p)
    # a NON-holder leaving does not disturb the holder
    await a.note_left("c", "p3")
    assert await a.admit("c", "p1", "x") == AdmitDecision.ADMITTED
    # the HOLDER leaving: the turn passes to a surviving member (not orphaned)
    await a.note_left("c", "p1")  # roster now [p2]; the token must land on p2
    assert await a.admit("c", "p2", "x") == AdmitDecision.ADMITTED
    assert await a.admit("c", "px", "x") == AdmitDecision.NOT_YOUR_TURN  # a non-member never holds


async def test_party_arbiter_holder_departs_at_nonzero_cursor() -> None:
    # gap-report Seventh Vault G-17: the old note_left stepped the cursor BACKWARD when the holder
    # left from a NON-ZERO position (the existing test masked it — its holder departs at cursor 0,
    # where max(0, cur-1) clamps right by coincidence). Rotate the token first so the holder is p2.
    a = PartyArbiter()
    for p in ("p1", "p2", "p3"):
        await a.note_joined("c", p)
    await a.beat_committed("c", "p1", "b1")  # token 0 -> 1, holder is now p2
    assert await a.admit("c", "p2", "x") == AdmitDecision.ADMITTED
    await a.note_left("c", "p2")  # the HOLDER (cursor 1) leaves → token must hand off to p3, not p1
    assert await a.admit("c", "p3", "x") == AdmitDecision.ADMITTED  # successor, not backward to p1
    assert await a.admit("c", "p1", "x") == AdmitDecision.NOT_YOUR_TURN


async def test_party_arbiter_member_before_holder_departs_keeps_holder() -> None:
    # a member strictly BEFORE the holder leaving must keep the SAME holder (shift the cursor down).
    a = PartyArbiter()
    for p in ("p1", "p2", "p3"):
        await a.note_joined("c", p)
    await a.beat_committed("c", "p1", "b1")
    await a.beat_committed("c", "p2", "b2")  # token now 2, holder is p3
    await a.note_left("c", "p1")  # before the holder → p3 stays the holder (now at index 1)
    assert await a.admit("c", "p3", "x") == AdmitDecision.ADMITTED


async def test_party_arbiter_empty_roster_admits() -> None:
    a = PartyArbiter()  # no one noted joined (e.g. a direct in-process call) → degenerate admit
    assert await a.admit("c", "p1", "x") == AdmitDecision.ADMITTED


# --- cross-phase review fixes: PvP guard, adopt-clobber guard, roster refcount ---


async def test_pvp_is_not_auto_resolved(store: PostgresEventStore) -> None:
    # A party member's single beat must NOT auto-resolve a fight against ANOTHER player's PC
    # (down + loot with no agency). Attacking another active PC falls back to free-roam.
    world, campaign = await _two_pc_campaign(store)  # a:alice (p1), a:bob (p2), both sheeted PCs
    plan = (
        '{"intent_class":"action","triggers":["violence"],'
        '"mechanics":[{"affordance":"attack","target":"a:bob"}]}'
    )
    rec = _Recorder(plan)
    result = await Engine(store, ProviderRouter(bindings={}, default=rec), ruleset=RS).run_beat(
        campaign, "p1", "I swing at Bob"
    )
    async with store.pool.acquire() as conn:
        types = [
            r["event_type"]
            for r in await conn.fetch(
                "SELECT event_type FROM events WHERE commit_id = $1", result.commit_id
            )
        ]
    assert "EncounterStarted" not in types and "ModeChanged" not in types  # PvP not auto-resolved
    assert "BeatResolved" in types  # committed as an ordinary free-roam beat
    assert (await store.get_sheet(world.main_branch_id, "a:bob"))["hp"] > 0  # Bob is unharmed


async def test_bind_pc_rejects_adopting_an_already_active_pc(store: PostgresEventStore) -> None:
    # Two players can't share a PC: adopting an actor already an active PC would clobber the first
    # participant's binding via the projector's actor-keyed upsert — so it is refused.
    _, campaign = await _two_pc_campaign(store)  # a:alice is p1's active PC
    with pytest.raises(ValueError, match="already an active PC"):
        await store.bind_pc(campaign.campaign_id, "p3", adopt_actor_id="a:alice")
    # p1's binding is intact — no clobber
    assert await store.pc_for_participant(campaign.campaign_id, "p1") == "a:alice"


async def test_party_arbiter_refcounts_connections() -> None:
    # A second connection for the same participant, then closing ONE, must not drop them from the
    # ring (a second device / an overlapping reconnect).
    a = PartyArbiter()
    await a.note_joined("c", "p1")
    await a.note_joined("c", "p2")
    await a.note_joined("c", "p1")  # p1's second connection
    await a.note_left("c", "p1")  # one of p1's two closes — p1 stays in the ring
    assert await a.admit("c", "p1", "x") == AdmitDecision.ADMITTED  # still the holder
    await a.note_left("c", "p1")  # p1's last connection closes — now truly gone
    # ring is now [p2]; p2 holds, p1 is not a member
    assert await a.admit("c", "p2", "x") == AdmitDecision.ADMITTED
    assert await a.admit("c", "p1", "x") == AdmitDecision.NOT_YOUR_TURN
