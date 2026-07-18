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
from pydantic import ValidationError
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    actor_created,
    actor_died,
    claim_recorded,
    counter_changed,
    edge_added,
    faction_created,
    module_cause,
    thread_created,
)
from uro_core.domain.ids import new_id
from uro_core.errors import PackError
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.worldpack.parse import parse_pack
from uro_core.worldpack.rules import RulePack

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
    assert [e.payload for e in a.events] == [e.payload for e in b.events]  # deterministic
    assert a.events[0].payload["claim_id"] == "m:c9:r1:0"  # keyed on trigger → idempotent upsert


# --- B11 / D-40: multi-ref scopes + the dropped-action audit trail ---


def test_scope_validator_enforces_exactly_one_jurisdiction() -> None:
    import pytest
    from uro_core.worldpack.rules import Scope

    Scope(world=True)  # ok
    Scope(faction="f:a")  # ok (singular)
    Scope(factions=["f:a", "f:b"])  # ok (multi-ref, D-40)
    Scope(faction="f:a", factions=["f:b"])  # ok — same category merges
    with pytest.raises(ValueError):
        Scope()  # empty → would drop every action
    with pytest.raises(ValueError):
        Scope(faction="f:a", place="p:x")  # two categories
    with pytest.raises(ValueError):
        Scope(world=True, faction="f:a")  # `world` is exclusive


async def test_multi_ref_faction_scope_unions_members(store: PostgresEventStore) -> None:
    # A rule scoped to TWO factions may touch members of EITHER (a pact between them) without the
    # blunt `world` scope; a ref in neither is dropped with an audit record (D-40).
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActAddEdge, Scope

    world = await store.create_world(f"multi-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [
            faction_created(faction_id="f:a", name="A"),
            faction_created(faction_id="f:b", name="B"),
            actor_created(actor_id="a:ma", name="Ma", tier=1),
            actor_created(actor_id="a:mb", name="Mb", tier=1),
            actor_created(actor_id="a:x", name="X", tier=1),  # member of neither
            edge_added(src="a:ma", rel_type="member_of", dst="f:a"),
            edge_added(src="a:mb", rel_type="member_of", dst="f:b"),
        ],
    )
    scope = Scope(factions=["f:a", "f:b"])
    fired = [
        FiredAction(  # both ends in the UNIONED jurisdiction → committed
            rule_id="r1",
            scope=scope,
            index=0,
            action=ActAddEdge(do="add_edge", src="a:ma", rel="allied_with", dst="a:mb"),
        ),
        FiredAction(  # a:x is in neither faction → dropped, audited
            rule_id="r2",
            scope=scope,
            index=0,
            action=ActAddEdge(do="add_edge", src="a:ma", rel="allied_with", dst="a:x"),
        ),
    ]
    result = await run_rules_gauntlet(store, campaign.branch_id, fired, trigger_commit="c1")
    assert len(result.events) == 1  # only the in-scope edge
    assert [(d.rule_id, d.ref, d.reason) for d in result.drops] == [
        ("r2", "a:x", "edge endpoint out of scope")
    ]


