"""Phase 2 increment 2.1: the timeline substrate — markers, snapshots, materialization,
and branch-from-any-commit (docs/03).

Deterministic (no LLM). These prove the machinery the meteor test (2.2) stands on:
forking carries world state as of a commit, a fork from a *past* commit excludes later
events, siblings never contaminate each other, and materialization restores the nearest
snapshot then replays forward (O(window), not O(history)).
"""

import hashlib

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    actor_created,
    beat_resolved,
    belief_changed,
    claim_recorded,
    place_created,
    place_destroyed,
    place_state_changed,
    terrain_changed,
)
from uro_core.domain.ids import new_id
from uro_core.timeline.models import World


async def _world(store: PostgresEventStore) -> World:
    return await store.create_world(f"test-{new_id()}")


# --- places projection (the meteor needs it; deferred in Phase 1) ---


async def test_place_projection_lifecycle(store: PostgresEventStore) -> None:
    w = await _world(store)
    b = w.main_branch_id
    await store.append_beat(
        b, [place_created(place_id="p:vel", name="Vel", kind="settlement", description="a port")]
    )
    place = await store.get_place(b, "p:vel")
    assert place is not None and place.name == "Vel" and place.status == "active"
    assert place.kind == "settlement" and place.description == "a port"

    await store.append_beat(
        b, [place_state_changed(place_id="p:vel", changes={"name": "Vel City"})]
    )
    assert (await store.get_place(b, "p:vel")).name == "Vel City"

    await store.append_beat(
        b, [terrain_changed(place_id="p:vel", description="a smoking crater", effects=["fire"])]
    )
    assert (await store.get_place(b, "p:vel")).description == "a smoking crater"

    await store.append_beat(b, [place_destroyed(place_id="p:vel", cause="meteor")])
    assert (await store.get_place(b, "p:vel")).status == "destroyed"


async def test_place_state_change_ignores_unknown_keys(store: PostgresEventStore) -> None:
    # The changes{} whitelist must not let an errant key touch a non-projection column.
    w = await _world(store)
    b = w.main_branch_id
    await store.append_beat(b, [place_created(place_id="p:x", name="X")])
    await store.append_beat(
        b,
        [
            place_state_changed(
                place_id="p:x", changes={"branch_id": "hijack", "status": "destroyed"}
            )
        ],
    )
    place = await store.get_place(b, "p:x")
    assert place is not None and place.status == "destroyed"  # legit key applied
    assert await store.get_place("hijack", "p:x") is None  # branch_id key ignored


def test_place_state_change_rejects_invalid_enum() -> None:
    # Review fix: changes{} must clear the same enum bar PlaceCreated does, at the mint
    # path — a bogus status like 'exploded' would confuse destroyed-vs-active state checks.
    place_state_changed(place_id="p:x", changes={"name": "ok", "status": "destroyed"})  # valid
    with pytest.raises(ValueError):
        place_state_changed(place_id="p:x", changes={"status": "exploded"})
    with pytest.raises(ValueError):
        place_state_changed(place_id="p:x", changes={"kind": "moon"})


# --- fork carries state as of the fork commit ---


async def test_fork_carries_world_state(store: PostgresEventStore) -> None:
    w = await _world(store)
    b = w.main_branch_id
    head = await store.append_beat(
        b,
        [
            actor_created(actor_id="a:duke", name="Duke Vel", tier=2, role="ruler"),
            place_created(place_id="p:vel", name="Vel", kind="settlement"),
            claim_recorded(
                claim_id="c:war",
                statement="The Duke plans war.",
                subject_refs=["a:duke"],
                truth="true",
            ),
            belief_changed(actor_id="a:duke", claim_id="c:war", confidence=0.9),
        ],
    )
    fork = await store.fork_branch(w.world_id, head.commit_id, "sibling")

    # Everything the world knew is carried — actors, places, claims, beliefs.
    assert (await store.get_actor(fork.branch_id, "a:duke")).name == "Duke Vel"
    assert (await store.get_place(fork.branch_id, "p:vel")).name == "Vel"
    assert [c.claim_id for c in await store.claims_about(fork.branch_id, "a:duke")] == ["c:war"]
    beliefs = await store.beliefs_of(fork.branch_id, "a:duke")
    assert len(beliefs) == 1 and beliefs[0].confidence == 0.9
    assert fork.forked_from is not None and fork.head_commit == fork.forked_from


async def test_fork_from_marker_name(store: PostgresEventStore) -> None:
    w = await _world(store)
    b = w.main_branch_id
    await store.append_beat(b, [actor_created(actor_id="a:mera", name="Mera", tier=1)])
    marker = await store.create_marker(w.world_id, "campaign-a-end", b)

    fork = await store.fork_branch(w.world_id, "campaign-a-end", "aftermath")
    assert fork.forked_from == marker.commit_id
    assert (await store.get_actor(fork.branch_id, "a:mera")).name == "Mera"


