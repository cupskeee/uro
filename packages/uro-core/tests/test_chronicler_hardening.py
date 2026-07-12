"""Phase 8 (OQ-12 → D-32): Chronicler ingestion is TRUST-SCOPED. Deterministic — no LLM.

An external game is untrusted beyond its declared encounter. These tests are the abuse suite from
the adversarial audit: a bundle must NOT kill/loot/first-hand-witness a PC, a T2+ named actor, or a
bystander it never fought; loot needs real ownership; oversized bundles are capped; a replay is
idempotent. The war-story happy path (feat → witness rumor) is in test_war_story.py.
"""

import pytest
from pydantic import ValidationError
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.chronicler import Feat, LootTransfer, OutcomeBundle, distill_outcome
from uro_core.domain.events import actor_created, edge_added, item_created
from uro_core.domain.ids import new_id


async def _world(store: PostgresEventStore) -> str:
    """A branch with: two T1 combatants (grull, raider), a T3 named figure (the king), a PC
    (hero), a knows-edge for belief propagation, and items owned by grull + the king."""
    world = await store.create_world(f"chron-{new_id()}")
    branch = world.main_branch_id
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:grull", name="Grull", tier=1),
            actor_created(actor_id="a:raider", name="A raider", tier=1),
            actor_created(actor_id="a:king", name="The King", tier=3, role="ruler"),
            actor_created(actor_id="a:hero", name="Hero", tier=2),
            edge_added(src="a:raider", rel_type="knows", dst="a:king"),
            item_created(item_id="i:sword", name="sword", owner_ref="a:grull"),
            item_created(item_id="i:crown", name="crown", owner_ref="a:king"),
        ],
    )
    # a:hero is a PC on this branch (participant p1)
    campaign = await store.start_campaign(
        world.world_id, branch, participant_id="p1", adopt_actor_id="a:hero"
    )
    assert campaign.campaign_id
    return branch


async def _dead(store: PostgresEventStore, branch: str, actor: str) -> bool:
    a = await store.get_actor(branch, actor)
    return a is not None and a.status == "dead"


# --- domain scope: a bundle can't kill outside its declared cast, or protected canon ---


async def test_out_of_domain_casualty_downgrades_to_testimony(store: PostgresEventStore) -> None:
    branch = await _world(store)
    # a malicious bundle lists the KING (never in the fight) as a casualty
    bundle = OutcomeBundle(
        encounter_id="e:1", participants=["a:grull"], casualties=["a:king", "a:grull"]
    )
    await store.append_beat(branch, await distill_outcome(store, branch, bundle))
    assert not await _dead(store, branch, "a:king")  # the king is NOT killed by an external POST
    assert await _dead(store, branch, "a:grull")  # an in-cast T1 combatant does die
    # the king's "death" survives only as a hedged rumor, never committed canon
    king_claims = await store.claims_about(branch, "a:king")
    assert any(c.truth == "unknown" and "fallen" in c.statement for c in king_claims)


async def test_pc_casualty_downgrades_to_testimony(store: PostgresEventStore) -> None:
    branch = await _world(store)
    # even a PC listed as a participant-casualty is not killed by an external bundle
    bundle = OutcomeBundle(encounter_id="e:2", participants=["a:hero"], casualties=["a:hero"])
    await store.append_beat(branch, await distill_outcome(store, branch, bundle))
    assert not await _dead(
        store, branch, "a:hero"
    )  # a PC's fate is not the external game's to take


async def test_protected_t2plus_participant_casualty_downgrades(store: PostgresEventStore) -> None:
    branch = await _world(store)
    # the king declared as a participant-casualty is STILL protected (T3 named canon) → testimony
    bundle = OutcomeBundle(encounter_id="e:3", participants=["a:king"], casualties=["a:king"])
    await store.append_beat(branch, await distill_outcome(store, branch, bundle))
    assert not await _dead(store, branch, "a:king")


