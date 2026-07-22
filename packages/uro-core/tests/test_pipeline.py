"""Phase 1 increment 2: the extractor → gauntlet → recall loop (prose becomes canon).

Gauntlet tests inspect the events it produces; the engine test drives the full loop
with a scripted provider and shows extracted state resurfacing via recall.
"""

from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import DomainEvent, actor_created, claim_recorded
from uro_core.domain.extraction_policy import ExtractionPolicy
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.extraction import (
    Extraction,
    ProposedActor,
    ProposedClaim,
    ProposedFaction,
    ProposedPlace,
    ProposedThread,
    run_gauntlet,
)
from uro_core.pipeline.recall import assemble_recall
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter


class ScriptedProvider:
    """Streams a fixed narration; returns queued JSON for each extractor call."""

    def __init__(
        self, *, narration: str = "The fire crackles.", completions: list[str] | None = None
    ):
        self._narration = narration
        self._completions = list(completions or [])

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield self._narration

    async def complete(self, req: CompletionRequest) -> str:
        return self._completions.pop(0) if self._completions else '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def _branch(store: PostgresEventStore) -> str:
    world = await store.create_world(f"test-{new_id()}")
    return world.main_branch_id


def _of_type(events: list[DomainEvent], t: str) -> list[DomainEvent]:
    return [e for e in events if e.event_type == t]