async def test_fork_rejects_duplicate_and_reserved_names(store: PostgresEventStore) -> None:
    # Review fix: branch names are unique per world (git-like); 'main' is always taken.
    w = await _world(store)
    b = w.main_branch_id
    head = await store.append_beat(b, [actor_created(actor_id="a:x", name="X")])
    await store.fork_branch(w.world_id, head.commit_id, "aftermath")
    with pytest.raises(ValueError):
        await store.fork_branch(w.world_id, head.commit_id, "aftermath")  # duplicate
    with pytest.raises(ValueError):
        await store.fork_branch(w.world_id, head.commit_id, "main")  # reserved (already exists)


# --- the meteor mechanic at the substrate level ---


async def test_fork_from_past_commit_excludes_later_events(store: PostgresEventStore) -> None:
    w = await _world(store)
    b = w.main_branch_id
    await store.append_beat(b, [place_created(place_id="p:vel", name="Vel", kind="settlement")])
    pre_meteor = await store.create_marker(w.world_id, "pre-meteor", b)
    after = await store.append_beat(b, [place_destroyed(place_id="p:vel", cause="meteor")])

    # main branch: the strike happened.
    assert (await store.get_place(b, "p:vel")).status == "destroyed"

    # what-if: forked BEFORE the strike → Vel still stands.
    whatif = await store.fork_branch(w.world_id, "pre-meteor", "whatif")
    assert (await store.get_place(whatif.branch_id, "p:vel")).status == "active"

    # continue: forked AT the head → Vel is a crater.
    aftermath = await store.fork_branch(w.world_id, after.commit_id, "aftermath")
    assert (await store.get_place(aftermath.branch_id, "p:vel")).status == "destroyed"

    assert pre_meteor.commit_id != after.commit_id


async def test_sibling_branches_do_not_contaminate(store: PostgresEventStore) -> None:
    w = await _world(store)
    b = w.main_branch_id
    root = await store.append_beat(b, [actor_created(actor_id="a:root", name="Root")])
    left = await store.fork_branch(w.world_id, root.commit_id, "left")
    right = await store.fork_branch(w.world_id, root.commit_id, "right")

    await store.append_beat(left.branch_id, [actor_created(actor_id="a:left", name="LeftOnly")])
    await store.append_beat(right.branch_id, [actor_created(actor_id="a:right", name="RightOnly")])

    # Each fork sees only its own post-fork commits; the shared ancestor is on both.
    assert await store.get_actor(left.branch_id, "a:root") is not None
    assert await store.get_actor(right.branch_id, "a:root") is not None
    assert await store.get_actor(left.branch_id, "a:left") is not None
    assert await store.get_actor(left.branch_id, "a:right") is None
    assert await store.get_actor(right.branch_id, "a:left") is None
    assert await store.get_actor(b, "a:left") is None  # parent untouched too

    left_beats = await store.recent_beats(left.branch_id, 10)
    assert all("RightOnly" not in b.narration for b in left_beats)


async def test_two_forks_at_same_commit_are_identical(store: PostgresEventStore) -> None:
    # State-at-a-commit is branch-independent (immutable ancestry) — two forks agree.
    w = await _world(store)
    b = w.main_branch_id
    head = await store.append_beat(
        b,
        [
            actor_created(actor_id="a:x", name="X", tier=2, role="smith", aliases=["the smith"]),
            claim_recorded(claim_id="c:x", statement="X forged it.", truth="true"),
        ],
    )
    f1 = await store.fork_branch(w.world_id, head.commit_id, "f1")
    f2 = await store.fork_branch(w.world_id, head.commit_id, "f2")

    a1, a2 = await store.get_actor(f1.branch_id, "a:x"), await store.get_actor(f2.branch_id, "a:x")
    assert a1 is not None and a2 is not None
    assert a1.model_dump() == a2.model_dump()
    c1 = await store.get_claim(f1.branch_id, "c:x")
    c2 = await store.get_claim(f2.branch_id, "c:x")
    assert c1.model_dump() == c2.model_dump()


# --- snapshots: materialization restores nearest snapshot + replays forward ---


async def test_materialization_uses_nearest_snapshot(store: PostgresEventStore) -> None:
    store._snapshot_every = 3  # snapshot at depths 3, 6, …
    w = await _world(store)
    b = w.main_branch_id
    head = None
    for i in range(7):  # depths 1..7
        head = await store.append_beat(b, [actor_created(actor_id=f"a:{i}", name=f"A{i}")])
    assert head is not None and head.depth == 7

    # snapshots exist exactly at the multiples of 3 for THIS world.
    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.depth FROM snapshots s JOIN commits c ON c.commit_id = s.commit_id "
            "WHERE c.world_id = $1 ORDER BY c.depth",
            w.world_id,
        )
    assert [r["depth"] for r in rows] == [3, 6]

    # materializing at the head restores the depth-6 snapshot and replays ONLY depth 7.
    target = f"mat-{new_id()}"
    async with store.pool.acquire() as conn, conn.transaction():
        ancestry = await store._ancestry(conn, head.commit_id)
        window = await store._materialize_into(conn, target, ancestry)
    assert window == 1  # not O(history): one commit replayed on top of the snapshot

    # …and the materialized state is complete: all 7 actors present.
    assert len(await store.list_actors(target)) == 7