async def test_dropped_action_audit_names_the_reason(store: PostgresEventStore) -> None:
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActCreateThread, ActSetThreadState, Scope

    world = await store.create_world(f"drop-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(campaign.branch_id, [faction_created(faction_id="f:g", name="G")])
    fired = [
        FiredAction(  # create_thread whose ref is OUTSIDE the faction jurisdiction
            rule_id="r1",
            scope=Scope(faction="f:g"),
            index=0,
            action=ActCreateThread(do="create_thread", thread="t:outside", stakes="x"),
        ),
        FiredAction(  # set_thread_state on a NONEXISTENT thread (in scope, but not there)
            rule_id="r2",
            scope=Scope(thread="t:ghost"),
            index=0,
            action=ActSetThreadState(do="set_thread_state", thread="t:ghost", to="active"),
        ),
    ]
    result = await run_rules_gauntlet(store, campaign.branch_id, fired, trigger_commit="c1")
    assert result.events == []  # both refused
    assert {(d.rule_id, d.reason) for d in result.drops} == {
        ("r1", "out of scope"),
        ("r2", "thread does not exist"),
    }


async def test_over_cap_actions_are_audited_not_silently_truncated(
    store: PostgresEventStore,
) -> None:
    # Review fix: the _MAX_ACTIONS DoS cap truncates the tail; that tail must be AUDITED (a drop
    # record), not vanish silently — B11's whole point (an author can't otherwise tell why).
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import _MAX_ACTIONS, run_rules_gauntlet
    from uro_core.worldpack.rules import ActRecordRumor, Scope

    world = await store.create_world(f"cap-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    fired = [
        FiredAction(
            rule_id=f"r{i}",
            scope=Scope(world=True),
            index=i,
            action=ActRecordRumor(do="record_rumor", text=f"rumor {i}", subjects=[]),
        )
        for i in range(_MAX_ACTIONS + 3)
    ]
    result = await run_rules_gauntlet(store, campaign.branch_id, fired, trigger_commit="c1")
    assert len(result.events) == _MAX_ACTIONS  # only the cap's worth committed
    assert any(d.rule_id == "*" and "cap" in d.reason for d in result.drops)  # …the tail is audited


async def test_partial_out_of_scope_subjects_are_audited(store: PostgresEventStore) -> None:
    # Review fix: a rumor with SOME out-of-scope subjects still commits (with the in-scope ones);
    # the filtered subject is recorded — a partial filter is no longer silent.
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActRecordRumor, Scope

    world = await store.create_world(f"partial-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [
            faction_created(faction_id="f:g", name="G"),
            actor_created(actor_id="a:in", name="In", tier=1),
            edge_added(src="a:in", rel_type="member_of", dst="f:g"),
        ],
    )
    fired = [
        FiredAction(
            rule_id="r1",
            scope=Scope(faction="f:g"),
            index=0,
            action=ActRecordRumor(do="record_rumor", text="word", subjects=["a:in", "a:out"]),
        )
    ]
    result = await run_rules_gauntlet(store, campaign.branch_id, fired, trigger_commit="c1")
    assert len(result.events) == 1  # the rumor still commits (with the in-scope subject)
    assert any(d.ref == "a:out" and "partial" in d.reason for d in result.drops)


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


# --- INC-C2: world scope + cross-entity counter_compare + count_edges (docs/19, RL-3/RL-5) ---


async def test_world_scope_and_counter_compare_predator(store: PostgresEventStore) -> None:
    # RL-3: a realm-wide rule declares war when one house outstrengths another (cross-entity compare
    # gating a cross-entity edge — needs `world` scope (single-dimension scope can't span both).
    pack = {
        "rules_api_version": 2,
        "rules": [
            {
                "id": "predator-smells-weakness",
                "trigger": {"event": "ActorDied"},
                "when": {
                    "kind": "counter_compare",
                    "left": {"scope_ref": "f:red", "key": "strength"},
                    "right": {"scope_ref": "f:blue", "key": "strength"},
                    "op": ">",
                    "left_mul": 5,  # strength(red) * 5 > strength(blue) * 6  ==  red > blue * 1.2
                    "right_mul": 6,
                },
                "then": [{"do": "add_edge", "src": "f:red", "rel": "at_war_with", "dst": "f:blue"}],
                "scope": {"world": True},  # cross-entity edge → whole-realm jurisdiction
            }
        ],
    }
    world = await store.create_world(f"c2-{new_id()}", rule_pack=pack)
    c = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        c.branch_id,
        [
            faction_created(faction_id="f:red", name="Red"),
            faction_created(faction_id="f:blue", name="Blue"),
        ],
    )
    # seed strengths directly (also proves cross-entity counter state)
    setpack_beat = [actor_died(actor_id="a:x")]
    # red=13, blue=10 → 13*5=65 > 10*6=60 → war
    await store.append_beat(
        c.branch_id,
        [
            counter_changed(
                scope_ref="f:red", key="strength", to_value=13, caused_by=module_cause("seed")
            ),
            counter_changed(
                scope_ref="f:blue", key="strength", to_value=10, caused_by=module_cause("seed")
            ),
        ],
    )
    await store.append_beat(c.branch_id, setpack_beat)
    await _engine(store).react(c, await _head(store, c.branch_id), setpack_beat)
    wars = [e for e in await store.list_edges(c.branch_id, "at_war_with") if e.src == "f:red"]
    assert any(e.dst == "f:blue" for e in wars)  # red out-strengthed blue → war declared


