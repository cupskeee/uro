"""D-115 (Uro Loom request 24): a saved beat says again what its commit said.

`beat_records` groups a branch's events into beats with their checks, mode, what they created and
the plots that moved until the next beat; `for_players` keeps a roll's player view only on the
acting participant's own character. Read from the branch's history, so a fork tells the same
past."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import start_campaign
from uro_core.domain.events import actor_created, beat_resolved, check_resolved
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.timeline.beats import beat_records, for_players, without_views
from uro_server.app import create_app, engine_deps


def _beat(n: int, who: str = "p1") -> dict[str, Any]:
    return {"beat_id": f"b{n}", "participant_id": who, "intent_text": f"i{n}", "narration": f"n{n}"}


def test_events_group_into_beats_with_what_followed_each() -> None:
    rows = [
        ("c0", "ThreadStateChanged", {"thread_id": "t:old", "to_state": "active"}),  # before any
        ("c1", "CheckResolved", {"actor_id": "a:pc", "affordance": "persuade",
                                 "outcome": "success", "player_view": {"dice": [14]}}),
        ("c1", "CheckResolved", {"actor_id": "a:npc", "affordance": "perceive",
                                 "outcome": "failure", "player_view": {"dice": [3]}}),
        ("c1", "ActorCreated", {"actor_id": "a:weck", "name": "Old Weck"}),
        ("c1", "ThreadCreated", {"thread_id": "t:debt", "stakes": "Weck's debt",
                                 "state": "active"}),
        ("c1", "ThreadStateChanged", {"thread_id": "t:feud", "from_state": "active",
                                      "to_state": "resolved"}),
        ("c1", "BeatResolved", _beat(1)),
        # the world's rules react, then a day passes: both follow beat 1
        ("c2", "ThreadCreated", {"thread_id": "t:hunt", "stakes": "The hunt", "state": "dormant"}),
        ("c3", "ThreadStateChanged", {"thread_id": "t:hunt", "from_state": "dormant",
                                      "to_state": "active"}),
        ("c3", "ThreadStateChanged", {"thread_id": "t:tide", "from_state": "active",
                                      "to_state": "escalating"}),
        ("c3", "ThreadStateChanged", {"thread_id": "t:tide", "from_state": "escalating",
                                      "to_state": "active"}),  # there and back: nothing moved
        ("c4", "EncounterStarted", {"encounter_id": "e:1"}),
        ("c4", "BeatResolved", _beat(2, "p2")),
    ]  # fmt: skip
    stakes = {"t:feud": "The feud", "t:hunt": "The hunt", "t:tide": "The tide"}
    one, two = beat_records(rows, stakes)
    assert (one.beat.beat_id, one.commit_id, one.mode) == ("b1", "c1", "freeroam")
    assert [(c.actor_id, c.outcome) for c in one.checks] == [
        ("a:pc", "success"),
        ("a:npc", "failure"),
    ]
    assert [(c.kind, c.id, c.name) for c in one.created] == [
        ("actor", "a:weck", "Old Weck"),
        ("thread", "t:debt", "Weck's debt"),
    ]
    assert [t.model_dump() for t in one.threads_changed] == [
        {"thread_id": "t:feud", "stakes": "The feud", "from_state": "active",
         "to_state": "resolved"},
        {"thread_id": "t:hunt", "stakes": "The hunt", "from_state": "", "to_state": "active"},
    ]  # fmt: skip
    assert (two.mode, two.checks, two.created, two.threads_changed) == ("encounter", [], [], [])

    # a player reads the dice of the character the beat was played as, never an NPC's
    assert [c.player_view for c in for_players([one])[0].checks] == [None, None]  # not recorded
    played = one.model_copy(update={"beat": one.beat.model_copy(update={"pc_actor_id": "a:pc"})})
    assert [c.player_view for c in for_players([played])[0].checks] == [{"dice": [14]}, None]
    assert [c.player_view for c in without_views([played])[0].checks] == [None, None]
    assert one.checks[1].player_view == {"dice": [3]}  # the records themselves are untouched


class _Narrator:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "The hall goes quiet."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def test_a_played_story_reads_back_its_rolls_and_fights_and_a_fork_keeps_them(
    store: PostgresEventStore,
) -> None:
    world = await store.create_world(f"Rec-{new_id()}")
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="Ada", seed=2
    )
    await store.append_beat(c.branch_id, [actor_created(actor_id="a:brute", name="Brute")])
    engine = Engine(store, ProviderRouter(bindings={}, default=_Narrator()), ruleset=UroBasic())
    talk = BeatPlan(
        triggers=["change_disposition"], mechanics=[PlanMechanic(affordance="persuade")]
    )
    fight = BeatPlan(
        triggers=["violence"], mechanics=[PlanMechanic(affordance="attack", target="a:brute")]
    )
    played = [
        await engine.run_beat(c, "p1", "I look around"),
        await engine.run_beat(c, "p1", "I talk the brute down", plan=talk),
        await engine.run_beat(c, "p1", "I swing at the brute", plan=fight),
    ]
    records = await store.beat_records(c.branch_id, 10)
    assert [r.beat.intent_text for r in records] == [
        "I look around",
        "I talk the brute down",
        "I swing at the brute",
    ]
    assert [r.commit_id for r in records] == [b.commit_id for b in played]
    assert [r.mode for r in records] == [b.mode for b in played] == [
        "freeroam", "freeroam", "encounter",
    ]  # fmt: skip
    # the saved check is the one the live beat reported, dice and all
    (live,) = played[1].outcomes
    (saved,) = records[1].checks
    assert saved.model_dump() == live.model_dump() and saved.player_view is not None
    assert records[1].beat.pc_actor_id == await store.pc_for_participant(c.campaign_id, "p1")
    assert for_players(records)[1].checks[0].player_view == saved.player_view
    assert records[0].checks == [] and records[2].checks == []
    # the newest ones only, still oldest first
    assert [r.beat.intent_text for r in await store.beat_records(c.branch_id, 2)] == [
        "I talk the brute down",
        "I swing at the brute",
    ]
    # a fork tells the same past
    head = (await store.get_branch(c.branch_id)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(c.world_id, head, f"what-if-{new_id()}")  # type: ignore[arg-type]
    assert await store.beat_records(fork.branch_id, 10) == records
    empty = await store.create_world(f"Empty-{new_id()}")
    assert await store.beat_records(empty.main_branch_id, 10) == []


async def test_the_campaign_chronicle_over_rest_shows_a_player_their_own_dice_only(
    store: PostgresEventStore,
) -> None:
    world = await store.create_world(f"Rest-{new_id()}")
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="alice", new_pc_name="Ada"
    )
    pc = await store.pc_for_participant(c.campaign_id, "alice")
    assert pc is not None
    await store.append_beat(
        c.branch_id,
        [
            check_resolved(actor_id=pc, affordance="persuade", outcome="success",
                           player_view={"dice": [14], "band": "success"}),
            check_resolved(actor_id="a:sage", affordance="perceive", outcome="failure",
                           player_view={"dice": [2]}),
            beat_resolved(beat_id=new_id(), participant_id="alice", intent_text="go",
                          narration="Done.", pc_actor_id=pc),
        ],
    )  # fmt: skip
    engine = Engine(store, ProviderRouter(bindings={}, default=_Narrator()))
    deps = engine_deps(store, engine, {"tok": "alice"}, admin_tokens=set())
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get(f"/campaigns/{c.campaign_id}/chronicle?token=tok")
    assert r.status_code == 200
    (beat,) = r.json()["beats"]
    assert beat["narration"] == "Done." and beat["mode"] == "freeroam" and beat["commit_id"]
    assert beat["checks"] == [
        {"actor_id": pc, "affordance": "persuade", "outcome": "success",
         "player_view": {"dice": [14], "band": "success"}},
        {"actor_id": "a:sage", "affordance": "perceive", "outcome": "failure",
         "player_view": None},
    ]  # fmt: skip
    assert beat["created"] == [] and beat["threads_changed"] == []