async def test_full_replay_when_no_snapshot(store: PostgresEventStore) -> None:
    store._snapshot_every = 50  # no snapshot within a short history
    w = await _world(store)
    b = w.main_branch_id
    head = None
    for i in range(4):
        head = await store.append_beat(b, [actor_created(actor_id=f"a:{i}", name=f"A{i}")])
    target = f"mat-{new_id()}"
    async with store.pool.acquire() as conn, conn.transaction():
        ancestry = await store._ancestry(conn, head.commit_id)
        window = await store._materialize_into(conn, target, ancestry)
    # genesis (depth 0, no projectable state) + 4 beats replayed from scratch.
    assert window == 5
    assert len(await store.list_actors(target)) == 4


async def test_current_world_time_batch(store: PostgresEventStore) -> None:
    """B5: many branches' in-fiction day in one query - matching the per-branch method, siblings
    isolated, empty input -> empty, unknown/dayless branch absent (caller defaults it to 0)."""
    w = await _world(store)
    main = w.main_branch_id
    root = await store.append_beat(main, [actor_created(actor_id="a:0", name="Root")])
    a = await store.fork_branch(w.world_id, root.commit_id, f"a-{new_id()}")
    b = await store.fork_branch(w.world_id, root.commit_id, f"b-{new_id()}")
    await store.time_skip(a.branch_id, 100)
    await store.time_skip(b.branch_id, 250)
    days = await store.current_world_time_batch([main, a.branch_id, b.branch_id])
    assert days.get(a.branch_id) == 100  # skipped
    assert days.get(b.branch_id) == 250  # sibling untouched by a's skip
    assert days.get(main, 0) == 0  # never advanced
    for br in (a.branch_id, b.branch_id):  # agrees with the per-branch method
        assert days[br] == await store.current_world_time(br)
    assert await store.current_world_time_batch([]) == {}  # empty in -> empty out
    assert "nope" not in await store.current_world_time_batch(["nope"])  # unknown absent


# --- markers & ref resolution ---


async def test_marker_resolution_and_duplicates(store: PostgresEventStore) -> None:
    w = await _world(store)
    b = w.main_branch_id
    head = await store.append_beat(b, [actor_created(actor_id="a:x", name="X")])
    marker = await store.create_marker(w.world_id, "here", b)
    assert marker.commit_id == head.commit_id

    assert await store.resolve_ref(w.world_id, "here") == head.commit_id  # by name
    assert await store.resolve_ref(w.world_id, head.commit_id) == head.commit_id  # by commit id
    with pytest.raises(KeyError):
        await store.resolve_ref(w.world_id, "nope")
    with pytest.raises(ValueError):
        await store.create_marker(w.world_id, "here", b)  # duplicate name


async def test_list_branches_and_lineage(store: PostgresEventStore) -> None:
    w = await _world(store)
    b = w.main_branch_id
    c1 = await store.append_beat(
        b,
        [
            beat_resolved(
                beat_id="b1", participant_id="p1", intent_text="enter the tavern", narration="n"
            )
        ],
    )
    await store.create_marker(w.world_id, "start", b)
    await store.fork_branch(w.world_id, c1.commit_id, "aftermath")

    branches = await store.list_branches(w.world_id)
    assert {br.name for br in branches} == {"main", "aftermath"}

    entries = await store.lineage(b, 10)
    assert entries[0].depth == 1 and entries[0].summary == "enter the tavern"
    assert "start" in entries[0].markers
    assert entries[-1].depth == 0  # genesis at the tail
    assert "WorldGenesis" in entries[-1].event_types


# --- memory copy-on-fork: pointers duplicated, embeddings shared ---


async def test_memory_copies_on_fork_without_reembedding(store: PostgresEventStore) -> None:
    w = await _world(store)
    b = w.main_branch_id
    c = await store.append_beat(
        b, [beat_resolved(beat_id="b1", participant_id="p1", intent_text="look", narration="n")]
    )
    # Unique per run: the embeddings corpus is deduped by content hash *globally*
    # (across worlds/branches, by design), and the dev DB persists between runs — a
    # constant text would let prior runs inflate the count.
    text = f"the party found a salt-crusted amulet {new_id()}"
    await store.add_memory(
        branch_id=b,
        commit_id=c.commit_id,
        kind="beat",
        text=text,
        vector=[1.0, 0.0, 0.0, 0.0],
        entity_refs=[],
    )
    fork = await store.fork_branch(w.world_id, c.commit_id, "aftermath")

    hits = await store.search(fork.branch_id, [1.0, 0.0, 0.0, 0.0], 3)
    assert any("amulet" in h.text for h in hits)  # recall works on the fork

    # the embedding vector was NOT recomputed — it lives once, by content hash.
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    async with store.pool.acquire() as conn:
        n_vectors = await conn.fetchval(
            "SELECT count(*) FROM embeddings WHERE content_hash = $1", content_hash
        )
        n_pointers = await conn.fetchval(
            "SELECT count(*) FROM memory_index WHERE content_hash = $1", content_hash
        )
    assert n_vectors == 1 and n_pointers == 2  # one vector, two branch pointers