async def test_count_edges_fall_of_house(store: PostgresEventStore) -> None:
    # RL-5: when a house holds zero territories (count owns == 0), its decline thread resolves.
    pack = {
        "rules_api_version": 2,
        "rules": [
            {
                "id": "fall-of-house",
                "trigger": {"event": "ActorDied"},
                "when": {
                    "kind": "count_edges",
                    "src": "f:dell",
                    "rel": "owns",
                    "op": "==",
                    "value": 0,
                },
                "then": [{"do": "set_thread_state", "thread": "t:dell-decline", "to": "resolved"}],
                "scope": {"thread": "t:dell-decline"},
            }
        ],
    }
    world = await store.create_world(f"c2b-{new_id()}", rule_pack=pack)
    c = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        c.branch_id,
        [
            faction_created(faction_id="f:dell", name="Dellmoor"),
            thread_created(thread_id="t:dell-decline", stakes="Dellmoor fades", state="active"),
        ],
    )
    # Dellmoor owns nothing → count owns == 0 → the rule fires
    died = [actor_died(actor_id="a:y")]
    await store.append_beat(c.branch_id, died)
    await _engine(store).react(c, await _head(store, c.branch_id), died)
    assert (await _states(store, c.branch_id))["t:dell-decline"] == "resolved"


async def test_narrow_scope_still_fences_cross_entity_writes(store: PostgresEventStore) -> None:
    # world scope is opt-in: a faction-scoped rule still cannot touch another faction (no regress)
    pack = {
        "rules_api_version": 2,
        "rules": [
            {
                "id": "overreach",
                "trigger": {"event": "ActorDied"},
                "then": [{"do": "add_edge", "src": "f:red", "rel": "at_war_with", "dst": "f:blue"}],
                "scope": {"faction": "f:red"},  # f:blue is NOT in this jurisdiction → dropped
            }
        ],
    }
    world = await store.create_world(f"c2c-{new_id()}", rule_pack=pack)
    c = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        c.branch_id,
        [
            faction_created(faction_id="f:red", name="Red"),
            faction_created(faction_id="f:blue", name="Blue"),
        ],
    )
    died = [actor_died(actor_id="a:z")]
    await store.append_beat(c.branch_id, died)
    await _engine(store).react(c, await _head(store, c.branch_id), died)
    assert not await store.list_edges(c.branch_id, "at_war_with")  # blue out of scope → dropped


async def test_concurrent_react_passes_do_not_lose_a_counter_increment(
    store: PostgresEventStore,
) -> None:
    # Computation-tier review: adjust_counter is a read-modify-write; two react() passes on one
    # branch (the un-arbitered Chronicler POST path / a multi-connection participant) must not
    # interleave and lose an increment. The per-branch _react_lock serializes them in-process.
    import asyncio as _asyncio

    pack = {
        "rules_api_version": 2,
        "rules": [
            {
                "id": "bump",
                "trigger": {"event": "ActorDied"},
                "then": [{"do": "adjust_counter", "scope_ref": "f:h", "key": "n", "delta": 1}],
                "scope": {"faction": "f:h"},
            }
        ],
    }
    world = await store.create_world(f"race-{new_id()}", rule_pack=pack)
    c = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(c.branch_id, [faction_created(faction_id="f:h", name="H")])
    engine = _engine(store)
    # two reacts fired concurrently on the SAME branch, each with its own ActorDied trigger
    died = [actor_died(actor_id="a:x")]
    head = await _head(store, c.branch_id)
    await _asyncio.gather(
        engine.react(c, head, died),
        engine.react(c, head, died),
    )
    assert await store.get_counter(c.branch_id, "f:h", "n") == 2  # both counted, none lost


# --- C3 / C4 / C5 (D-34): for_each + roll_table + expire_claims (docs/19 staged) ---


async def test_expire_claims_retracts_old_module_rumors_never_canon(
    store: PostgresEventStore,
) -> None:
    # C5 (RL-8): expire_claims retracts a STALE module rumor (truth→false) but STRUCTURALLY never a
    # canon claim (truth=true / origin=narration), and never one that isn't old enough yet.
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActExpireClaims, Scope

    world = await store.create_world(f"expire-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    branch = campaign.branch_id
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:m", name="M", tier=1),
            claim_recorded(
                claim_id="c:old",
                statement="a stale rumor",
                subject_refs=["a:m"],
                truth="unknown",
                origin="module",
                created_day=0,
            ),
            claim_recorded(  # canon about the SAME subject — must be untouchable
                claim_id="c:canon",
                statement="the truth",
                subject_refs=["a:m"],
                truth="true",
                origin="narration",
                created_day=0,
            ),
            claim_recorded(  # a module rumor, but not old enough at world_day 100
                claim_id="c:fresh",
                statement="fresh gossip",
                subject_refs=["a:m"],
                truth="unknown",
                origin="module",
                created_day=90,
            ),
        ],
    )
    await store.time_skip(branch, 100)  # world_day → 100; cutoff = 100 - 60 = 40
    fired = [
        FiredAction(
            rule_id="r1",
            scope=Scope(world=True),
            index=0,
            action=ActExpireClaims(do="expire_claims", older_than_days=60),
        )
    ]
    result = await run_rules_gauntlet(store, branch, fired, trigger_commit="c1")
    await store.append_beat(branch, result.events)
    assert (await store.get_claim(branch, "c:old")).truth == "false"  # stale module rumor retracted
    assert (await store.get_claim(branch, "c:canon")).truth == "true"  # canon UNTOUCHED
    assert (await store.get_claim(branch, "c:fresh")).truth == "unknown"  # not old enough → kept


