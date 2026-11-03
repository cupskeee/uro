"""D-95: the pacing director. Each beat records the live threads it brought into play (the
extractor's `develops` among the KNOWN threads, plus any it created or resolved) and the quiet
thread the engine named; once a live thread has gone `quiet_after` beats without either, the next
beat names the quietest one to the narrator beside the intent, at most once every `every` beats. A
pure function of the branch: a dry run, a fork and a replay name the same thread."""

import json
import re
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    BeatResolvedPayload,
    DomainEvent,
    actor_created,
    sheet_updated,
    thread_created,
    thread_state_changed,
)
from uro_core.domain.ids import new_id
from uro_core.errors import ExportError, PackError
from uro_core.export import stamp_chain
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.extraction import Extraction, parse_extraction, touched_threads
from uro_core.pipeline.pacing import (
    PacingSettings,
    pacing_line,
    pacing_report,
    quiet_thread,
    thread_pacing,
    world_settings,
)
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import CharSpec
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic
from uro_core.timeline.models import Campaign, ThreadView
from uro_core.worldpack.create import import_pack
from uro_core.worldpack.parse import parse_pack
from uro_core.worldpack.rules import Rule

WORLDS = Path(__file__).resolve().parents[3] / "worlds"


def _t(tid: str, state: str = "active", stakes: str = "") -> ThreadView:
    return ThreadView(thread_id=tid, stakes=stakes or f"plot {tid}", state=state, provenance="x")


def _b(threads: tuple[str, ...] = (), paced: str = "") -> BeatResolvedPayload:
    return BeatResolvedPayload(
        beat_id=new_id(),
        participant_id="p1",
        intent_text="i",
        narration="n",
        threads=list(threads),
        paced_thread=paced,
    )


# --- the policy (pure) ---


def test_nothing_is_quiet_before_quiet_after_beats_are_played() -> None:
    s = PacingSettings(quiet_after=3, every=1)
    threads = [_t("t:a")]
    assert quiet_thread(threads, [_b(), _b()], s) is None
    assert quiet_thread(threads, [_b(), _b(), _b()], s) == threads[0]


def test_the_quietest_live_thread_is_named() -> None:
    s = PacingSettings(quiet_after=2, every=1)
    threads = [_t("t:a"), _t("t:b"), _t("t:c", state="dormant"), _t("t:d", state="resolved")]
    recent = [_b(("t:a",)), _b(("t:b",)), _b(), _b()]
    assert quiet_thread(threads, recent, s) == threads[0]  # a: 3 quiet beats, b: 2
    # a thread unseen in the whole window counts as quietest; dormant/resolved never named
    threads.append(_t("t:e"))
    assert quiet_thread(threads, recent, s) == threads[-1]
    report = {p.thread_id: (p.quiet_beats, p.at_least) for p in thread_pacing(threads, recent)}
    assert report == {"t:a": (3, False), "t:b": (2, False), "t:e": (4, True)}


def test_a_thread_that_came_up_recently_is_not_named() -> None:
    s = PacingSettings(quiet_after=3, every=1)
    assert quiet_thread([_t("t:a")], [_b(), _b(), _b(("t:a",))], s) is None


def test_naming_counts_as_touching_and_spacing_holds() -> None:
    s = PacingSettings(quiet_after=2, every=3)
    threads = [_t("t:a"), _t("t:b")]
    named = [_b(), _b(), _b(paced="t:a")]
    assert quiet_thread(threads, named, s) is None  # one was named in the last `every` beats
    later = [*named, _b(), _b()]
    assert quiet_thread(threads, later, s) == threads[1]  # b; a was named, so it waits


def test_ties_go_to_the_stores_order_and_off_means_never() -> None:
    threads = [_t("t:a"), _t("t:b")]
    recent = [_b(), _b(), _b()]
    assert quiet_thread(threads, recent, PacingSettings(quiet_after=2, every=1)) == threads[0]
    assert quiet_thread(threads, recent, PacingSettings(quiet_after=0)) is None
    assert quiet_thread([], recent, PacingSettings(quiet_after=2)) is None


def test_settings_are_validated() -> None:
    with pytest.raises(ValidationError):
        PacingSettings.model_validate({"quiet_afterr": 5})  # a misspelt key is an error
    with pytest.raises(ValidationError):
        PacingSettings(quiet_after=-1)
    with pytest.raises(ValidationError):
        PacingSettings(every=0)
    assert PacingSettings(quiet_after=10, every=30).window == 30


