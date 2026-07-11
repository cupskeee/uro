"""HOLLOWLOOP's contract — the acceptance, and PROOF of every load-bearing claim the GAP REPORT
makes about the engine. Deterministic, no key. Run explicitly (see conftest):

    uv run pytest examples/games/hollowloop/tests
"""

from __future__ import annotations

from pathlib import Path

import game
import loop as loopmod
import pytest
from codex import FileCodex, open_codex
from loop import begin_loop, bootstrap, commit_the_fall
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.pipeline.recall import assemble_recall
from world import (
    CLUES,
    DOOM,
    DOOM_SEGMENT,
    DOOM_STATES,
    DOOM_WORDS,
    KEYSTONES,
    ORIGIN_REF,
    PARTICIPANT,
    PC_ID,
    VALE,
)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path


async def _vale(store: PostgresEventStore, out: Path):
    return await bootstrap(store, out, f"Vale of Mourn — test {new_id()[:6]}")


# --------------------------------------------------------------------------------------------
# The acceptance: the meteor test, as a game
# --------------------------------------------------------------------------------------------


async def test_the_story_breaks_the_loop(store: PostgresEventStore, out: Path) -> None:
    """Discover across a doomed loop; return knowing; ring the bell; the Vale survives."""
    vale = await _vale(store, out)
    codex = await open_codex("file", store=store, world_id=vale.world_id, out_dir=out)
    r = await game.story(vale, codex)

    # loop 1 fell: the Fall is committed PlaceDestroyed on THAT branch
    assert r["loop1"]["state"]["vale"] == "destroyed"
    assert r["loop1"]["state"]["doom"] == "fallen"
    assert sorted(r["loop1"]["discovered"]) == list(KEYSTONES)

    # loop 2 broke the cycle: the aversion is committed and the Vale is STILL STANDING
    assert r["loop2"]["outcome"].startswith("WARDED")
    assert r["loop2"]["state"]["vale"] == "active"
    assert r["loop2"]["state"]["doom"] == "warded"
    assert r["loop2"]["holds_key"] is True
    assert "m:broke-the-loop" in r["markers"] and ORIGIN_REF in r["markers"]

    # the tree is legible: origin, both loops, the sideways fork, the codex ledger
    names = {row["name"] for row in r["tree"]}
    assert {"main", "codex", "loop-0001", "loop-0002"} <= names
    assert any(n.startswith("whatif-") for n in names)


async def test_the_knowledge_boundary_is_the_whole_game(
    store: PostgresEventStore, out: Path
) -> None:
    """THE DoD claim: a clue found in loop 1 unlocks a gated intent in loop 2, while the WORLD
    itself has been reset by the fork. The Codex carried it; Uro did not."""
    vale = await _vale(store, out)
    codex = await open_codex("file", store=store, world_id=vale.world_id, out_dir=out)
    r = await game.story(vale, codex)

    assert r["boundary"]["codex_knows"] == list(KEYSTONES)  # the Loopwalker remembers everything
    assert r["boundary"]["world_remembers"] == []  # the fresh fork has never heard of it
    assert r["boundary"]["vale_is"] == "active"  # and the Vale is pristine again

    # and the gated intent that carried across: `search the well` needs K3, which lives ONLY in
    # the Codex on loop 2 — the branch has no claim about it.
    l3 = await begin_loop(vale, 3)
    l3.place = "p:well"
    keys = {o.key for o in loopmod.options(l3, codex)}
    assert "search" in keys, "K3 (from the Codex) must unlock the well on a branch that forgot it"
    statements = {c.statement for c in await store.list_claims(l3.branch_id)}
    assert CLUES["K3"]["statement"] not in statements, "the world must NOT remember K3"


async def test_a_fresh_fork_from_origin_is_pristine(store: PostgresEventStore, out: Path) -> None:
    """Fork isolation: a loop that fell leaves the origin — and every later loop — untouched."""
    vale = await _vale(store, out)
    doomed = await begin_loop(vale, 1)
    for _ in range(6):
        await loopmod.advance(doomed)
    await commit_the_fall(doomed)
    assert (await store.get_place(doomed.branch_id, VALE)).status == "destroyed"

    fresh = await begin_loop(vale, 2)
    assert (await store.get_place(fresh.branch_id, VALE)).status == "active"
    assert await store.current_world_time(fresh.branch_id) == 0  # dawn again
    assert (await store.get_place(vale.main_branch, VALE)).status == "active"  # origin untouched