def test_roll_table_weights_must_match_outcomes() -> None:
    import pytest
    from uro_core.worldpack.rules import ActRollTable

    with pytest.raises(ValueError):
        ActRollTable(do="roll_table", weights={"A": 1}, outcomes={"B": []})  # key mismatch
    with pytest.raises(ValueError):
        ActRollTable(do="roll_table", weights={"A": 0}, outcomes={"A": []})  # non-positive weight
    with pytest.raises(ValueError):
        ActRollTable(do="roll_table", weights={}, outcomes={})  # empty → ZeroDivision (review fix)


async def test_roll_table_picks_deterministically_and_applies_one_outcome(
    store: PostgresEventStore,
) -> None:
    # C4 (RL-4): a seeded weighted pick applies exactly ONE outcome; BAKED → replay-identical.
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActRollTable, ActSetThreadState, Scope

    world = await store.create_world(f"roll-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    branch = campaign.branch_id
    await store.append_beat(
        branch,
        [
            thread_created(thread_id="t:a", stakes="A", state="dormant"),
            thread_created(thread_id="t:b", stakes="B", state="dormant"),
        ],
    )
    action = ActRollTable(
        do="roll_table",
        weights={"A": 1, "B": 1},
        outcomes={
            "A": [ActSetThreadState(do="set_thread_state", thread="t:a", to="active")],
            "B": [ActSetThreadState(do="set_thread_state", thread="t:b", to="active")],
        },
    )
    fired = [FiredAction(rule_id="r1", scope=Scope(world=True), index=0, action=action)]
    r1 = await run_rules_gauntlet(store, branch, fired, trigger_commit="c1")
    r2 = await run_rules_gauntlet(store, branch, fired, trigger_commit="c1")
    assert [e.payload for e in r1.events] == [e.payload for e in r2.events]  # deterministic (baked)
    assert len(r1.events) == 1 and r1.events[0].event_type == "ThreadStateChanged"  # one outcome


async def test_for_each_drags_allies_into_war_scope_fenced(store: PostgresEventStore) -> None:
    # C3 (RL-11): for_each traverses allied_with from $trigger.src, binding each ally, and applies
    # add_edge(ally, at_war_with, $trigger.dst). A neighbor out of scope is dropped + audited.
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActForEach, Scope

    world = await store.create_world(f"foreach-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    branch = campaign.branch_id
    await store.append_beat(
        branch,
        [
            faction_created(faction_id="f:a", name="A"),
            faction_created(faction_id="f:b", name="B"),
            faction_created(faction_id="f:ally1", name="Ally1"),
            faction_created(faction_id="f:ally2", name="Ally2"),
            edge_added(src="f:a", rel_type="allied_with", dst="f:ally1"),
            edge_added(src="f:a", rel_type="allied_with", dst="f:ally2"),
        ],
    )
    action = ActForEach.model_validate(
        {
            "do": "for_each",
            "traverse": "allied_with",
            "from": "$trigger.src",
            "as": "ALLY",
            "apply": [
                {"do": "add_edge", "src": "ALLY", "rel": "at_war_with", "dst": "$trigger.dst"}
            ],
        }
    )
    fired = [
        FiredAction(
            rule_id="r1",
            scope=Scope(world=True),
            index=0,
            action=action,
            trigger_payload={"src": "f:a", "dst": "f:b", "rel_type": "at_war_with"},
        )
    ]
    result = await run_rules_gauntlet(store, branch, fired, trigger_commit="c1")
    await store.append_beat(branch, result.events)
    for ally in ("f:ally1", "f:ally2"):  # both allies dragged into war with f:b (the $trigger.dst)
        edges = await store.edges_from(branch, ally)
        assert any(e.rel_type == "at_war_with" and e.dst == "f:b" for e in edges)


async def test_for_each_drops_out_of_scope_neighbors(store: PostgresEventStore) -> None:
    # C3: under a narrow faction scope, an ally reached by traversal that is NOT in the jurisdiction
    # is DROPPED + audited (the for_each body can't reach out of scope via a bound neighbor).
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActForEach, Scope

    world = await store.create_world(f"foreach2-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    branch = campaign.branch_id
    await store.append_beat(
        branch,
        [
            faction_created(faction_id="f:a", name="A"),
            faction_created(faction_id="f:out", name="Outsider"),
            edge_added(src="f:a", rel_type="allied_with", dst="f:out"),  # ally, NOT a member
        ],
    )
    action = ActForEach.model_validate(
        {
            "do": "for_each",
            "traverse": "allied_with",
            "from": "f:a",
            "as": "ALLY",
            "apply": [{"do": "add_edge", "src": "ALLY", "rel": "at_war_with", "dst": "f:a"}],
        }
    )
    # scope = faction f:a → allowed is {f:a} + its members; f:out (an ally, not a member) is out
    fired = [FiredAction(rule_id="r1", scope=Scope(faction="f:a"), index=0, action=action)]
    result = await run_rules_gauntlet(store, branch, fired, trigger_commit="c1")
    assert result.events == []  # the out-of-scope neighbor produced nothing
    assert any(d.ref == "f:out" and "out of scope" in d.reason for d in result.drops)  # audited


# --- D-34 review fixes (C3/C4/C5 phase-end review) ---


def test_created_day_is_in_snapshot_tables() -> None:
    # HIGH review fix: else a snapshot-based fork/materialize zeroes rumor age and expire mis-fires.
    from uro_core.adapters.postgres.projector import _SNAPSHOT_TABLES

    assert "created_day" in _SNAPSHOT_TABLES["claims"]


async def test_claim_created_day_survives_a_snapshot_fork(store: PostgresEventStore) -> None:
    # HIGH review fix (C5 x P2): a rumor's created_day must survive a SNAPSHOT-based fork intact.
    store._snapshot_every = 3  # force snapshots so the fork materializes from one
    world = await store.create_world(f"cday-{new_id()}")
    b = world.main_branch_id
    await store.append_beat(
        b,
        [
            claim_recorded(
                claim_id="c:r",
                statement="a rumor",
                subject_refs=[],
                truth="unknown",
                origin="module",
                created_day=5,
            )
        ],
    )
    for i in range(6):  # pad past a snapshot boundary (depths 2..7 → snapshots at 3, 6)
        await store.append_beat(b, [actor_created(actor_id=f"a:{i}", name=f"A{i}")])
    fork = await store.fork_branch(world.world_id, await _head(store, b), "fork")
    forked = await store.get_claim(fork.branch_id, "c:r")
    assert forked is not None and forked.created_day == 5  # NOT zeroed by the snapshot restore


def test_recursive_action_lists_are_capped_at_parse() -> None:
    import pytest
    from uro_core.worldpack.rules import ActForEach, ActRollTable

    leaf = {"do": "set_thread_state", "thread": "t:x", "to": "active"}
    with pytest.raises(ValueError):  # for_each apply cap (DoS review fix)
        ActForEach.model_validate(
            {"do": "for_each", "traverse": "knows", "from": "f:a", "as": "X", "apply": [leaf] * 17}
        )
    with pytest.raises(ValueError):  # roll_table outcome cap
        ActRollTable.model_validate(
            {"do": "roll_table", "weights": {"a": 1}, "outcomes": {"a": [leaf] * 17}}
        )


async def test_expire_claims_subjectless_rumor_needs_world_scope(store: PostgresEventStore) -> None:
    # LOW review fix: a SUBJECT-LESS module rumor has no scope anchor — a narrow (faction) rule must
    # NOT retract it; only a `world` rule may.
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActExpireClaims, Scope

    world = await store.create_world(f"subless-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    b = campaign.branch_id
    await store.append_beat(
        b,
        [
            faction_created(faction_id="f:court", name="Court"),
            claim_recorded(
                claim_id="c:sub",
                statement="a subjectless rumor",
                subject_refs=[],
                truth="unknown",
                origin="module",
                created_day=0,
            ),
        ],
    )
    await store.time_skip(b, 100)
    action = ActExpireClaims(do="expire_claims", older_than_days=60)
    faction = [FiredAction(rule_id="r1", scope=Scope(faction="f:court"), index=0, action=action)]
    faction_r = await run_rules_gauntlet(store, b, faction, trigger_commit="c1")
    assert faction_r.events == []  # a faction scope can't reach a subject-less rumor
    world_scope = [FiredAction(rule_id="r2", scope=Scope(world=True), index=0, action=action)]
    world_r = await run_rules_gauntlet(store, b, world_scope, trigger_commit="c1")
    assert len(world_r.events) == 1  # only world scope may


async def test_for_each_bind_var_named_like_a_verb_does_not_corrupt_do(
    store: PostgresEventStore,
) -> None:
    # LOW review fix (the do-skip): even a pathological loop var NAMED like an action verb must not
    # corrupt the `do` discriminator during substitution — the action still applies correctly.
    from uro_core.engines.rules import FiredAction
    from uro_core.engines.rules_gauntlet import run_rules_gauntlet
    from uro_core.worldpack.rules import ActForEach, Scope

    world = await store.create_world(f"bindname-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    b = campaign.branch_id
    await store.append_beat(
        b,
        [
            faction_created(faction_id="f:a", name="A"),
            faction_created(faction_id="f:n", name="N"),
            edge_added(src="f:a", rel_type="allied_with", dst="f:n"),
        ],
    )
    # `as: add_edge` — the bind token collides with an action verb; src="add_edge" is the loop var.
    # The do-skip keeps each inner `do` intact, so it still produces a valid add_edge.
    action = ActForEach.model_validate(
        {
            "do": "for_each",
            "traverse": "allied_with",
            "from": "f:a",
            "as": "add_edge",
            "apply": [{"do": "add_edge", "src": "add_edge", "rel": "knows", "dst": "f:a"}],
        }
    )
    result = await run_rules_gauntlet(
        store,
        b,
        [FiredAction(rule_id="r1", scope=Scope(world=True), index=0, action=action)],
        trigger_commit="c1",
    )
    assert len(result.events) == 1  # neighbor (f:n) → knows → f:a; `do` was never corrupted
    assert result.events[0].payload["src"] == "f:n"


# --- RL-6 (#25): $trigger.<field>-aware `when` + trigger.per_event -------------------------------


def _rl6_pack(when: dict, then: list[dict], *, per_event: bool = False) -> dict:  # type: ignore[type-arg]
    """A v5 pack: an ActorDied trigger whose `when` binds the dead actor via $trigger.actor_id."""
    trigger: dict = {"event": "ActorDied"}  # type: ignore[type-arg]
    if per_event:
        trigger["per_event"] = True
    return {
        "rules_api_version": 5,
        "rules": [
            {
                "id": "red-band-vendetta",
                "trigger": trigger,
                "when": when,
                "then": then,
                "scope": {"faction": "f:red-band"},
            }
        ],
    }


async def _rl6_campaign(store: PostgresEventStore, pack: dict):  # type: ignore[no-untyped-def, type-arg]
    """A world with a Red Band faction, one MEMBER, one OUTSIDER, and a dormant war thread."""
    world = await store.create_world(f"rl6-{new_id()}", rule_pack=pack)
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await store.append_beat(
        campaign.branch_id,
        [
            faction_created(faction_id="f:red-band", name="Red Band", kind="faction"),
            actor_created(actor_id="a:member", name="Vorlund"),
            actor_created(actor_id="a:member2", name="Sela"),
            actor_created(actor_id="a:outsider", name="A Stranger"),
            edge_added(src="a:member", rel_type="member_of", dst="f:red-band"),
            edge_added(src="a:member2", rel_type="member_of", dst="f:red-band"),
            thread_created(thread_id="t:war", stakes="the Red Band war", state="dormant"),
        ],
    )
    return world, campaign


async def _module_claims(store: PostgresEventStore, branch: str) -> list[str]:
    return [c.statement for c in await store.list_claims(branch) if c.origin == "module"]


_MEMBER_WHEN = {
    "kind": "edge_exists",
    "src": "$trigger.actor_id",
    "rel": "member_of",
    "dst": "f:red-band",
}
_RUMOR = [
    {"do": "record_rumor", "text": "The Red Band counts its dead.", "subjects": ["f:red-band"]}
]


async def test_rl6_existential_fires_when_a_member_dies_even_if_not_first(
    store: PostgresEventStore,
) -> None:
    """The fatal case the design-check adversary found: a multi-death beat where a NON-member dies
    FIRST. Binding only the first event would miss the member; per-event `when` evaluation makes it
    a true existential — the rule fires because a Red Band member is among the dead."""
    _, campaign = await _rl6_campaign(store, _rl6_pack(_MEMBER_WHEN, _RUMOR))
    branch = campaign.branch_id
    died = [actor_died(actor_id="a:outsider"), actor_died(actor_id="a:member")]  # outsider FIRST
    await store.append_beat(branch, died)
    await _engine(store).react(campaign, await _head(store, branch), died)
    assert len(await _module_claims(store, branch)) == 1  # fired despite the outsider dying first


async def test_rl6_existential_default_fires_once_for_many_member_deaths(
    store: PostgresEventStore,
) -> None:
    """Default (per_event omitted) is an EXISTENTIAL: two members die in one beat → the rule fires
    ONCE (one rumor), bound to the first satisfying event."""
    _, campaign = await _rl6_campaign(store, _rl6_pack(_MEMBER_WHEN, _RUMOR))
    branch = campaign.branch_id
    died = [actor_died(actor_id="a:member"), actor_died(actor_id="a:member2")]
    await store.append_beat(branch, died)
    await _engine(store).react(campaign, await _head(store, branch), died)
    assert len(await _module_claims(store, branch)) == 1  # fired once, not per-death


async def test_rl6_no_fire_when_no_member_dies(store: PostgresEventStore) -> None:
    """Only an outsider dies → the $trigger-bound edge check is false for every matching event →
    no fire, no module commit."""
    _, campaign = await _rl6_campaign(store, _rl6_pack(_MEMBER_WHEN, _RUMOR))
    branch = campaign.branch_id
    head_before = await _head(store, branch)
    died = [actor_died(actor_id="a:outsider")]
    await store.append_beat(branch, died)
    await _engine(store).react(campaign, await _head(store, branch), died)
    assert await _module_claims(store, branch) == []
    # only the trigger beat committed; react added no module beat
    assert (await _states(store, branch))["t:war"] == "dormant"
    assert head_before != await _head(store, branch)  # the trigger beat did commit


async def test_rl6_per_event_fires_once_per_matching_death_and_rides_a_fork(
    store: PostgresEventStore,
) -> None:
    """per_event: true fires once PER matching death (the count-each shape). Two members + one
    outsider die → two rumors (not one, not three). The per-event emissions are event-sourced, so
    they REBUILD on a fork by replay (the whole point — a shadow game-code counter would not)."""
    pack = _rl6_pack(_MEMBER_WHEN, _RUMOR, per_event=True)
    world, campaign = await _rl6_campaign(store, pack)
    branch = campaign.branch_id
    died = [
        actor_died(actor_id="a:member"),
        actor_died(actor_id="a:outsider"),
        actor_died(actor_id="a:member2"),
    ]
    await store.append_beat(branch, died)
    await _engine(store).react(campaign, await _head(store, branch), died)
    assert len(await _module_claims(store, branch)) == 2  # one per member death, outsider skipped
    fork = await store.fork_branch(world.world_id, await _head(store, branch), "aftermath")
    assert len(await _module_claims(store, fork.branch_id)) == 2  # per-event emissions ride a fork


async def test_rl6_whole_when_fails_closed_on_an_unbound_trigger_ref(
    store: PostgresEventStore,
) -> None:
    """An unbound $trigger.<field> fails the WHOLE `when` closed (a sentinel raised through the
    tree), not just the leaf - so a `not`-wrapped unbound ref can't fail OPEN and fire."""
    from uro_core.engines.rules import _eval, _UnboundTrigger
    from uro_core.worldpack.rules import CondEdgeExists, CondNot

    inner = CondEdgeExists(kind="edge_exists", src="$trigger.actor_id", rel="member_of", dst="f:x")
    cond = CondNot(kind="not", cond=inner)
    with pytest.raises(_UnboundTrigger):  # field ABSENT: not(False)=True would fail OPEN; raises
        await _eval(store, "b", cond, 0, [100], {})
    with pytest.raises(_UnboundTrigger):  # field PRESENT-but-null (e.g. learned_from): also unbound
        await _eval(store, "b", cond, 0, [100], {"actor_id": None})


def test_rl6_parse_rejects_trigger_ref_in_a_literal_slot() -> None:
    """$trigger in a literal value slot (a counter key) is a loud parse error, not a silent
    type-confusion misfire (resolving an actor id into a counter key)."""
    pack = _rl6_pack(
        {
            "kind": "counter",
            "scope_ref": "f:red-band",
            "key": "$trigger.actor_id",
            "op": ">",
            "value": 0,
        },
        _RUMOR,
    )
    with pytest.raises(ValidationError, match="literal slot"):
        RulePack(**pack)


def test_rl6_parse_rejects_unknown_trigger_field() -> None:
    """$trigger.<field> in a ref slot must name a real field of the trigger event (ActorDied has
    actor_id/cause, not 'faction') — else it would validate but never bind."""
    pack = _rl6_pack(
        {"kind": "edge_exists", "src": "$trigger.faction", "rel": "member_of", "dst": "f:red-band"},
        _RUMOR,
    )
    with pytest.raises(ValidationError, match="not a field of ActorDied"):
        RulePack(**pack)


def test_rl6_parse_rejects_trigger_ref_in_an_agenda_rule() -> None:
    """An agenda rule has no trigger event, so a $trigger ref can never bind - parse rejects it."""
    pack = {
        "rules_api_version": 5,
        "agendas": [
            {
                "id": "a1",
                "every_days": 7,
                "when": {"kind": "actor_is_pc", "actor": "$trigger.actor_id"},
                "then": _RUMOR,
                "scope": {"faction": "f:red-band"},
            }
        ],
    }
    with pytest.raises(ValidationError, match="agenda"):
        RulePack(**pack)


def test_rl6_parse_rejects_trigger_ref_on_a_non_string_field() -> None:
    """A $trigger ref must name a STRING field. A list field (ClaimRecorded.subject_refs) would
    str()-ify and silently never match — rejected at parse, not accepted-but-inert."""
    pack = {
        "rules_api_version": 5,
        "rules": [
            {
                "id": "r",
                "trigger": {"event": "ClaimRecorded"},
                "when": {
                    "kind": "edge_exists",
                    "src": "$trigger.subject_refs",  # a list field, not a string ref
                    "rel": "member_of",
                    "dst": "f:x",
                },
                "then": _RUMOR,
                "scope": {"faction": "f:red-band"},
            }
        ],
    }
    with pytest.raises(ValidationError, match="string ref field"):
        RulePack(**pack)


async def test_rl6_trigger_free_when_evaluated_once_not_per_event(
    store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix: a $trigger-free `when` is constant across a beat's matching events, so it is
    evaluated ONCE, not once per event - else a when-False rule burns the SHARED node budget per
    event and starves later rules. With a tight budget + 5 deaths, the later when-True rule B still
    fires (pre-fix, A's per-event evaluation would exhaust the budget and drop the pass)."""
    from uro_core.engines import rules as rules_mod
    from uro_core.engines.rules import evaluate_rules

    monkeypatch.setattr(rules_mod, "_MAX_NODES", 3)
    pack = {
        "rules_api_version": 1,  # a legacy (no-$trigger) pack - the one the fix protects
        "rules": [
            {  # A: $trigger-free, when FALSE (t:x dormant, not active) - never fires, no break
                "id": "a-hog",
                "trigger": {"event": "ActorDied"},
                "when": {"kind": "thread_state", "thread": "t:x", "state": "active"},
                "then": [{"do": "set_thread_state", "thread": "t:x", "to": "active"}],
                "scope": {"thread": "t:x"},
            },
            {  # B: $trigger-free, when is TRUE → should fire (unless A starved the budget first)
                "id": "b-fires",
                "trigger": {"event": "ActorDied"},
                "when": {"kind": "thread_state", "thread": "t:y", "state": "dormant"},
                "then": [{"do": "set_thread_state", "thread": "t:y", "to": "active"}],
                "scope": {"thread": "t:y"},
            },
        ],
    }
    world = await store.create_world(f"rl6b-{new_id()}", rule_pack=pack)
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    branch = campaign.branch_id
    await store.append_beat(
        branch,
        [
            thread_created(thread_id="t:x", stakes="x", state="dormant"),
            thread_created(thread_id="t:y", stakes="y", state="dormant"),
        ],
    )
    rules = RulePack(**pack).rules
    events = [actor_died(actor_id=f"a:{i}") for i in range(5)]
    fired = await evaluate_rules(store, branch, rules=rules, trigger_events=events, world_day=0)
    assert any(f.rule_id == "b-fires" for f in fired)  # not starved by a-hog's 5 events