def test_the_line_names_the_plot_and_leaves_the_action_first() -> None:
    line = pacing_line("The salt wagons have stopped coming.")
    assert line.startswith("A PLOT TO KEEP ALIVE: The salt wagons have stopped coming. If it")
    assert "Never at the cost of the player's action" in line


def test_touched_threads_keeps_known_live_ids_and_the_beats_own_threads() -> None:
    threads = [_t("t:a"), _t("t:b", state="dormant")]
    ex = Extraction(develops=["t:a", "t:b", "t:nope", "t:a"], resolves=[])
    events = [
        thread_created(thread_id="t:new", stakes="a new plot", state="active"),
        thread_state_changed(thread_id="t:old", to_state="resolved", from_state="active"),
    ]
    assert touched_threads(ex, threads, events) == ["t:a", "t:new", "t:old"]
    parsed = parse_extraction('{"develops": ["t:a", 3, null], "claims": []}')
    assert parsed is not None and parsed.develops == ["t:a"]


# --- the engine ---


class _Extracting(StubProvider):
    """The stub's narration; an extractor that reports every KNOWN thread whose id the
    narration quotes (the stub narrates the intent, so an intent naming `t:x` develops it)."""

    async def complete(self, req: CompletionRequest) -> str:
        if req.stage_tag != "extractor":
            return await super().complete(req)
        text = req.messages[-1].content
        narration = text.split("NARRATION:\n", 1)[1]
        known = text.split("KNOWN THREADS:\n", 1)[1].split("\n\n", 1)[0]
        ids = re.findall(r"\[(t:[\w-]+)\]", known)
        return json.dumps({"develops": [i for i in ids if i in narration], "claims": []})


async def _world(store: PostgresEventStore, pacing: dict[str, int]) -> tuple[Engine, Campaign]:
    world = await store.create_world(
        f"pacing-{new_id()}",
        pacing=pacing,
        extra_events=[
            thread_created(thread_id="t:wagons", stakes="The salt wagons stopped.", state="active"),
            thread_created(thread_id="t:heron", stakes="The Grey Heron is lost.", state="offered"),
            thread_created(thread_id="t:sleeper", stakes="Something sleeps.", state="dormant"),
        ],
    )
    campaign = await store.start_campaign(
        world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="Wren"
    )
    return Engine(store, ProviderRouter(bindings={}, default=_Extracting())), campaign


async def _told(engine: Engine, campaign: Campaign) -> str:
    ctx = await engine._context(campaign, "p1", "I look around.")
    messages, _ = await engine._prepare_narration(campaign, ctx, "I look around.")
    return messages[-1].content


def _beat(events: list[DomainEvent]) -> BeatResolvedPayload:
    return BeatResolvedPayload(**next(e.payload for e in events if e.event_type == "BeatResolved"))


async def test_a_quiet_thread_is_named_once_then_waits(store: PostgresEventStore) -> None:
    engine, campaign = await _world(store, {"quiet_after": 3, "every": 2})
    await engine.run_beat(
        campaign, "p1", "I ask about the wagons [t:wagons] and the heron [t:heron]."
    )
    last = (await store.recent_beats(campaign.branch_id, 1))[-1]
    assert sorted(last.threads) == ["t:heron", "t:wagons"] and last.paced_thread == ""
    await engine.run_beat(campaign, "p1", "I check the wagons again [t:wagons].")
    await engine.run_beat(campaign, "p1", "I look around.")
    assert "A PLOT TO KEEP ALIVE" not in await _told(engine, campaign)  # heron quiet only 2
    await engine.run_beat(campaign, "p1", "I look around.")
    told = await _told(engine, campaign)  # heron quiet 3, wagons 2 → heron
    assert told.count(pacing_line("The Grey Heron is lost.")) == 1
    assert told.index("A PLOT TO KEEP ALIVE") < told.index("PLAYER'S ACTION")  # beside the intent
    # a dry run names the same thread and commits nothing; the real beat records it
    assert _beat(await engine.preview_beat(campaign, "p1", "I wait.")).paced_thread == "t:heron"
    await engine.run_beat(campaign, "p1", "I wait.")
    assert (await store.recent_beats(campaign.branch_id, 1))[-1].paced_thread == "t:heron"
    # within `every` beats nothing is named; then the next quietest (the dormant one never)
    assert "A PLOT TO KEEP ALIVE" not in await _told(engine, campaign)
    await engine.run_beat(campaign, "p1", "I wait.")
    assert pacing_line("The salt wagons stopped.") in await _told(engine, campaign)
    report = await engine.pacing(campaign.branch_id)
    assert report.next_paced == "t:wagons"
    assert {t.thread_id for t in report.threads} == {"t:wagons", "t:heron"}