async def test_pc_binding_survives_every_fork(store: PostgresEventStore, out: Path) -> None:
    """PC-ness through the fork (stress target 5): the Loopwalker is a PC on every loop, and the
    fork already carries the binding BEFORE the per-loop campaign adopts it."""
    vale = await _vale(store, out)
    fork = await store.fork_branch(vale.world_id, ORIGIN_REF, "bare-fork")
    # copy-on-write carried proj_pcs: the PC is already bound on the fork, with no campaign work
    assert await store.is_pc(fork.branch_id, PC_ID) is True
    assert await store.active_pcs(fork.branch_id) == [PC_ID]

    loop = await begin_loop(vale, 1)
    assert await store.is_pc(loop.branch_id, PC_ID) is True
    assert await store.pc_for_participant(loop.campaign.campaign_id, PARTICIPANT) == PC_ID


# --------------------------------------------------------------------------------------------
# Proof of the GAP REPORT's engine claims (each of these IS a gap row's evidence)
# --------------------------------------------------------------------------------------------


async def test_the_model_copy_rebind_is_correct_by_coincidence(
    store: PostgresEventStore, out: Path
) -> None:
    """GAP: there is no `rebind_campaign`. `campaign.model_copy(branch_id=fork)` runs beats on the
    fork, but the engine resolves the acting PC from the campaign ROW's branch — the ORIGIN — and
    is right only because the fork is a copy. Proven here, which is why the game does NOT do it.
    """
    vale = await _vale(store, out)
    origin_campaign = await store.get_campaign((await begin_loop(vale, 99)).campaign.campaign_id)
    assert origin_campaign is not None

    fork = await store.fork_branch(vale.world_id, ORIGIN_REF, "rebind-probe")
    # the campaigns ROW still points at the branch it was started on — nothing can change it
    assert origin_campaign.branch_id != fork.branch_id
    repointed = origin_campaign.model_copy(update={"branch_id": fork.branch_id})

    # the acting-PC lookup takes ONLY the campaign_id: it never sees the re-pointed object, and
    # resolves against the campaign row's own branch (store.py:641-656 joins on c.branch_id)
    assert await store.pc_for_participant(repointed.campaign_id, PARTICIPANT) == PC_ID
    assert repointed.branch_id == fork.branch_id  # ...even though the beat would run over here

    # there is no engine call that could fix this — no rebind exists anywhere on the store
    assert not hasattr(store, "rebind_campaign")


async def test_the_extractor_will_not_let_a_game_key_its_clues(
    store: PostgresEventStore, out: Path
) -> None:
    """GAP: `ProposedClaim` has no id field and the gauntlet mints `c:{ulid}` — so a clue's
    identity on a branch is its PROSE. (And `truth` is DERIVED from provenance, never chosen.)
    Meanwhile a claim the GAME authors keeps the id the game picked — hence the Codex's `k:` ids.
    """
    vale = await _vale(store, out)
    codex = await open_codex("file", store=store, world_id=vale.world_id, out_dir=out)

    loop = await begin_loop(vale, 1)
    opts = {o.key: o for o in loopmod.options(loop, codex)}
    await loopmod.choose(loop, opts["go:p:chapel"], codex)  # dawn -> morning, at the chapel
    opts = {o.key: o for o in loopmod.options(loop, codex)}
    await loopmod.choose(loop, opts["talk:a:aldis"], codex)  # K1 — the extractor commits it

    k1 = [
        c
        for c in await store.list_claims(loop.branch_id)
        if c.statement == CLUES["K1"]["statement"]
    ]
    assert len(k1) == 1, "the beat's extractor must have committed the keystone"
    claim = k1[0]
    # the id the game WANTED was "c:nature"; the engine minted its own and never asked
    assert claim.claim_id != CLUES["K1"]["id"]
    assert claim.claim_id.startswith("c:")
    # truth was DERIVED from provenance="narrator" — the extraction JSON cannot set it
    assert claim.truth == "true" and claim.origin == "narrator"
    # it bound to the real Elder, because `about` carried his NAME (an id would have dangled)
    assert "a:aldis" in claim.subject_refs
    # so the only stable handle a game has on an extracted fact is its exact prose:
    assert claim.statement in {c["statement"] for c in CLUES.values()}


