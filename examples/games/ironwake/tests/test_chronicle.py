"""Inc 2-5 assertions against a REAL store: the Chronicler write path, the protection
downgrade, the witnessless silence, and the near/far rumor-confidence split."""

from __future__ import annotations

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.chronicler import OutcomeBundle, distill_outcome

from ironwake.world.setup import CORIN, MIRA, ODO, VORLUND, VORLUNDS_BLADE, seed_world

MERC = "a:merc-gerhardt"
MERC2 = "a:merc-elke"


async def _report(store: PostgresEventStore, branch: str, bundle: OutcomeBundle) -> int:
    events = await distill_outcome(store, branch, bundle)
    await store.append_beat(branch, events)
    return len(events)


async def test_deaths_feats_loot_reach_canon(store: PostgresEventStore) -> None:
    m = await seed_world(store, season_seed=90001)
    b = m.branch_id
    # muster one tier-0 raider owning a lootable item (the game's authored write path)
    from uro_core.domain.events import actor_created, item_created

    await store.append_beat(
        b,
        [
            actor_created(actor_id="a:rb-t-raider", name="Test Raider", tier=0, role="raider"),
            item_created(item_id="i:t-torc", name="a torc", owner_ref="a:rb-t-raider"),
        ],
    )
    await _report(
        store,
        b,
        OutcomeBundle(
            encounter_id="e:t-cull",
            participants=[MERC, MERC2, "a:rb-t-raider"],
            witnesses=[MERC2],
            casualties=["a:rb-t-raider", MERC],  # the raider AND a merc fall
            feats=[{"actor": MERC2, "description": "Elke stood the line at the test field"}],
            loot=[{"item_id": "i:t-torc", "from_ref": "a:rb-t-raider", "to_ref": MERC2}],
            duration_rounds=3,
        ),
    )
    raider = await store.get_actor(b, "a:rb-t-raider")
    merc = await store.get_actor(b, MERC)
    assert raider is not None and raider.status == "dead"  # tier 0: really dies
    assert merc is not None and merc.status == "dead"  # tier 1 merc: permadeath is canon
    feat_claims = [c for c in await store.claims_about(b, MERC2) if "stood the line" in c.statement]
    assert feat_claims and all(c.truth == "unknown" for c in feat_claims)  # testimony, not canon
    assert "i:t-torc" in await store.items_owned_by(b, MERC2)  # loot moved


async def test_protection_ceiling_downgrades_vorlund(store: PostgresEventStore) -> None:
    m = await seed_world(store, season_seed=90002)
    b = m.branch_id
    await _report(
        store,
        b,
        OutcomeBundle(
            encounter_id="e:t-headhunt",
            participants=[MERC, VORLUND],
            witnesses=[MERC],
            casualties=[VORLUND],
            loot=[{"item_id": VORLUNDS_BLADE, "from_ref": VORLUND, "to_ref": MERC}],
        ),
    )
    vorlund = await store.get_actor(b, VORLUND)
    assert vorlund is not None and vorlund.status == "alive"  # the world refused the death
    fell = [c for c in await store.claims_about(b, VORLUND) if "said to have fallen" in c.statement]
    assert fell and all(c.truth == "unknown" for c in fell)  # a story, not a fact
    assert VORLUNDS_BLADE in await store.items_owned_by(b, VORLUND)  # loot refused


async def test_witnessless_wipe_leaves_no_rumor(store: PostgresEventStore) -> None:
    m = await seed_world(store, season_seed=90003)
    b = m.branch_id
    from uro_core.domain.events import actor_created

    await store.append_beat(
        b,
        [
            actor_created(actor_id="a:rb-t-brute", name="Test Brute", tier=0, role="brute"),
        ],
    )
    await _report(
        store,
        b,
        OutcomeBundle(
            encounter_id="e:t-mill",
            participants=[MERC, "a:rb-t-brute"],
            witnesses=[],  # nobody walked away
            casualties=[MERC, "a:rb-t-brute"],
            feats=[{"actor": MERC, "description": "Gerhardt held the burning mill alone"}],
        ),
    )
    merc = await store.get_actor(b, MERC)
    brute = await store.get_actor(b, "a:rb-t-brute")
    assert merc is not None and merc.status == "dead"  # the deaths still record...
    assert brute is not None and brute.status == "dead"
    for npc in (MIRA, CORIN, ODO):
        assert await store.beliefs_of(b, npc) == []  # ...but the legend is LOST


async def test_rumor_confidence_decays_down_the_knows_chain(store: PostgresEventStore) -> None:
    m = await seed_world(store, season_seed=90004)
    b = m.branch_id
    await _report(
        store,
        b,
        OutcomeBundle(
            encounter_id="e:t-feat",
            participants=[MERC, MERC2],
            witnesses=[MERC],  # eyewitness merc -> Mira -> Corin -> (floor) Odo
            feats=[{"actor": MERC2, "description": "Elke felled five in the test press"}],
        ),
    )

    def conf(beliefs):
        return {x.actor_id: x.confidence for x in beliefs}

    mira = conf(await store.beliefs_of(b, MIRA))
    corin = conf(await store.beliefs_of(b, CORIN))
    odo = conf(await store.beliefs_of(b, ODO))
    assert mira and corin, "the tale must reach both towns"
    (mira_c,) = mira.values()
    (corin_c,) = corin.values()
    assert mira_c > corin_c  # near = confident, far = hedged
    # the narrator's phrasing thresholds (recall._certainty): believes vs has-heard-a-rumor
    assert 0.45 <= mira_c < 0.75 and corin_c < 0.45
    assert odo == {}  # 3 hops: below the propagation floor — the far fen hears NOTHING


async def test_out_of_cast_refs_drop_or_downgrade(store: PostgresEventStore) -> None:
    m = await seed_world(store, season_seed=90005)
    b = m.branch_id
    await _report(
        store,
        b,
        OutcomeBundle(
            encounter_id="e:t-probe",
            participants=[MERC],
            witnesses=[],
            casualties=[MIRA],  # exists, never in the declared cast
            feats=[{"actor": MERC2, "description": "a feat by someone not in the fight"}],
        ),
    )
    mira = await store.get_actor(b, MIRA)
    assert mira is not None and mira.status == "alive"  # scope: no out-of-cast kill
    lies = [c for c in await store.claims_about(b, MERC2) if "not in the fight" in c.statement]
    assert lies == []  # out-of-cast feat: dropped entirely
    gossip = [c for c in await store.claims_about(b, MIRA) if "said to have fallen" in c.statement]
    assert gossip, "an out-of-cast casualty DOWNGRADES to gossip rather than dropping (D-32 nuance)"