async def test_a_fork_names_what_the_original_would(store: PostgresEventStore) -> None:
    engine, campaign = await _world(store, {"quiet_after": 2, "every": 1})
    for _ in range(2):
        await engine.run_beat(campaign, "p1", "I look around.")
    head = (await store.get_branch(campaign.branch_id)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(campaign.world_id, head, "what-if")  # type: ignore[arg-type]
    on_fork = campaign.model_copy(update={"branch_id": fork.branch_id})
    assert await _told(engine, on_fork) == await _told(engine, campaign)
    assert "A PLOT TO KEEP ALIVE" in await _told(engine, on_fork)


async def test_pacing_off_names_nothing_and_bare_has_no_director(
    store: PostgresEventStore,
) -> None:
    engine, campaign = await _world(store, {"quiet_after": 0})
    for _ in range(3):
        await engine.run_beat(campaign, "p1", "I look around.")
    assert "A PLOT TO KEEP ALIVE" not in await _told(engine, campaign)
    assert (await engine.pacing(campaign.branch_id)).next_paced == ""
    engine, campaign = await _world(store, {"quiet_after": 1, "every": 1})
    bare = Engine(store, ProviderRouter(bindings={}, default=_Extracting()), bare=True)
    await bare.run_beat(campaign, "p1", "I look around.")
    assert "A PLOT TO KEEP ALIVE" not in await _told(bare, campaign)
    assert "A PLOT TO KEEP ALIVE" in await _told(engine, campaign)


async def test_the_default_waits_25_beats(store: PostgresEventStore) -> None:
    _, campaign = await _world(store, {})
    report = await pacing_report(store, campaign.branch_id)
    assert report.settings == PacingSettings() and report.settings.quiet_after == 25
    assert report.beats_read == 0 and report.next_paced == ""


async def test_a_bad_pacing_setting_is_refused_at_creation(store: PostgresEventStore) -> None:
    with pytest.raises(ValueError):
        await store.create_world(f"bad-{new_id()}", pacing={"quiet_after": -2})


async def test_a_pack_declares_its_pacing_and_it_travels(
    store: PostgresEventStore, tmp_path: Path
) -> None:
    pack = tmp_path / "ashfall"
    shutil.copytree(WORLDS / "ashfall", pack)
    toml = pack / "world.toml"
    toml.write_text(toml.read_text() + "\n[pacing]\nquiet_after = 12\nevery = 4\n")
    parsed = parse_pack(pack)
    assert parsed.manifest.pacing == PacingSettings(quiet_after=12, every=4)
    world = (await import_pack(store, parsed)).world
    assert await store.world_pacing(world.main_branch_id) == {"quiet_after": 12, "every": 4}
    # a default pack stores no settings, so its genesis is as before D-95
    plain = (await import_pack(store, parse_pack(WORLDS / "ashfall"))).world
    assert await store.world_pacing(plain.main_branch_id) == {}
    # and it travels with the world
    imported = await store.import_world(await store.export_world(world.world_id))
    assert await store.world_pacing(imported.main_branch_id) == {"quiet_after": 12, "every": 4}
    toml.write_text(toml.read_text().replace("every = 4", "evry = 4"))  # a misspelt key
    with pytest.raises(PackError):
        parse_pack(pack)


async def test_an_engine_can_set_pacing_over_the_worlds(store: PostgresEventStore) -> None:
    engine, campaign = await _world(store, {"quiet_after": 0})  # the world turns it off
    for _ in range(2):
        await engine.run_beat(campaign, "p1", "I look around.")
    on = Engine(
        store,
        ProviderRouter(bindings={}, default=_Extracting()),
        pacing=PacingSettings(quiet_after=2, every=1),
    )
    assert "A PLOT TO KEEP ALIVE" in await _told(on, campaign)
    assert (await on.pacing(campaign.branch_id)).next_paced == "t:heron"
    assert "A PLOT TO KEEP ALIVE" not in await _told(engine, campaign)


# --- review fixes (D-95 phase-end review) ---


async def test_a_fight_beat_names_no_plot_and_records_none(store: PostgresEventStore) -> None:
    rs = UroBasic()
    world = await store.create_world(
        f"pacing-fight-{new_id()}",
        pacing={"quiet_after": 1, "every": 1},
        extra_events=[
            thread_created(thread_id="t:wagons", stakes="The salt wagons stopped.", state="active"),
            actor_created(actor_id="a:grull", name="Grull", tier=2, role="brute"),
            sheet_updated(
                actor_id="a:grull",
                sheet=rs.new_character(
                    CharSpec(data={"abilities": {"STR": 20, "CON": 20}}), Rng(0)
                ),
                ruleset_id="uro-basic",
            ),
        ],
    )
    campaign = await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Ada",
        new_pc_id="a:ada",
        pc_sheet=rs.new_character(CharSpec(data={"abilities": {"STR": 14}}), Rng(0)),
        ruleset_id="uro-basic",
        seed=7,
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Extracting()), ruleset=rs)
    await engine.run_beat(campaign, "p1", "I look around.")
    attack = BeatPlan(
        triggers=["violence"],
        mechanics=[PlanMechanic(affordance="attack", actor="a:ada", target="a:grull")],
    )
    ctx = await engine._context(campaign, "p1", "I swing at Grull", attack)
    assert ctx.paced_thread is not None  # due, but…
    messages, fight = await engine._prepare_narration(campaign, ctx, "I swing at Grull")
    assert fight is not None and "A PLOT TO KEEP ALIVE" not in messages[-1].content
    await engine.run_beat(campaign, "p1", "I swing at Grull", plan=attack)
    assert (await store.recent_beats(campaign.branch_id, 1))[-1].paced_thread == ""