async def test_the_snapshot_cadence_never_fires_in_a_fork_per_loop_game(
    store: PostgresEventStore, out: Path
) -> None:
    """GAP/finding: snapshots are written every 50 commits BY DEPTH, and every loop branch
    restarts at the origin's depth — so no loop branch ever gets deep enough. The only snapshot
    in the world is the one `create_marker` forced at the origin, and it is doing 100% of the
    materialization work."""
    vale = await _vale(store, out)
    for i in range(1, 6):
        loop = await begin_loop(vale, i)
        for _ in range(6):
            await loopmod.advance(loop)
        await commit_the_fall(loop)

    async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
        snapshots = await conn.fetchval(
            "SELECT count(*) FROM snapshots s JOIN commits c ON c.commit_id = s.commit_id "
            "WHERE c.world_id = $1",
            vale.world_id,
        )
        max_depth = await conn.fetchval(
            "SELECT max(depth) FROM commits WHERE world_id = $1", vale.world_id
        )
    assert snapshots == 1, "only the origin marker's snapshot should exist"
    assert max_depth < 50, "no loop branch ever reaches the depth%50 snapshot cadence"


async def test_both_codex_backends_carry_the_same_knowledge(
    store: PostgresEventStore, out: Path
) -> None:
    """The brief asked for a JSON file OR a never-forked Uro branch, and a justification. Both
    are implemented; both survive the fork that resets the world; only the branch one can hold a
    STABLE claim id (`k:K1`) — the very thing the extractor denies the loop branches."""
    vale = await _vale(store, out)
    file_codex = FileCodex(out / "codex.json")
    await file_codex.load()
    branch_codex = await open_codex("branch", store=store, world_id=vale.world_id, out_dir=out)

    for cx in (file_codex, branch_codex):
        assert await cx.record("K1", loop="loop-0001", segment=1) is True
        assert await cx.record("K1", loop="loop-0009", segment=3) is False  # idempotent
        assert cx.known() == {"K1"}
        assert cx.complete() is False

    # the branch Codex's knowledge is real, queryable Uro state with the id the GAME chose
    claims = {c.claim_id for c in await store.list_claims(branch_codex.branch_id)}
    assert "k:K1" in claims

    # and it survives a reload from Uro alone
    reloaded = await open_codex("branch", store=store, world_id=vale.world_id, out_dir=out)
    assert reloaded.known() == {"K1"}
    assert reloaded.entries()[0].loop == "loop-0001"


async def test_the_reaction_layer_actually_fires(store: PostgresEventStore, out: Path) -> None:
    """The dread ladder and the villagers' unease are load-bearing (they carry target 6's verdict,
    and commit_the_fall assumes t:doom reached `active`). A schema-VALID pack that simply never
    fires would leave every other test green — G-6's exact failure mode — so pin it."""
    vale = await _vale(store, out)
    loop = await begin_loop(vale, 1)
    threads = {t.thread_id: t.state for t in await store.list_threads(loop.branch_id)}
    assert threads[DOOM] == DOOM_STATES["looming"], "dawn: the doom is unseen"

    for _ in range(DOOM_SEGMENT):  # 6 segments of agenda_tick
        await loopmod.advance(loop)

    # the Reaction Layer escalated the thread, purely from world_day conditions
    threads = {t.thread_id: t.state for t in await store.list_threads(loop.branch_id)}
    assert threads[DOOM] == DOOM_STATES["imminent"], (
        f"at last light the doom must be imminent, not {DOOM_WORDS.get(threads[DOOM])}"
    )
    # ...and the module-authored rumors are on the branch (the only channel that carries
    # ESCALATING dread into the narrator's prose — a thread's stakes text never changes)
    rumors = [c for c in await store.list_claims(loop.branch_id) if c.origin == "module"]
    assert rumors, "the unease agendas must have recorded rumors"

    # The doom thread reaches the narrator (active/offered threads carry their STAKES text)...
    recall = await assemble_recall(store, loop.branch_id, "what is coming?", 6)
    assert any("Fall will end the Vale" in t.stakes for t in recall.active_threads)
    # ...and a module rumor reaches it only when its SUBJECT is on-stage — recall carries a claim
    # iff one of its subject_refs is an actor mentioned in the intent or the recent beats
    # (recall.py:73-79). So the villagers' unease surfaces when you are talking to a villager.
    with_sela = await assemble_recall(store, loop.branch_id, "I ask Chaplain Sela about the sky", 6)
    assert any(c.statement in {r.statement for r in rumors} for c in with_sela.claims), (
        "the unease rumor must reach the narrator when its subject is on-stage"
    )

    # the Fall then closes the ladder to its terminal state
    await commit_the_fall(loop)
    threads = {t.thread_id: t.state for t in await store.list_threads(loop.branch_id)}
    assert threads[DOOM] == DOOM_STATES["fallen"]


