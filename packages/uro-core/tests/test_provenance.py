"""D-89: provenance integrity for the two closed trust tiers. An event stamped `external` must be
one distillation can make, and one stamped `module` one the rules gauntlet can make — checked where
events are committed and imported. The sets come from the producers: every rule action kind and
both distillers are run here, so a new action that emits another event type fails this test
instead of silently dropping its module beat (react is exception-isolated)."""

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.authored import distill_authored_outcome
from uro_core.chronicler import Feat, LootTransfer, OutcomeBundle, distill_outcome
from uro_core.domain.events import (
    CausedBy,
    DomainEvent,
    actor_created,
    beat_resolved,
    claim_recorded,
    claim_truth_changed,
    edge_added,
    external_cause,
    item_created,
    module_cause,
    sheet_updated,
    thread_created,
)
from uro_core.domain.ids import new_id
from uro_core.domain.provenance import ProvenanceError, check_provenance
from uro_core.engines.rules import evaluate_rules
from uro_core.engines.rules_gauntlet import run_rules_gauntlet
from uro_core.errors import ExportError
from uro_core.export import stamp_chain
from uro_core.worldpack.rules import RulePack


def test_a_closed_tier_cause_its_producer_cant_make_is_refused() -> None:
    sheet = sheet_updated(actor_id="a:x", sheet={"hp": 1}, caused_by=external_cause("e:1"))
    refused = [
        sheet,  # Seventh Vault G-27: a host stamped its own sheet write `external`
        claim_recorded(
            claim_id="c:1", statement="x", truth="true", caused_by=external_cause("e:1")
        ),
        DomainEvent(
            event_type="ActorDied", caused_by=module_cause("r"), payload={"actor_id": "a:x"}
        ),
        claim_truth_changed(claim_id="c:1", truth="true", caused_by=module_cause("r")),
        thread_created(thread_id="t:1", stakes="x", caused_by=module_cause("r")),  # author
    ]
    for event in refused:
        with pytest.raises(ProvenanceError):
            check_provenance([event])
    # the open causes aren't the check's business
    check_provenance([sheet.model_copy(update={"caused_by": CausedBy(kind="system")})])


async def _world(store: PostgresEventStore) -> tuple[str, list[DomainEvent], str]:
    world = await store.create_world(f"prov-{new_id()}")
    branch = world.main_branch_id
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:x", name="Xan", tier=1),
            actor_created(actor_id="a:y", name="Yor", tier=1),
            actor_created(actor_id="a:king", name="The King", tier=3, role="ruler"),
            edge_added(src="a:x", rel_type="knows", dst="a:y"),
            thread_created(thread_id="t:old", stakes="an old plot", state="dormant"),
            claim_recorded(claim_id="c:known", statement="A wagon burned.", truth="unknown"),
            item_created(item_id="i:crown", name="crown", owner_ref="a:king"),
        ],
    )
    trigger = [beat_resolved(beat_id=new_id(), participant_id="p", intent_text="i", narration="n")]
    commit = await store.append_beat(branch, trigger)
    return branch, trigger, commit.commit_id