async def test_the_streaming_path_records_what_run_beat_does(store: PostgresEventStore) -> None:
    engine, campaign = await _world(store, {"quiet_after": 1, "every": 1})
    await engine.run_beat(campaign, "p1", "I look around.")
    assert (await engine.pacing(campaign.branch_id)).next_paced == "t:heron"
    async for _ in engine.run_beat_stream(campaign, "p1", "I wait."):
        pass
    assert (await store.recent_beats(campaign.branch_id, 1))[-1].paced_thread == "t:heron"


async def test_a_thread_a_rule_wakes_is_named_next(store: PostgresEventStore) -> None:
    # A thread made live outside a beat (a rule wakes it) has not come up in the beats read, so
    # the next eligible beat names it: the world's news reaches the story (pinned, D-95 review).
    engine, campaign = await _world(store, {"quiet_after": 2, "every": 1})
    for _ in range(2):
        await engine.run_beat(
            campaign, "p1", "I check on the wagons [t:wagons] and heron [t:heron]."
        )
    assert (await engine.pacing(campaign.branch_id)).next_paced == ""
    await store.append_beat(
        campaign.branch_id,
        [thread_state_changed(thread_id="t:sleeper", to_state="active", from_state="dormant")],
    )
    report = await engine.pacing(campaign.branch_id)
    assert report.next_paced == "t:sleeper"
    woken = next(t for t in report.threads if t.thread_id == "t:sleeper")
    assert woken.at_least and woken.quiet_beats == report.beats_read


async def test_settings_fall_back_safely(store: PostgresEventStore) -> None:
    class _Old:  # an embedder's store from before D-95
        pass

    class _Bad:
        async def world_pacing(self, branch_id: str) -> dict[str, object]:
            return {"quiet_after": -3}

    assert await world_settings(_Old(), "b") == PacingSettings()
    assert (await world_settings(_Bad(), "b")).quiet_after == 0  # invalid → off, not a crash


async def test_an_import_refuses_a_bad_pacing_setting(store: PostgresEventStore) -> None:
    _, campaign = await _world(store, {"quiet_after": 4})
    bundle = await store.export_world(campaign.world_id)
    genesis = next(e for c in bundle.commits for e in c.events if e.event_type == "WorldGenesis")
    genesis.payload["pacing"] = {"quiet_after": -1}
    stamp_chain(bundle)  # a self-consistent chain: only the setting is wrong
    with pytest.raises(ExportError, match="pacing"):
        await store.import_world(bundle)


def test_a_rule_cannot_filter_on_a_list_field() -> None:
    rule = {
        "id": "on-wagons",
        "trigger": {"event": "BeatResolved", "where": {"threads": "t:wagons"}},
        "then": [{"do": "set_thread_state", "thread": "t:wagons", "to": "dormant"}],
        "scope": {"world": True},
    }
    with pytest.raises(ValidationError, match="list or a dict"):
        Rule.model_validate(rule)