async def test_a_whatif_fork_carries_the_clock_and_the_reactions(
    store: PostgresEventStore, out: Path
) -> None:
    """A sideways fork must be taken from the branch HEAD, not from the last beat's commit —
    otherwise it silently drops that segment's time-skip and its Reaction-Layer rumors."""
    vale = await _vale(store, out)
    main = await begin_loop(vale, 1)
    for _ in range(4):
        await loopmod.advance(main)
    assert main.segment == 4

    wi = await begin_loop(
        vale, 0, from_ref=await main.head(), name="whatif-probe", place=main.place
    )
    # the what-if hydrated its clock FROM the branch — same segment, not one behind
    assert wi.segment == main.segment == 4
    assert await store.current_world_time(wi.branch_id) == 4
    # ...and it inherited the dread the agendas had already committed on the main line
    threads = {t.thread_id: t.state for t in await store.list_threads(wi.branch_id)}
    assert threads[DOOM] == DOOM_STATES["gathering"]
    assert [c for c in await store.list_claims(wi.branch_id) if c.origin == "module"]


async def test_the_codex_is_scoped_to_its_world(store: PostgresEventStore, out: Path) -> None:
    """A shared codex file would carry the Loopwalker's knowledge into a BRAND-NEW Vale, so a
    fresh game would start already knowing K1-K4 and be winnable on loop 1. Each world's Codex
    must be its own."""
    v1 = await _vale(store, out)
    c1 = await open_codex("file", store=store, world_id=v1.world_id, out_dir=out)
    await c1.record("K1", loop="loop-0001", segment=1)
    assert c1.known() == {"K1"}

    v2 = await _vale(store, out)  # a NEW Vale, same out_dir
    c2 = await open_codex("file", store=store, world_id=v2.world_id, out_dir=out)
    assert c2.known() == set(), "a fresh world must start with an empty Codex"
    assert c2.complete() is False


async def test_scale_harness_runs(store: PostgresEventStore, out: Path) -> None:
    """A small scale run, so CI proves the harness itself works (the real evidence is N=500).
    Also pins the two shipped-evidence claims the GAP report quotes."""
    from scale import run_scale

    summary = await run_scale(store, 5, out)
    assert summary["n"] == 5
    assert summary["branches"] > 7  # main + codex + 5 loops + the fork-cost benchmark's forks
    assert summary["events"] > 0
    assert (out / "scale-5.csv").exists()
    assert (out / "scale-5-summary.json").exists()

    # the snapshot finding, measured by the shipped harness (not asserted in prose)
    assert summary["snapshots"] == 1, "only the origin marker's snapshot ever exists"
    assert summary["max_commit_depth"] < 50, "no branch reaches the depth%50 cadence"
    # ...and the fork-cost inversion: the DEEPER, more RECENT commit is the slower fork point
    assert summary["deep_commit_depth"] > 1
    assert summary["fork_from_deep_ms"] > summary["fork_from_marker_ms"]