# --- loot integrity: real item, real ownership, in-cast, unprotected ---


async def test_loot_requires_real_ownership_and_scope(store: PostgresEventStore) -> None:
    branch = await _world(store)
    bundle = OutcomeBundle(
        encounter_id="e:4",
        participants=["a:grull", "a:raider"],
        loot=[
            LootTransfer(item_id="i:sword", from_ref="a:grull", to_ref="a:raider"),  # legit
            LootTransfer(item_id="i:sword", from_ref="a:raider", to_ref="a:grull"),  # forged owner
            LootTransfer(
                item_id="i:crown", from_ref="a:king", to_ref="a:raider"
            ),  # king not in cast
            LootTransfer(item_id="i:ghost", from_ref="a:grull", to_ref="a:raider"),  # no such item
        ],
    )
    await store.append_beat(branch, await distill_outcome(store, branch, bundle))
    assert (await store.get_item(branch, "i:sword"))[
        "owner_ref"
    ] == "a:raider"  # only the legit move
    assert (await store.get_item(branch, "i:crown"))[
        "owner_ref"
    ] == "a:king"  # the crown never moved


# --- witnesses: real, in-cast, alive, unprotected ---


async def test_forged_and_protected_witnesses_get_no_belief(store: PostgresEventStore) -> None:
    branch = await _world(store)
    # witnesses list a nonexistent actor, the PC (protected), and a real in-cast T1 raider
    bundle = OutcomeBundle(
        encounter_id="e:5",
        participants=["a:grull", "a:raider", "a:hero"],
        witnesses=["a:nobody", "a:hero", "a:raider"],
        feats=[Feat(actor="a:grull", description="Grull cleaved the standard in two")],
    )
    await store.append_beat(branch, await distill_outcome(store, branch, bundle))
    feat = next(c for c in await store.claims_about(branch, "a:grull") if "standard" in c.statement)
    assert feat.truth == "unknown"
    # only the real, unprotected, in-cast witness believes it
    assert [b.claim_id for b in await store.beliefs_of(branch, "a:raider")] == [feat.claim_id]
    assert (
        await store.beliefs_of(branch, "a:hero") == []
    )  # a PC is not conscripted as an eyewitness
    assert await store.beliefs_of(branch, "a:nobody") == []  # a nonexistent witness gets nothing


async def test_feat_about_an_out_of_cast_or_unknown_actor_is_dropped(
    store: PostgresEventStore,
) -> None:
    branch = await _world(store)
    bundle = OutcomeBundle(
        encounter_id="e:6",
        participants=["a:grull"],
        feats=[
            Feat(actor="a:king", description="The king single-handedly won"),  # not in cast
            Feat(actor="a:ghost", description="A phantom did it"),  # unknown actor
        ],
    )
    events = await distill_outcome(store, branch, bundle)
    assert events == []  # neither feat is attributable to a declared combatant


# --- anti-abuse: caps + idempotent replay ---


def test_oversized_bundle_is_rejected_at_the_schema() -> None:
    with pytest.raises(ValidationError):
        OutcomeBundle(encounter_id="e", feats=[Feat(actor="a", description="x")] * 65)  # > cap
    with pytest.raises(ValidationError):
        OutcomeBundle(encounter_id="e", casualties=["a"] * 65)


async def test_replaying_a_bundle_is_idempotent(store: PostgresEventStore) -> None:
    branch = await _world(store)
    bundle = OutcomeBundle(
        encounter_id="e:7",
        participants=["a:grull", "a:raider"],
        witnesses=["a:raider"],
        casualties=["a:grull"],
        loot=[LootTransfer(item_id="i:sword", from_ref="a:grull", to_ref="a:raider")],
        feats=[Feat(actor="a:grull", description="Grull fought to the last")],
    )
    for _ in range(3):  # re-POST the SAME bundle three times
        await store.append_beat(branch, await distill_outcome(store, branch, bundle))

    # committed effects apply exactly once: one death, one item move, one feat claim, one belief
    assert await _dead(store, branch, "a:grull")
    assert (await store.get_item(branch, "i:sword"))["owner_ref"] == "a:raider"
    feats = [c for c in await store.claims_about(branch, "a:grull") if "last" in c.statement]
    assert len(feats) == 1  # deterministic claim id → upsert, not duplicate
    beliefs = await store.beliefs_of(branch, "a:raider")
    assert len([b for b in beliefs if b.claim_id == feats[0].claim_id]) == 1


