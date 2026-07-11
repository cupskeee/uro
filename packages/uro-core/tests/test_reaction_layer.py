"""Reaction Layer INC-1: the post-beat hook (docs/17, D-33). Deterministic — no LLM.

Proves the mechanism a hardcoded trusted rule stands in for the future pack interpreter: after a
beat commits, a post-beat pass reads the just-committed state and commits any consequence as a
SEPARATE caused_by=module beat — and that consequence survives a fork (rebuilt by replay), exactly
like any other event. INC-3 replaces the hardcoded rule body with the pack-data interpreter.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    actor_created,
    actor_died,
    edge_added,
    faction_created,
    thread_created,
)
from uro_core.domain.ids import new_id
from uro_core.errors import PackError
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.worldpack.parse import parse_pack

WORLDS = Path(__file__).resolve().parents[3] / "worlds"


class _Stub:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "x"

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _engine(store: PostgresEventStore) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=_Stub()))


# A pack rule: when someone dies, a dormant feud thread wakes. This is the death→activate mechanism
# INC-1 hardcoded, now expressed as pack DATA (INC-3 evaluates it through the interpreter+gauntlet).
_FEUD_PACK = {
    "rules_api_version": 1,
    "rules": [
        {
            "id": "death-wakes-the-feud",
            "trigger": {"event": "ActorDied"},
            "when": {"kind": "thread_state", "thread": "t:feud", "state": "dormant"},
            "then": [{"do": "set_thread_state", "thread": "t:feud", "to": "active"}],
            "scope": {"thread": "t:feud"},
        }
    ],
}


async def _campaign_with_rule(store: PostgresEventStore, *, feud_state: str = "dormant"):  # type: ignore[no-untyped-def]
    world = await store.create_world(f"react-{new_id()}", rule_pack=_FEUD_PACK)
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [thread_created(thread_id="t:feud", stakes="the miners' feud", state=feud_state)],
    )
    return world, campaign


async def _head(store: PostgresEventStore, branch_id: str) -> str:
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT head_commit FROM branches WHERE branch_id = $1", branch_id
        )


async def _states(store: PostgresEventStore, branch_id: str) -> dict[str, str]:
    return {t.thread_id: t.state for t in await store.list_threads(branch_id)}


async def test_death_activates_dormant_thread_as_a_module_beat(store: PostgresEventStore) -> None:
    _, campaign = await _campaign_with_rule(store)
    branch = campaign.branch_id
    died = [actor_died(actor_id="a:mook", cause="slain in the brawl")]
    await store.append_beat(branch, died)  # the trigger beat (a death committed)
    await _engine(store).react(campaign, await _head(store, branch), died)

    assert (await _states(store, branch))["t:feud"] == "active"  # the dormant thread woke
    # the consequence is one module-caused ThreadStateChanged — auditable, un-laundered provenance
    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT e.caused_by FROM events e JOIN commits c ON c.commit_id = e.commit_id "
            "JOIN branches b ON b.world_id = c.world_id "
            "WHERE b.branch_id = $1 AND e.event_type = 'ThreadStateChanged'",
            branch,
        )
    assert len(rows) == 1
    cb = rows[0]["caused_by"]
    cb = cb if isinstance(cb, dict) else json.loads(cb)
    assert cb["kind"] == "module" and cb["rule_id"] == "death-wakes-the-feud"


async def test_reaction_survives_a_fork(store: PostgresEventStore) -> None:
    world, campaign = await _campaign_with_rule(store)
    branch = campaign.branch_id
    await store.append_beat(branch, [actor_died(actor_id="a:mook")])
    await _engine(store).react(
        campaign, await _head(store, branch), [actor_died(actor_id="a:mook")]
    )
    # fork AFTER the reaction — the module consequence must rebuild by replay on the sibling
    fork = await store.fork_branch(world.world_id, await _head(store, branch), "aftermath")
    assert (await _states(store, fork.branch_id))["t:feud"] == "active"


async def test_no_trigger_event_is_a_no_op(store: PostgresEventStore) -> None:
    _, campaign = await _campaign_with_rule(store)
    branch = campaign.branch_id
    head_before = await _head(store, branch)
    await _engine(store).react(campaign, head_before, [])  # no ActorDied → trigger doesn't match
    assert (await _states(store, branch))["t:feud"] == "dormant"  # untouched
    assert await _head(store, branch) == head_before  # no empty module commit


async def test_condition_gate_blocks_the_rule(store: PostgresEventStore) -> None:
    # the feud is already active — the rule's `when: thread_state dormant` is false → no fire
    _, campaign = await _campaign_with_rule(store, feud_state="active")
    branch = campaign.branch_id
    head_before = await _head(store, branch)
    await _engine(store).react(campaign, head_before, [actor_died(actor_id="a:x")])
    assert await _head(store, branch) == head_before  # condition unmet → nothing committed


async def test_no_rule_pack_short_circuits(store: PostgresEventStore) -> None:
    # a rule-less world pays nothing — the pass returns before touching state
    world = await store.create_world(f"react-{new_id()}")  # no rule_pack
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    head_before = await _head(store, campaign.branch_id)
    await _engine(store).react(campaign, head_before, [actor_died(actor_id="a:x")])
    assert await _head(store, campaign.branch_id) == head_before


# --- INC-2: pack rule format, version pin, inline WorldGenesis carry (docs/17) ---


def test_ashfall_rule_pack_parses() -> None:
    pack = parse_pack(WORLDS / "ashfall")
    assert pack.rule_pack is not None
    assert pack.rule_pack.rules_api_version == 1
    ids = [r.id for r in pack.rule_pack.rules]
    assert "ritual-reckons-on-death" in ids


def test_rule_pack_bad_version_fails_loud(tmp_path: Path) -> None:
    (tmp_path / "world.toml").write_text('[world]\nname = "V"\n')
    (tmp_path / "rules.yaml").write_text("rules_api_version: 999\nrules: []\n")
    with pytest.raises(PackError, match="rules_api_version"):
        parse_pack(tmp_path)


def test_rule_pack_malformed_action_fails_loud(tmp_path: Path) -> None:
    (tmp_path / "world.toml").write_text('[world]\nname = "V"\n')
    (tmp_path / "rules.yaml").write_text(
        "rules_api_version: 1\n"
        "rules:\n"
        "  - id: bad\n"
        "    trigger: { event: ActorDied }\n"
        "    then:\n"
        "      - do: cast_fireball\n"  # not in the closed Action union → rejected
        "    scope: { thread: t:x }\n"
    )
    with pytest.raises(PackError):
        parse_pack(tmp_path)


def test_no_rule_pack_is_none(tmp_path: Path) -> None:
    (tmp_path / "world.toml").write_text('[world]\nname = "V"\n')
    assert parse_pack(tmp_path).rule_pack is None  # a pack without rules.yaml is fine


async def test_worldgenesis_carries_the_rule_pack_inline(store: PostgresEventStore) -> None:
    # Decided-OQ: rule content is stamped INLINE in WorldGenesis (like prompt_overrides) so an
    # exported→imported world stays self-contained (reactions fire without the pack files).
    pack = parse_pack(WORLDS / "ashfall")
    world = await store.create_world(
        pack.manifest.name, rule_pack=pack.rule_pack.model_dump() if pack.rule_pack else {}
    )
    async with store.pool.acquire() as conn:
        payload = await conn.fetchval(
            "SELECT e.payload FROM events e JOIN commits c ON c.commit_id = e.commit_id "
            "JOIN branches b ON b.world_id = c.world_id "
            "WHERE b.branch_id = $1 AND e.event_type = 'WorldGenesis'",
            world.main_branch_id,
        )
    payload = payload if isinstance(payload, dict) else json.loads(payload)
    assert payload["rule_pack"]["rules_api_version"] == 1
    assert payload["rule_pack"]["rules"][0]["id"] == "ritual-reckons-on-death"


# --- INC-3: the interpreter + gauntlet (scope, forced testimony, determinism) ---


async def test_out_of_scope_action_is_dropped(store: PostgresEventStore) -> None:
    # a thread-scoped rule may only touch its own thread — a set_thread_state on another is dropped
    pack = {
        "rules_api_version": 1,
        "rules": [
            {
                "id": "overreach",
                "trigger": {"event": "ActorDied"},
                "then": [{"do": "set_thread_state", "thread": "t:other", "to": "active"}],
                "scope": {"thread": "t:feud"},  # jurisdiction is t:feud, NOT t:other
            }
        ],
    }
    world = await store.create_world(f"scope-{new_id()}", rule_pack=pack)
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [
            thread_created(thread_id="t:feud", stakes="a", state="active"),
            thread_created(thread_id="t:other", stakes="b", state="dormant"),
        ],
    )
    await _engine(store).react(
        campaign, await _head(store, campaign.branch_id), [actor_died(actor_id="a:x")]
    )
    assert (await _states(store, campaign.branch_id))["t:other"] == "dormant"  # untouched


async def test_record_rumor_is_forced_testimony(store: PostgresEventStore) -> None:
    pack = {
        "rules_api_version": 1,
        "rules": [
            {
                "id": "guild-gossips",
                "trigger": {"event": "ActorDied"},
                "then": [
                    {
                        "do": "record_rumor",
                        "text": "a death stirs the guild",
                        "subjects": ["a:smith"],
                    }
                ],
                "scope": {"faction": "f:guild"},
            }
        ],
    }
    world = await store.create_world(f"rumor-{new_id()}", rule_pack=pack)
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [
            faction_created(faction_id="f:guild", name="The Guild"),
            actor_created(actor_id="a:smith", name="Smith", tier=1),
            edge_added(src="a:smith", rel_type="member_of", dst="f:guild"),
        ],
    )
    await _engine(store).react(
        campaign, await _head(store, campaign.branch_id), [actor_died(actor_id="a:smith")]
    )
    claims = await store.claims_about(campaign.branch_id, "a:smith")
    rumor = next(c for c in claims if "stirs the guild" in c.statement)
    assert rumor.truth == "unknown" and rumor.origin == "module"  # never canon


async def test_gauntlet_is_deterministic_and_idempotent(store: PostgresEventStore) -> None:
    # same (fired actions, trigger commit) → byte-identical events with a stable, keyed claim id
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActRecordRumor, Scope

    world = await store.create_world(f"det-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [
            faction_created(faction_id="f:g", name="G"),
            actor_created(actor_id="a:m", name="M", tier=1),
            edge_added(src="a:m", rel_type="member_of", dst="f:g"),
        ],
    )
    fired = [
        FiredAction(
            rule_id="r1",
            scope=Scope(faction="f:g"),
            action=ActRecordRumor(do="record_rumor", text="word spreads", subjects=["a:m"]),
            index=0,
        )
    ]
    a = await run_rules_gauntlet(store, campaign.branch_id, fired, trigger_commit="c9")
    b = await run_rules_gauntlet(store, campaign.branch_id, fired, trigger_commit="c9")
    assert [e.payload for e in a] == [e.payload for e in b]  # deterministic
    assert a[0].payload["claim_id"] == "m:c9:r1:0"  # keyed on trigger commit → idempotent upsert


# --- INC-4: the downtime/agenda tick (docs/17) ---


async def test_agenda_tick_fires_on_time_skip_and_replays(store: PostgresEventStore) -> None:
    # an agenda rule: every 30 in-fiction days, two rival houses drift to war (an edge the module
    # MAY touch — at_war_with, both ends in the faction scope). A 60-day skip crosses the boundary.
    pack = {
        "rules_api_version": 1,
        "agendas": [
            {
                "id": "houses-drift-to-war",
                "every_days": 30,
                "then": [{"do": "add_edge", "src": "f:red", "rel": "at_war_with", "dst": "f:blue"}],
                "scope": {"faction": "f:pact"},
            }
        ],
    }
    world = await store.create_world(f"agenda-{new_id()}", rule_pack=pack)
    branch = world.main_branch_id
    await store.append_beat(
        branch,
        [
            faction_created(faction_id="f:pact", name="The Pact"),
            faction_created(faction_id="f:red", name="Red"),
            faction_created(faction_id="f:blue", name="Blue"),
            edge_added(src="f:red", rel_type="member_of", dst="f:pact"),
            edge_added(src="f:blue", rel_type="member_of", dst="f:pact"),
        ],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    await engine.agenda_tick(branch, 60)  # crosses the 30-day cadence

    wars = [e for e in await store.list_edges(branch, "at_war_with") if e.src == "f:red"]
    assert any(e.dst == "f:blue" for e in wars)  # the agenda moved the world off-screen
    # the war edge survives a fork (rebuilt by replay)
    fork = await store.fork_branch(world.world_id, await _head(store, branch), "later")
    fork_wars = [
        e for e in await store.list_edges(fork.branch_id, "at_war_with") if e.src == "f:red"
    ]
    assert any(e.dst == "f:blue" for e in fork_wars)


async def test_agenda_does_not_fire_below_cadence(store: PostgresEventStore) -> None:
    pack = {
        "rules_api_version": 1,
        "agendas": [
            {
                "id": "slow-burn",
                "every_days": 365,
                "then": [{"do": "add_edge", "src": "f:a", "rel": "at_war_with", "dst": "f:b"}],
                "scope": {"faction": "f:p"},
            }
        ],
    }
    world = await store.create_world(f"agenda2-{new_id()}", rule_pack=pack)
    branch = world.main_branch_id
    await store.append_beat(
        branch,
        [
            faction_created(faction_id="f:p", name="P"),
            faction_created(faction_id="f:a", name="A"),
            faction_created(faction_id="f:b", name="B"),
            edge_added(src="f:a", rel_type="member_of", dst="f:p"),
            edge_added(src="f:b", rel_type="member_of", dst="f:p"),
        ],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    await engine.agenda_tick(branch, 30)  # 30 < 365 → no cadence boundary crossed
    assert not await store.list_edges(branch, "at_war_with")  # nothing fired


# --- INC-5 (phase-end review fixes): Chronicler-path reactions + runtime version pin ---


async def test_chronicler_death_fires_a_reaction(store: PostgresEventStore) -> None:
    # Phase-end review (high): combat is lethal=False, so an EXTERNAL (Chronicler) death is the only
    # runtime ActorDied — the war-story premise. report_outcome must run react() so a rule that
    # triggers on ActorDied fires (it was wired only into _finish, bypassing the Chronicler path).
    from uro_server.app import engine_deps

    world = await store.create_world(f"chron-{new_id()}", rule_pack=_FEUD_PACK)
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [
            thread_created(thread_id="t:feud", stakes="the feud", state="dormant"),
            actor_created(actor_id="a:mook", name="Mook", tier=1),
            actor_created(actor_id="a:killer", name="Killer", tier=1),
        ],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    deps = engine_deps(store, engine, {"tok": "chronicler"})
    assert deps.report_outcome is not None
    # an external bundle: an unprotected T1 combatant falls → distill emits ActorDied
    await deps.report_outcome(
        campaign.campaign_id,
        {
            "encounter_id": "e:1",
            "participants": ["a:mook", "a:killer"],
            "casualties": ["a:mook"],
        },
    )
    assert (await _states(store, campaign.branch_id))["t:feud"] == "active"  # reaction fired


async def test_runtime_rejects_an_unsupported_rules_api_version(store: PostgresEventStore) -> None:
    # The version pin is on the MODEL, so RulePack() rejects a bad version at construction; and
    # (gap-report Hollowloop G-6 fix) create_world validates the pack LOUDLY — a future-version pack
    # fails at world creation, not silently at the first beat.
    from pydantic import ValidationError
    from uro_core.worldpack.rules import RULES_API_VERSION, RulePack

    with pytest.raises(ValueError, match="unsupported"):
        RulePack(rules_api_version=RULES_API_VERSION + 1)
    bad_pack = {**_FEUD_PACK, "rules_api_version": RULES_API_VERSION + 1}
    with pytest.raises((ValidationError, ValueError)):
        await store.create_world(f"ver-{new_id()}", rule_pack=bad_pack)


async def test_module_activated_thread_reaches_the_narrator(store: PostgresEventStore) -> None:
    # The loop that motivated wiring thread-recall: a Reaction-Layer rule activates a dormant
    # thread, and that now-live plot surfaces in the NEXT beat's narrator context (was invisible —
    # recall gathered actors/claims/beliefs but not threads). docs/17 + docs/04.
    from uro_core.pipeline.recall import assemble_recall, build_narrator_messages

    _, campaign = await _campaign_with_rule(store)  # feud dormant + death-wakes-the-feud rule
    branch = campaign.branch_id
    died = [actor_died(actor_id="a:mook")]
    await store.append_beat(branch, died)
    await _engine(store).react(campaign, await _head(store, branch), died)  # feud → active

    recall = await assemble_recall(store, branch, "what now?", 8)
    assert any(t.thread_id == "t:feud" for t in recall.active_threads)  # the woken plot is live
    blob = "\n".join(m.content for m in build_narrator_messages(recall, "what now?"))
    assert "ACTIVE THREADS" in blob and "the miners' feud" in blob  # and it reaches the prose


# --- gap-report fixes: accepted-but-inert trigger validation (3-game) + loud pack death (G-6) ---


def test_trigger_on_unknown_event_type_is_rejected() -> None:
    from uro_core.worldpack.rules import RulePack

    with pytest.raises(ValueError, match="not a known event type"):
        RulePack(
            rules_api_version=1,
            rules=[
                {
                    "id": "inert",
                    "trigger": {"event": "CheckResolved"},  # no such event → would never fire
                    "then": [{"do": "set_thread_state", "thread": "t:x", "to": "active"}],
                    "scope": {"thread": "t:x"},
                }
            ],
        )


def test_trigger_where_key_not_a_payload_field_is_rejected() -> None:
    from uro_core.worldpack.rules import RulePack

    with pytest.raises(ValueError, match="could never match"):
        RulePack(
            rules_api_version=1,
            rules=[
                {
                    "id": "inert2",
                    # actor.member_of is not a field of ActorDied's payload → silently never matched
                    "trigger": {"event": "ActorDied", "where": {"actor.member_of": "f:x"}},
                    "then": [{"do": "set_thread_state", "thread": "t:x", "to": "active"}],
                    "scope": {"thread": "t:x"},
                }
            ],
        )


def test_valid_where_keys_still_pass() -> None:
    from uro_core.worldpack.rules import RulePack

    # real payload fields must pass (origin on ClaimRecorded, rel_type on EdgeAdded, actor_id on
    # ActorDied) — the live game packs use exactly these
    RulePack(
        rules_api_version=1,
        rules=[
            {
                "id": "ok",
                "trigger": {"event": "ClaimRecorded", "where": {"origin": "narrator"}},
                "then": [{"do": "record_rumor", "text": "x", "subjects": ["a:m"]}],
                "scope": {"faction": "f:g"},
            }
        ],
    )


async def test_create_world_rejects_a_bad_rule_pack_loudly(store: PostgresEventStore) -> None:
    # gap-report Hollowloop G-6: a malformed pack must fail at world creation, not silently disable
    # every reaction at the first beat.
    from pydantic import ValidationError

    bad = {
        "rules_api_version": 1,
        "rules": [
            {
                "id": "typo",
                "trigger": {"event": "Nonexistent"},
                "then": [{"do": "set_thread_state", "thread": "t:x", "to": "active"}],
                "scope": {"thread": "t:x"},
            }
        ],
    }
    with pytest.raises((ValidationError, ValueError)):
        await store.create_world(f"bad-{new_id()}", rule_pack=bad)


async def test_append_and_react_fires_rules_on_authored_events(store: PostgresEventStore) -> None:
    # gap-report B1 (all 4 games): store.append_beat commits but runs NO rules; append_and_react is
    # the one-call path that both commits and reacts. Here an AUTHORED death wakes the feud —
    # whereas a bare append_beat would leave it dormant (the silent-dead-rules footgun).
    _, campaign = await _campaign_with_rule(store)
    branch = campaign.branch_id

    # bare append_beat: commits the death but the pack rule does NOT run
    await store.append_beat(branch, [actor_died(actor_id="a:extra1")])
    assert (await _states(store, branch))["t:feud"] == "dormant"  # rules never ran

    # append_and_react: commits AND fires the rule → the feud wakes
    commit = await _engine(store).append_and_react(campaign, [actor_died(actor_id="a:extra2")])
    assert commit.commit_id
    assert (await _states(store, branch))["t:feud"] == "active"


# --- INC-C1: the Computation Layer — engine-owned integer counters (docs/19, D-34) ---

_COUNTER_PACK = {
    "rules_api_version": 2,
    "rules": [
        {  # each death bumps the houses' tension counter
            "id": "bump-tension",
            "trigger": {"event": "ActorDied"},
            "then": [
                {"do": "adjust_counter", "scope_ref": "f:houses", "key": "tension", "delta": 1}
            ],
            "scope": {"faction": "f:houses"},
        },
        {  # once tension reaches 2, the feud goes to war (threshold read — one-beat lag, expected)
            "id": "boil-to-war",
            "trigger": {"event": "ActorDied"},
            "when": {
                "kind": "counter",
                "scope_ref": "f:houses",
                "key": "tension",
                "op": ">=",
                "value": 2,
            },
            "then": [{"do": "set_thread_state", "thread": "t:feud", "to": "active"}],
            "scope": {"thread": "t:feud"},
        },
    ],
}


async def _counter_campaign(store: PostgresEventStore, pack=_COUNTER_PACK):  # type: ignore[no-untyped-def]
    world = await store.create_world(f"counter-{new_id()}", rule_pack=pack)
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [
            faction_created(faction_id="f:houses", name="The Houses"),
            thread_created(thread_id="t:feud", stakes="the houses' feud", state="dormant"),
        ],
    )
    return world, campaign


async def _kill(store, campaign, actor="a:mook"):  # type: ignore[no-untyped-def]
    died = [actor_died(actor_id=actor)]
    await store.append_beat(campaign.branch_id, died)
    await _engine(store).react(campaign, await _head(store, campaign.branch_id), died)


async def test_counter_accumulates_and_threshold_fires(store: PostgresEventStore) -> None:
    _, c = await _counter_campaign(store)
    branch = c.branch_id
    await _kill(store, c)  # tension 0->1
    assert await store.get_counter(branch, "f:houses", "tension") == 1
    await _kill(store, c)  # tension 1->2 (boil reads 1 at start → not yet)
    assert await store.get_counter(branch, "f:houses", "tension") == 2
    assert (await _states(store, branch))["t:feud"] == "dormant"  # one-beat lag (docs/19)
    await _kill(store, c)  # boil reads tension=2 → war; bump → 3
    assert (await _states(store, branch))["t:feud"] == "active"
    assert await store.get_counter(branch, "f:houses", "tension") == 3


async def test_counter_survives_a_fork(store: PostgresEventStore) -> None:
    # THE load-bearing acceptance: engine-owned numeric state rides fork_branch (not shadow state)
    world, c = await _counter_campaign(store)
    await _kill(store, c)
    await _kill(store, c)
    fork = await store.fork_branch(world.world_id, await _head(store, c.branch_id), "sib")
    assert (
        await store.get_counter(fork.branch_id, "f:houses", "tension") == 2
    )  # carried by the fork


async def test_in_pass_accumulation_two_adjusts_both_count(store: PostgresEventStore) -> None:
    # critic finding: two adjusts to one key in one pass must BOTH count (read-your-writes), not
    # collide-and-drop-one under the absolute-baked model.
    pack = {
        "rules_api_version": 2,
        "rules": [
            {
                "id": "double",
                "trigger": {"event": "ActorDied"},
                "then": [
                    {"do": "adjust_counter", "scope_ref": "f:houses", "key": "x", "delta": 1},
                    {"do": "adjust_counter", "scope_ref": "f:houses", "key": "x", "delta": 1},
                ],
                "scope": {"faction": "f:houses"},
            }
        ],
    }
    _, c = await _counter_campaign(store, pack)
    await _kill(store, c)
    assert await store.get_counter(c.branch_id, "f:houses", "x") == 2  # both, not 1


async def test_counter_write_out_of_scope_is_dropped(store: PostgresEventStore) -> None:
    pack = {
        "rules_api_version": 2,
        "rules": [
            {
                "id": "overreach",
                "trigger": {"event": "ActorDied"},
                # scoped to t:feud but writes a faction counter → out of jurisdiction → dropped
                "then": [{"do": "adjust_counter", "scope_ref": "f:houses", "key": "y", "delta": 5}],
                "scope": {"thread": "t:feud"},
            }
        ],
    }
    _, c = await _counter_campaign(store, pack)
    await _kill(store, c)
    assert await store.get_counter(c.branch_id, "f:houses", "y") == 0  # never written


async def test_v1_packs_still_valid_under_v2_engine() -> None:
    from uro_core.worldpack.rules import RulePack

    RulePack(**_FEUD_PACK)  # declares version 1 — must still validate (additive grammar)