async def test_narrator_claim_becomes_true(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    ex = Extraction(
        claims=[ProposedClaim(statement="The cellar door is locked.", provenance="narrator")]
    )
    events = await run_gauntlet(store, branch, ex)
    claims = _of_type(events, "ClaimRecorded")
    assert len(claims) == 1 and claims[0].payload["truth"] == "true"
    assert _of_type(events, "BeliefChanged") == []  # narrator fact, no belief


async def test_gauntlet_drops_flavor_claims(store: PostgresEventStore) -> None:
    # Flavor (durable=false) is not canon (docs/05); a real fact alongside it survives.
    # Guards the fix for the first live run's over-extraction (~5 claims/beat, mostly atmosphere).
    branch = await _branch(store)
    ex = Extraction(
        claims=[
            ProposedClaim(statement="The Duke disbanded his army.", durable=True),
            ProposedClaim(statement="The fire crackles merrily in the hearth.", durable=False),
        ]
    )
    events = await run_gauntlet(store, branch, ex)
    kept = _of_type(events, "ClaimRecorded")
    assert len(kept) == 1 and kept[0].payload["statement"] == "The Duke disbanded his army."


async def test_dialogue_claim_is_testimony_plus_belief(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(branch, [actor_created(actor_id="a:flora", name="Flora", tier=2)])
    ex = Extraction(
        claims=[
            ProposedClaim(
                statement="The Duke plans war.",
                about=["Duke"],
                provenance="dialogue",
                speaker="Flora",
            )
        ]
    )
    events = await run_gauntlet(store, branch, ex)
    claim = _of_type(events, "ClaimRecorded")[0]
    assert claim.payload["truth"] == "unknown"  # a character saying it ≠ truth
    belief = _of_type(events, "BeliefChanged")[0]
    assert belief.payload["actor_id"] == "a:flora"  # resolved the speaker
    assert belief.payload["claim_id"] == claim.payload["claim_id"]


async def test_contradiction_downgrades_a_would_be_fact(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            claim_recorded(
                claim_id="c:pacifist",
                statement="The Duke disbanded his army.",
                subject_refs=["name:duke"],
                truth="true",
            )
        ],
    )
    # A new narrator-asserted claim that the extractor flags as contradicting the fact.
    ex = Extraction(
        claims=[
            ProposedClaim(
                statement="The Duke masses troops.",
                about=["Duke"],
                provenance="narrator",
                contradicts=["c:pacifist"],
            )
        ]
    )
    events = await run_gauntlet(store, branch, ex)
    claim = _of_type(events, "ClaimRecorded")[0]
    assert claim.payload["truth"] == "unknown"  # downgraded — can't hold two contradictory truths


async def test_dialogue_claim_about_speaker_links_to_actor_id(store: PostgresEventStore) -> None:
    # A speaker not pre-listed as an actor, asserting something about themselves: the
    # claim subject and the belief must resolve to the SAME minted actor_id, not diverge
    # into a name-token vs a:uuid (review Phase-1.2).
    branch = await _branch(store)
    ex = Extraction(
        claims=[
            ProposedClaim(
                statement="I poisoned the ale.",
                about=["Flora"],
                provenance="dialogue",
                speaker="Flora",
            )
        ]
    )
    events = await run_gauntlet(store, branch, ex)
    actor_ref = _of_type(events, "ActorCreated")[0].payload["actor_id"]
    claim = _of_type(events, "ClaimRecorded")[0]
    belief = _of_type(events, "BeliefChanged")[0]
    assert claim.payload["subject_refs"] == [actor_ref]  # linked to the actor, not name:flora
    assert belief.payload["actor_id"] == actor_ref


async def test_entity_resolution_deduplicates_actors(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(branch, [actor_created(actor_id="a:weck", name="Weck", tier=1)])
    ex = Extraction(actors=[ProposedActor(name="Weck"), ProposedActor(name="Bran")])
    events = await run_gauntlet(store, branch, ex)
    created = _of_type(events, "ActorCreated")
    assert [e.payload["name"] for e in created] == ["Bran"]  # Weck linked, only Bran created
    assert created[0].payload["tier"] == 1  # tier ceiling


async def test_gauntlet_extracts_an_emergent_place(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    ex = Extraction(places=[ProposedPlace(name="The Rusty Tankard", description="a dim tavern")])
    created = _of_type(await run_gauntlet(store, branch, ex), "PlaceCreated")
    assert [e.payload["name"] for e in created] == ["The Rusty Tankard"]
    assert created[0].payload["kind"] == "site"  # emergent places default to a site
    assert created[0].payload["description"] == "a dim tavern"


async def test_gauntlet_deduplicates_places(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    # commit "Alder Hollow", then re-propose it (case/article-folded) alongside a new place
    first = await run_gauntlet(
        store, branch, Extraction(places=[ProposedPlace(name="Alder Hollow")])
    )
    await store.append_beat(branch, first)
    ex = Extraction(
        places=[ProposedPlace(name="alder hollow"), ProposedPlace(name="The Deep Mine")]
    )
    created = _of_type(await run_gauntlet(store, branch, ex), "PlaceCreated")
    assert [e.payload["name"] for e in created] == ["The Deep Mine"]  # Alder Hollow deduped


async def test_extraction_policy_gates_each_category(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    ex = Extraction(
        actors=[ProposedActor(name="Mara")],
        places=[ProposedPlace(name="The Vault")],
        claims=[ProposedClaim(statement="The vault is sealed.", provenance="narrator")],
    )
    # each run is independent (nothing committed) — a disabled category drops, the rest fire
    off_places = await run_gauntlet(
        store, branch, ex, policy=ExtractionPolicy(extract_places=False)
    )
    assert _of_type(off_places, "PlaceCreated") == []
    assert len(_of_type(off_places, "ActorCreated")) == 1
    assert len(_of_type(off_places, "ClaimRecorded")) == 1

    off_actors = await run_gauntlet(
        store, branch, ex, policy=ExtractionPolicy(extract_actors=False)
    )
    assert _of_type(off_actors, "ActorCreated") == []  # no new actor minted
    assert len(_of_type(off_actors, "PlaceCreated")) == 1

    off_claims = await run_gauntlet(
        store, branch, ex, policy=ExtractionPolicy(extract_claims=False)
    )
    assert _of_type(off_claims, "ClaimRecorded") == []
    assert len(_of_type(off_claims, "ActorCreated")) == 1  # actors/places still emerge


async def test_extraction_policy_store_roundtrip(store: PostgresEventStore) -> None:
    # The policy is an instance-wide SINGLETON, so mutating it leaks across tests via the shared
    # test DB — restore the all-on default in a finally so later beat tests aren't poisoned.
    assert (await store.get_extraction_policy()).extract_places is True  # migration default: all on
    try:
        await store.set_extraction_policy(
            ExtractionPolicy(extract_actors=True, extract_places=False, extract_claims=False)
        )
        got = await store.get_extraction_policy()
        assert (got.extract_actors, got.extract_places, got.extract_claims) == (True, False, False)
    finally:
        await store.set_extraction_policy(ExtractionPolicy())


async def test_engine_extracts_state_and_recall_resurfaces_it(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    branch = campaign.branch_id

    extraction_json = (
        '{"actors":[{"name":"Flora","role":"innkeeper"}],'
        '"claims":[{"statement":"The Duke disbanded his army.","about":["Duke"],'
        '"provenance":"narrator"}]}'
    )
    provider = ScriptedProvider(
        narration="Flora wipes a mug. 'The Duke? He disbanded his army years ago.'",
        completions=[extraction_json],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=provider))

    result = await engine.run_beat(campaign, "player-1", "I ask Flora about the Duke")
    assert result.extracted == 2  # Flora (actor) + the Duke claim

    # State was committed and projected.
    assert (await store.find_actor_by_name(branch, "Flora")) is not None
    duke_claims = await store.claims_about(branch, "name:duke")
    assert len(duke_claims) == 1 and duke_claims[0].truth == "true"

    # A later beat mentioning the Duke recalls the established fact.
    recall = await assemble_recall(store, branch, "what do I know about the Duke?", 8)
    assert any(c.statement == "The Duke disbanded his army." for c in recall.claims)


async def test_fact_consistency_metric(store: PostgresEventStore) -> None:
    # Thesis metric T2: narrator claims surviving as truth=true are consistent; those
    # downgraded to unknown are not; dialogue (testimony) claims are excluded.
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            claim_recorded(claim_id="c1", statement="A", truth="true", origin="narrator"),
            claim_recorded(claim_id="c2", statement="B", truth="true", origin="narrator"),
            claim_recorded(claim_id="c3", statement="C", truth="unknown", origin="narrator"),
            claim_recorded(claim_id="c4", statement="D", truth="unknown", origin="dialogue"),
        ],
    )
    consistent, total = await store.fact_consistency(branch)
    assert (consistent, total) == (2, 3)  # dialogue excluded; one narrator claim downgraded


async def test_bare_mode_is_a_true_ablation(store: PostgresEventStore) -> None:
    # The T1 baseline: same scripted narration + extraction, but bare mode records ONLY
    # the transcript — no state, no memory — so it can be A/B'd against the full engine.
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    provider = ScriptedProvider(
        narration="Flora reveals a hidden passage.",
        completions=['{"actors":[{"name":"Flora"}],"claims":[{"statement":"a passage exists"}]}'],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=provider), bare=True)
    result = await engine.run_beat(campaign, "player-1", "I ask Flora")

    assert result.extracted == 0  # bare → no extraction, though the script would have made 2
    assert await store.list_actors(campaign.branch_id) == []  # no state built
    hits = await store.search(campaign.branch_id, hashing_embedding("hidden passage"), k=5)
    assert hits == []  # no memory indexed


async def test_extractor_reasks_on_unparseable_json_then_extracts(
    store: PostgresEventStore,
) -> None:
    # docs/13: the extractor gets up to 2 re-asks on unparseable output (like the planner) before
    # falling back to a narration-only beat. A single malformed response must NOT silently drop the
    # beat's whole state. First completion is garbage; the re-ask returns valid JSON with a claim.
    world = await store.create_world(f"reask-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    good = '{"actors":[],"claims":[{"statement":"The bridge collapsed.","provenance":"narrator"}]}'
    provider = ScriptedProvider(
        narration="The old bridge groans and gives way.",
        completions=["```not json at all```", good],  # attempt 1 unparseable → re-ask → attempt 2
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=provider))
    result = await engine.run_beat(campaign, "player-1", "I cross the bridge")
    assert result.extracted == 1  # the re-ask salvaged the state (was 0 = narration-only before)


async def test_extractor_falls_back_to_narration_only_after_exhausting_reasks(
    store: PostgresEventStore,
) -> None:
    # Three unparseable completions (1 + 2 re-asks) → the beat still COMMITS, narration-only.
    world = await store.create_world(f"reask2-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    provider = ScriptedProvider(
        narration="Mist rolls in.", completions=["nope", "still nope", "nope again"]
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=provider))
    result = await engine.run_beat(campaign, "player-1", "I look around")
    assert result.commit_id and result.extracted == 0  # prose kept, no state (the honest fallback)


# --- D-50: emergent RELATIONAL world-building (cascade + edges + affiliation-aware dedup) --------


async def test_relational_actor_cascades_faction_and_place(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    ex = Extraction(
        actors=[
            ProposedActor(
                name="Ser Alden", role="knight", member_of="Iron Order", located_in="Alder Hollow"
            )
        ]
    )
    events = await run_gauntlet(store, branch, ex)
    aid = _of_type(events, "ActorCreated")[0].payload["actor_id"]
    fid = _of_type(events, "FactionCreated")[0].payload["faction_id"]
    pid = _of_type(events, "PlaceCreated")[0].payload["place_id"]
    edges = {
        (e.payload["src"], e.payload["rel_type"], e.payload["dst"])
        for e in _of_type(events, "EdgeAdded")
    }
    assert (aid, "member_of", fid) in edges  # the Order + the Hollow were cascade-created + wired
    assert (aid, "located_in", pid) in edges


async def test_same_name_actors_of_different_houses_stay_distinct(
    store: PostgresEventStore,
) -> None:
    branch = await _branch(store)
    await store.append_beat(
        branch,
        await run_gauntlet(
            store, branch, Extraction(actors=[ProposedActor(name="the Duke", member_of="Ashfall")])
        ),
    )
    # a Duke of a DIFFERENT house → a NEW, distinct actor (relationship-aware dedup)
    ev = await run_gauntlet(
        store, branch, Extraction(actors=[ProposedActor(name="the Duke", member_of="Windhelm")])
    )
    assert len(_of_type(ev, "ActorCreated")) == 1
    # the SAME house → linked, not recreated
    ev2 = await run_gauntlet(
        store, branch, Extraction(actors=[ProposedActor(name="the Duke", member_of="Ashfall")])
    )
    assert _of_type(ev2, "ActorCreated") == []


async def test_bare_mention_links_and_enriches(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(
        branch, await run_gauntlet(store, branch, Extraction(actors=[ProposedActor(name="Mara")]))
    )
    # a later mention adds an affiliation → links to Mara (no new actor) + enriches the graph
    ev = await run_gauntlet(
        store, branch, Extraction(actors=[ProposedActor(name="Mara", member_of="Gray Watch")])
    )
    assert _of_type(ev, "ActorCreated") == []  # linked, not duplicated
    assert len(_of_type(ev, "FactionCreated")) == 1  # Gray Watch created
    assert len(_of_type(ev, "EdgeAdded")) == 1  # Mara member_of Gray Watch (enrichment)


async def test_place_parent_cascade(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    ex = Extraction(places=[ProposedPlace(name="The Broken Spindle", parent="Alder Hollow")])
    events = await run_gauntlet(store, branch, ex)
    assert {e.payload["name"] for e in _of_type(events, "PlaceCreated")} == {
        "The Broken Spindle",
        "Alder Hollow",
    }
    assert len(_of_type(events, "EdgeAdded")) == 1  # spindle located_in Alder Hollow


async def test_emergent_factions_and_threads(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    ex = Extraction(
        factions=[ProposedFaction(name="The Ember Cult", description="fire-worshippers")],
        threads=[ProposedThread(stakes="a war looms on the Ashfall border")],
    )
    events = await run_gauntlet(store, branch, ex)
    assert [e.payload["name"] for e in _of_type(events, "FactionCreated")] == ["The Ember Cult"]
    thr = _of_type(events, "ThreadCreated")
    assert len(thr) == 1 and thr[0].payload["provenance"] == "emergent"


async def test_policy_gates_factions_and_threads(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    ex = Extraction(
        actors=[ProposedActor(name="Knight", member_of="Order")],  # cascade faction
        threads=[ProposedThread(stakes="a plot")],
    )
    off_f = await run_gauntlet(store, branch, ex, policy=ExtractionPolicy(extract_factions=False))
    assert _of_type(off_f, "FactionCreated") == []  # cascade suppressed
    assert _of_type(off_f, "EdgeAdded") == []  # no member_of edge (faction wasn't made)
    assert len(_of_type(off_f, "ActorCreated")) == 1  # the actor is still made
    assert len(_of_type(off_f, "ThreadCreated")) == 1

    off_t = await run_gauntlet(store, branch, ex, policy=ExtractionPolicy(extract_threads=False))
    assert _of_type(off_t, "ThreadCreated") == []
    assert len(_of_type(off_t, "FactionCreated")) == 1  # factions still on


async def test_alias_mention_links_not_duplicates(store: PostgresEventStore) -> None:
    # Review HIGH: a pack-authored actor mentioned by an ALIAS must link (not split)
    # — the relational rewrite dropped find_actor_by_name's alias matching.
    branch = await _branch(store)
    await store.append_beat(
        branch, [actor_created(actor_id="a:mera", name="Mera", tier=2, aliases=["the barkeep"])]
    )
    ex = Extraction(
        actors=[ProposedActor(name="the barkeep", role="innkeeper")],
        claims=[
            ProposedClaim(
                statement="The barkeep waters the ale.",
                about=["the barkeep"],
                provenance="narrator",
            )
        ],
    )
    events = await run_gauntlet(store, branch, ex)
    assert _of_type(events, "ActorCreated") == []  # linked to Mera via the alias, no duplicate
    assert _of_type(events, "ClaimRecorded")[0].payload["subject_refs"] == ["a:mera"]  # right actor


async def test_affiliation_off_wires_no_edge_to_existing_faction(store: PostgresEventStore) -> None:
    # Review C1: with factions OFF, an actor's member_of wires NO edge even to a PRE-EXISTING
    # (authored) faction — the policy gate is at the call site, before resolve_* links the id.
    from uro_core.domain.events import faction_created

    branch = await _branch(store)
    await store.append_beat(branch, [faction_created(faction_id="f:order", name="Iron Order")])
    ex = Extraction(actors=[ProposedActor(name="Knight", member_of="Iron Order")])
    ev = await run_gauntlet(store, branch, ex, policy=ExtractionPolicy(extract_factions=False))
    assert _of_type(ev, "EdgeAdded") == []  # no member_of edge, even to the existing faction
    assert len(_of_type(ev, "ActorCreated")) == 1  # the actor is still made