async def test_feat_actor_resolves_like_the_extractor_exact_name_wins(
    store: PostgresEventStore,
) -> None:
    # P8xP1 cross-phase fix: feat.actor must resolve via find_actor_by_name the SAME way the P1
    # extractor does — raw name, so the exact-name tiebreak wins over a canonical-only duplicate.
    world = await store.create_world(f"dup-{new_id()}")
    branch = world.main_branch_id
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:strexact", name="The Stranger", tier=2),
            actor_created(actor_id="a:strdup", name="Stranger", tier=1),  # same canonical form
        ],
    )
    bundle = OutcomeBundle(
        encounter_id="e:dup",
        participants=["a:strexact"],  # only the exact-name actor is in the cast
        feats=[Feat(actor="The Stranger", description="The Stranger turned the rout")],
    )
    await store.append_beat(branch, await distill_outcome(store, branch, bundle))
    # the feat attaches to the EXACT-name actor (a:strexact), in cast — not the canonical dup
    assert any("rout" in c.statement for c in await store.claims_about(branch, "a:strexact"))
    assert not any("rout" in c.statement for c in await store.claims_about(branch, "a:strdup"))


async def test_loot_to_a_fallen_recipient_is_dropped(store: PostgresEventStore) -> None:
    # gap Ironwake: a recipient reported as a casualty can't carry loot off the field. grull's sword
    # would transfer to the raider, but the raider is ALSO a casualty this bundle → dropped.
    branch = await _world(store)
    bundle = OutcomeBundle(
        encounter_id="e:loot-dead",
        participants=["a:grull", "a:raider"],
        casualties=["a:grull", "a:raider"],  # both fell; the raider can't receive loot
        loot=[LootTransfer(item_id="i:sword", from_ref="a:grull", to_ref="a:raider")],
    )
    await store.append_beat(branch, await distill_outcome(store, branch, bundle))
    item = await store.get_item(branch, "i:sword")
    assert item["owner_ref"] == "a:grull"  # never moved to the dead raider


async def test_ingestion_receipt_reports_dispositions(store: PostgresEventStore) -> None:
    # gap B6 (Ironwake rows 1-2, Seventh G-22): a Chronicler consumer learns per-ref what its bundle
    # did — an applied death, a protected-canon DOWNGRADE, a dropped loot — not just a bare count.
    from uro_core.chronicler import distill_outcome_with_receipt

    branch = await _world(store)
    bundle = OutcomeBundle(
        encounter_id="e:receipt",
        participants=["a:grull", "a:king", "a:hero"],
        casualties=["a:grull", "a:king"],  # grull dies (T1 in-cast); king downgrades (T3 protected)
        loot=[
            LootTransfer(item_id="i:crown", from_ref="a:king", to_ref="a:hero")
        ],  # king protected
    )
    result = await distill_outcome_with_receipt(store, branch, bundle)
    by_ref = {(r.kind, r.ref): r.disposition for r in result.receipt}
    assert by_ref[("casualty", "a:grull")] == "applied"
    assert (
        by_ref[("casualty", "a:king")] == "downgraded"
    )  # protected → rumor, visible to the consumer
    assert by_ref[("loot", "i:crown")] == "dropped"  # king's gear can't be looted out-of-band
    # and the wrapper still returns just events for the ergonomic path
    assert isinstance(await distill_outcome(store, branch, bundle), list)
