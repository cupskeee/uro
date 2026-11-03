"""D-84: the first beat after the clock moved OUTSIDE play — a skip, a game's pass_time, a
time-skip fork — tells the narrator, beside the intent, how many days went by since the turns it
is shown. An intent's own days don't (its beat narrated them), ordinary beats are unchanged, and
only an integer comes from state."""

from pathlib import Path

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import CausedBy
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.recall import build_narrator_messages, time_passed_line
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.router import ProviderRouter
from uro_core.timeline.models import FORK_TIME_SKIP, Campaign
from uro_core.worldpack.create import import_pack
from uro_core.worldpack.parse import parse_pack

WORLDS = Path(__file__).resolve().parents[3] / "worlds"


async def _setup(store: PostgresEventStore) -> tuple[Engine, Campaign]:
    world = (await import_pack(store, parse_pack(WORLDS / "ashfall"))).world  # weekly agendas
    campaign = await store.start_campaign(
        world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="Wren"
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=StubProvider()))
    await engine.run_beat(campaign, "p1", "I step into the Brinehouse.")
    return engine, campaign


async def _told(engine: Engine, campaign: Campaign) -> str:
    """The narrator's final user message for a next beat."""
    ctx = await engine._context(campaign, "p1", "I look around.")
    messages, _ = await engine._prepare_narration(campaign, ctx, "I look around.")
    return messages[-1].content


async def test_a_skip_is_told_to_the_next_beat_only(store: PostgresEventStore) -> None:
    engine, campaign = await _setup(store)
    assert "TIME PASSED" not in await _told(engine, campaign)  # an ordinary beat
    await engine.agenda_tick(campaign.branch_id, 7)  # its agendas commit after it, as module beats
    assert time_passed_line(7) in await _told(engine, campaign)
    await engine.run_beat(campaign, "p1", "I look around.")
    assert "TIME PASSED" not in await _told(engine, campaign)  # told once, then the scene moves


async def test_a_night_the_player_narrated_isnt_told_again(store: PostgresEventStore) -> None:
    engine, campaign = await _setup(store)
    await engine.run_beat(campaign, "p1", "I sleep until morning.")  # D-77: its own TimeAdvanced
    assert await store.current_world_time(campaign.branch_id) == 1
    assert "TIME PASSED" not in await _told(engine, campaign)
    # a game's pass_time is time the turns above didn't show — unless it says it's the player's
    await engine.pass_time(campaign, 2, reason="caravan")
    assert time_passed_line(2) in await _told(engine, campaign)
    await engine.run_beat(campaign, "p1", "I look around.")
    player = CausedBy(kind="player_action", participant_id="p1")
    await engine.pass_time(campaign, 1, reason="play", caused_by=player)
    assert "TIME PASSED" not in await _told(engine, campaign)
    # the cause alone isn't enough: a player's advance under another reason is told (review)
    await engine.pass_time(campaign, 1, caused_by=player)
    assert time_passed_line(1) in await _told(engine, campaign)


async def test_a_time_skip_fork_is_told_and_a_fork_before_it_isnt(
    store: PostgresEventStore,
) -> None:
    engine, campaign = await _setup(store)
    head = (await store.get_branch(campaign.branch_id)).head_commit  # type: ignore[union-attr]
    later = await store.fork_branch(campaign.world_id, head, "a-year-on")  # type: ignore[arg-type]
    await engine.agenda_tick(later.branch_id, 365, reason=FORK_TIME_SKIP)
    on_later = campaign.model_copy(update={"branch_id": later.branch_id})
    assert time_passed_line(365) in await _told(engine, on_later)
    same = await store.fork_branch(campaign.world_id, head, "no-gap")  # type: ignore[arg-type]
    on_same = campaign.model_copy(update={"branch_id": same.branch_id})
    assert "TIME PASSED" not in await _told(engine, on_same)


async def test_with_no_gap_the_narrator_prompt_is_unchanged(store: PostgresEventStore) -> None:
    engine, campaign = await _setup(store)
    ctx = await engine._context(campaign, "p1", "I look around.")
    assert ctx.recall.days_since_last_scene == 0
    messages, _ = await engine._prepare_narration(campaign, ctx, "I look around.")
    style, env = await engine._prompt_style(campaign.branch_id)
    golden = build_narrator_messages(
        ctx.recall, "I look around.", pc_actor_id=ctx.pc_actor_id, style=style, env=env
    )
    assert [(m.role, m.content) for m in messages] == [(m.role, m.content) for m in golden]


def test_the_line_names_only_a_count() -> None:
    assert time_passed_line(1).startswith("TIME PASSED: 1 day has gone by")
    assert time_passed_line(14).startswith("TIME PASSED: 14 days have gone by")
