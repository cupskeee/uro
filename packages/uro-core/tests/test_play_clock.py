"""D-77: the clock follows play. A solo player's intent that passes whole days ("I sleep until
morning", "I rest for two days") moves the branch's day by its own TimeAdvanced commit, caused by
the player, after the beat and its reactions; the agendas the new day crosses then fire. No LLM:
a closed English grammar reads the intent (quoted speech stripped), gated here on a labelled
fixture. Speech, plans, a fight, a party's beat and a clock that moved mid-beat pass no day.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import CausedBy, DomainEvent
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.router import ProviderRouter
from uro_core.timeline.clock import MAX_DAYS, day_cue, parse_day_cue
from uro_core.timeline.models import Campaign
from uro_core.worldpack.create import import_pack
from uro_core.worldpack.parse import parse_pack

WORLDS = Path(__file__).resolve().parents[3] / "worlds"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "day_cues.json").read_text())


def _confusion(rows: list[list[Any]]) -> dict[str, int]:
    out = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "exact": 0}
    for intent, days in rows:
        found = parse_day_cue(intent)
        key = ("tp" if days else "fp") if found else ("fn" if days else "tn")
        out[key] += 1
        out["exact"] += bool(days and found == days)
    return out


def test_the_grammar_meets_its_gate_on_the_labelled_intents() -> None:
    # The design's ship gate (D-77): cue precision >= 0.9 and recall >= 0.7 on held-out intents,
    # the exact days on >= 90% of the cues found, never a fire on speech, plans or negation.
    for name in ("fresh", "targeted"):
        c = _confusion(FIXTURE[name])
        assert c["tp"] / (c["tp"] + c["fp"]) >= 0.9, (name, c)
        assert c["tp"] / (c["tp"] + c["fn"]) >= 0.7, (name, c)
        assert c["exact"] >= 0.9 * c["tp"], (name, c)
    assert _confusion(FIXTURE["fresh"]) == {"tp": 21, "fp": 0, "fn": 1, "tn": 38, "exact": 21}
    # the long run's own seven day-crossings (beat 62 is two days) and nothing else
    passed = [(i, parse_day_cue(t)) for i, (t, _) in enumerate(FIXTURE["long_run"])]
    assert [(i, d) for i, d in passed if d] == [
        (i, d) for i, (_, d) in enumerate(FIXTURE["long_run"]) if d
    ]
    assert sum(d for _, d in passed) == 7
    for intent in FIXTURE["play"] + FIXTURE["adversarial"]:
        assert parse_day_cue(intent) == 0, intent
    # the shapes the D-77 review found: dates and adjectives aren't durations, single-quoted and
    # whispered or read words are speech, wishes and questions aren't acts — and ordinary
    # openings ("Tonight I…", "Exhausted, I…") still pass their days
    for intent, days in FIXTURE["review"]:
        assert parse_day_cue(intent) == days, intent


def test_the_evidence_is_the_sentence_that_passes_the_days() -> None:
    assert day_cue('I pay Mera. "Three days pass," I joke. Then I sleep until dawn.') == (
        1,
        "Then I sleep until dawn",
    )
    assert day_cue("I rest for a week at the Lantern.") == (0, "")  # over the cap: a skip's job
    assert MAX_DAYS == 3


async def _ashfall(store: PostgresEventStore) -> tuple[Engine, Campaign]:
    world = (await import_pack(store, parse_pack(WORLDS / "ashfall"))).world
    campaign = await store.start_campaign(
        world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="Wren"
    )
    return Engine(store, ProviderRouter(bindings={}, default=StubProvider())), campaign


async def _day(store: PostgresEventStore, campaign: Campaign) -> int:
    return await store.current_world_time(campaign.branch_id)


async def test_a_night_in_play_moves_the_clock(store: PostgresEventStore) -> None:
    engine, campaign = await _ashfall(store)
    branch = campaign.branch_id
    result = await engine.run_beat(campaign, "p1", "I eat a bowl of stew. Then I sleep until dawn.")

    # the advance is its own commit, the last one, after the beat: a night, caused by the player
    head, *_ = await store.lineage(branch, 3)
    assert head.event_types == ["TimeAdvanced"] and head.commit_id != result.commit_id
    detail = await store.commit_detail(campaign.world_id, head.commit_id)
    (advanced,) = detail.events  # type: ignore[union-attr]
    assert advanced.payload == {
        "v": 1,
        "from_day": 0,
        "to_day": 1,
        "reason": "play",
        "evidence": "Then I sleep until dawn",
        "beat_id": result.beat_id,
    }
    assert advanced.caused_by["kind"] == "player_action"
    assert advanced.caused_by["participant_id"] == "p1"
    assert advanced.caused_by["beat_id"] == result.beat_id
    assert advanced.world_time["day"] == 1
    assert await _day(store, campaign) == 1

    # speech, a promise, a plan, someone else's night: no day passes, nothing is committed
    for intent in (
        'I tell Mera, "Three days pass before the tide turns."',
        "I promise to return in two days.",
        "I plan to leave at first light.",
        "I watch the guard sleep until morning.",
    ):
        await engine.run_beat(campaign, "p1", intent)
        (head,) = await store.lineage(branch, 1)
        assert "TimeAdvanced" not in head.event_types, intent
        assert await _day(store, campaign) == 1, intent

    # a fork at the beat that slept (before its advance) is still on day 0
    fork = await store.fork_branch(campaign.world_id, result.commit_id, "before-the-night")
    assert await store.current_world_time(fork.branch_id) == 0


async def test_days_in_play_fire_the_weekly_agendas(store: PostgresEventStore) -> None:
    engine, campaign = await _ashfall(store)
    branch = campaign.branch_id
    await engine.run_beat(campaign, "p1", "I rest at the Lantern for three days.")
    await engine.run_beat(campaign, "p1", "I ride the salt road for two days.")
    assert await _day(store, campaign) == 5
    assert await store.get_counter(branch, "f:saltborn", "war_heat") == 0  # no week crossed yet

    await engine.run_beat(campaign, "p1", "I camp by the road and sleep through the night.")
    await engine.run_beat(campaign, "p1", "I stay another night at the inn.")
    assert await _day(store, campaign) == 7
    # day 7 crossed in play: the advance, then the world's reaction as a module beat
    head, advance = await store.lineage(branch, 2)
    assert advance.event_types == ["TimeAdvanced"]
    assert {"CounterChanged", "BeliefChanged", "ThreadStateChanged"} <= set(head.event_types)
    assert await store.get_counter(branch, "f:saltborn", "war_heat") == 1
    held = [b for b in await store.beliefs_of(branch, "a:hesk") if b.claim_id == "c:wagon-burned"]
    assert held and held[0].learned_from == "a:weck"

    # it rebuilds by replay: a fork at the head and a re-imported world agree on all of it
    fork = await store.fork_branch(campaign.world_id, head.commit_id, "replayed")
    sections = ["counters", "beliefs", "threads", "edges"]
    a = (await store.query_across([branch], sections))[branch]
    b = (await store.query_across([fork.branch_id], sections))[fork.branch_id]
    assert a == b and await store.current_world_time(fork.branch_id) == 7
    imported = await store.import_world(await store.export_world(campaign.world_id))
    assert await store.current_world_time(imported.main_branch_id) == 7
    assert await store.get_counter(imported.main_branch_id, "f:saltborn", "war_heat") == 1


async def test_a_clock_moved_mid_beat_isnt_stacked_on(store: PostgresEventStore) -> None:
    engine, campaign = await _ashfall(store)
    branch = campaign.branch_id
    cause = CausedBy(kind="player_action", participant_id="p1")
    # the compare-and-set itself: a stale base commits nothing
    await store.time_skip(branch, 2)
    head = (await store.get_branch(branch)).head_commit  # type: ignore[union-attr]
    stale = await store.advance_day(
        branch, based_on_day=0, days=1, reason="play", evidence="I sleep", caused_by=cause
    )
    assert stale is None and (await store.get_branch(branch)).head_commit == head  # type: ignore[union-attr]

    # ...and through a beat: an operator's skip lands while the narrator is still talking
    real_stream = engine._router.stream

    def skip_mid_beat(*args: Any, **kwargs: Any) -> Any:
        async def gen() -> Any:
            await store.time_skip(branch, 3)
            async for chunk in real_stream(*args, **kwargs):
                yield chunk

        return gen()

    engine._router.stream = skip_mid_beat  # type: ignore[method-assign]
    await engine.run_beat(campaign, "p1", "I sleep until morning.")
    assert await _day(store, campaign) == 5  # 2 + 3; the night isn't added on top of the skip


async def test_a_party_a_fight_and_bare_play_pass_no_day(store: PostgresEventStore) -> None:
    engine, campaign = await _ashfall(store)
    # a fight passes no day (the beat's encounter events are what make it a fight)
    ctx = await engine._context(campaign, "p1", "I sleep until morning.")
    assert ctx.cue_days == 1
    fight: list[DomainEvent] = []
    assert await engine._days_passed(campaign, "p1", ctx, fight) == 0
    assert await engine._days_passed(campaign, "p1", ctx, None) == 1

    # someone without a seat (review): an unbound co-player drives the solo PC, but not the clock
    await engine.run_beat(campaign, "spectator", "I sleep until morning.")
    assert await _day(store, campaign) == 0
    # ...and nobody has one on a campaign with no PC
    no_pc = await store.create_campaign(campaign.world_id, campaign.branch_id)
    await engine.run_beat(no_pc, "p1", "I sleep until morning.")
    assert await _day(store, campaign) == 0

    # bare play (the ablation arm) never reads a cue
    bare = Engine(store, ProviderRouter(bindings={}, default=StubProvider()), bare=True)
    await bare.run_beat(campaign, "p1", "I sleep until morning.")
    assert await _day(store, campaign) == 0
    previewed = await bare.preview_beat(campaign, "p1", "I sleep until morning.")
    assert "TimeAdvanced" not in {e.event_type for e in previewed}

    # a second seated player: one player's night isn't the table's
    await store.bind_pc(campaign.campaign_id, "p2", new_pc_name="Ash")
    await engine.run_beat(campaign, "p1", "I sleep until morning.")
    assert await _day(store, campaign) == 0


async def test_a_dry_run_shows_the_advance_and_commits_nothing(store: PostgresEventStore) -> None:
    engine, campaign = await _ashfall(store)
    head = (await store.get_branch(campaign.branch_id)).head_commit  # type: ignore[union-attr]
    events = await engine.preview_beat(campaign, "p1", "I rest for two days to heal.")
    advanced = [e for e in events if e.event_type == "TimeAdvanced"]
    assert [(e.payload["from_day"], e.payload["to_day"]) for e in advanced] == [(0, 2)]
    assert advanced[0].caused_by.kind == "player_action"
    assert (await store.get_branch(campaign.branch_id)).head_commit == head  # type: ignore[union-attr]


async def test_a_skip_reads_its_window_from_its_own_commit(store: PostgresEventStore) -> None:
    # D-77 fixed with the factor-out: agenda_tick read the day BEFORE the skip took the branch
    # lock, so a concurrent advance shifted the window it evaluated. Here play passes 3 days in
    # between; the skip of 4 commits (3, 7], which crosses the weekly boundary, and the agendas run.
    engine, campaign = await _ashfall(store)
    branch = campaign.branch_id
    real_skip = store.time_skip

    async def raced(branch_id: str, days: int, *, reason: str = "time-skip") -> Any:
        await store.advance_day(
            branch_id,
            based_on_day=0,
            days=3,
            reason="play",
            evidence="I rest for three days",
            caused_by=CausedBy(kind="player_action", participant_id="p1"),
        )
        return await real_skip(branch_id, days, reason=reason)

    store.time_skip = raced  # type: ignore[method-assign]
    try:
        await engine.agenda_tick(branch, 4)
    finally:
        store.time_skip = real_skip  # type: ignore[method-assign]
    assert await _day(store, campaign) == 7
    assert await store.get_counter(branch, "f:saltborn", "war_heat") == 1


@pytest.mark.parametrize("days", [0, -1])
async def test_advance_day_refuses_no_days(store: PostgresEventStore, days: int) -> None:
    _, campaign = await _ashfall(store)
    with pytest.raises(ValueError):
        await store.advance_day(
            campaign.branch_id,
            based_on_day=0,
            days=days,
            reason="play",
            evidence="",
            caused_by=CausedBy(kind="player_action"),
        )