async def test_every_rule_action_kind_passes_the_check(store: PostgresEventStore) -> None:
    branch, trigger, commit_id = await _world(store)
    count = {"scope_ref": "w:x", "key": "k"}
    every = [
        {"do": "set_thread_state", "thread": "t:old", "to": "offered"},
        {"do": "set_counter", **count, "value": 2},
        {"do": "adjust_counter", **count, "delta": 1},
        {"do": "reset_counter", "scope_ref": "w:x", "key": "j"},
        {"do": "create_thread", "thread": "t:new", "stakes": "a new plot"},
        {"do": "record_rumor", "text": "The bells rang.", "subjects": ["a:x"]},
        {"do": "spread_belief", "claim": "c:known", "witnesses": ["a:x"]},
        {"do": "add_edge", "src": "a:x", "rel": "allied_with", "dst": "a:y"},
        {"do": "remove_edge", "src": "a:x", "rel": "knows", "dst": "a:y"},
        {
            "do": "roll_table",
            "weights": {"one": 1},
            "outcomes": {"one": [{"do": "adjust_counter", **count, "delta": 1}]},
        },
        {
            "do": "for_each",
            "traverse": "knows",
            "from": "a:x",
            "as": "$n",
            "apply": [{"do": "adjust_counter", "scope_ref": "$n", "key": "met", "delta": 1}],
        },
    ]
    pack = RulePack(
        rules_api_version=7,
        rules=[
            {
                "id": "every",
                "trigger": {"event": "BeatResolved"},
                "then": every,
                "scope": {"world": True},
            },
        ],
    )
    fired = await evaluate_rules(
        store, branch, rules=pack.rules, trigger_events=trigger, world_day=0
    )
    result = await run_rules_gauntlet(store, branch, fired, trigger_commit=commit_id)
    assert not result.drops, result.drops
    check_provenance(result.events)
    emitted = {e.event_type for e in result.events}
    assert emitted >= {
        "ThreadStateChanged",
        "CounterChanged",
        "ThreadCreated",
        "ClaimRecorded",
        "BeliefChanged",
        "EdgeAdded",
        "EdgeRemoved",
    }
    await store.append_beat(branch, result.events)  # and the store accepts them

    # expire_claims retracts the module rumor once it is old enough
    await store.time_skip(branch, 3)
    expire = RulePack(
        rules_api_version=7,
        rules=[
            {
                "id": "expire",
                "trigger": {"event": "BeatResolved"},
                "then": [{"do": "expire_claims", "older_than_days": 1}],
                "scope": {"world": True},
            },
        ],
    )
    again = [beat_resolved(beat_id=new_id(), participant_id="p", intent_text="i", narration="n")]
    later = await store.append_beat(branch, again)
    fired = await evaluate_rules(
        store, branch, rules=expire.rules, trigger_events=again, world_day=3
    )
    result = await run_rules_gauntlet(store, branch, fired, trigger_commit=later.commit_id)
    assert {e.event_type for e in result.events} == {"ClaimTruthChanged"}
    check_provenance(result.events)


async def test_both_distillers_pass_the_check(store: PostgresEventStore) -> None:
    branch, _, _ = await _world(store)
    bundle = OutcomeBundle(
        encounter_id=f"e:{new_id()}",
        participants=["a:x", "a:y", "a:king"],
        witnesses=["a:y"],
        casualties=["a:x", "a:king"],
        feats=[Feat(actor="a:y", description="held the gate alone")],
        loot=[LootTransfer(item_id="i:crown", from_ref="a:king", to_ref="a:y")],
    )
    untrusted = await distill_outcome(store, branch, bundle)
    check_provenance(untrusted)
    authored = await distill_authored_outcome(store, branch, bundle)  # a protected king dies
    assert any(e.event_type == "ActorDied" for e in authored.events)
    check_provenance(authored.events)
    await store.append_beat(branch, authored.events)


async def test_the_store_refuses_a_mislabelled_commit(store: PostgresEventStore) -> None:
    branch, _, _ = await _world(store)
    head = (await store.get_branch(branch)).head_commit  # type: ignore[union-attr]
    forged = sheet_updated(actor_id="a:x", sheet={"hp": 0}, caused_by=external_cause("e:9"))
    with pytest.raises(ProvenanceError):
        await store.append_beat(branch, [forged])
    assert (await store.get_branch(branch)).head_commit == head  # type: ignore[union-attr]


async def test_an_import_refuses_a_mislabelled_bundle(store: PostgresEventStore) -> None:
    branch, _, _ = await _world(store)
    world = (await store.get_branch(branch)).world_id  # type: ignore[union-attr]
    bundle = await store.export_world(world)
    assert (await store.import_world(bundle)).world_id  # a clean world imports
    event = bundle.commits[-1].events[0]
    event.event_type = "SheetUpdated"
    event.caused_by = {"kind": "external", "encounter_id": "e:9"}
    event.payload = {"actor_id": "a:x", "sheet": {"hp": 0}}
    stamp_chain(bundle)  # a self-consistent chain: only the provenance is wrong
    with pytest.raises(ExportError, match="bundle refused"):
        await store.import_world(bundle)
