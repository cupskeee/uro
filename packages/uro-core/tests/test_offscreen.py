"""OQ-4 narrowed (D-76): the world moves while nobody watches — at a time-skip, deterministically.

Ashfall now authors who knows whom along the salt road, a rumor nobody has heard, and three
agendas: the Saltborn feud heats up with each skip of a week or more, word of a burned wagon
travels inland once (Old Weck saw it → Hesk the carter → Brannoc at the Lantern, two hops away in
the next region), and after a week the stopped wagons become a plot. No LLM: agendas are data,
rumors spread along `knows` edges with per-hop confidence decay, and a third-hand belief reaches
the narrator as "has heard a rumor".
"""

import shutil
from pathlib import Path

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.errors import PackError
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.recall import assemble_recall, build_narrator_messages
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.router import ProviderRouter
from uro_core.worldpack.create import import_pack
from uro_core.worldpack.parse import parse_pack

WORLDS = Path(__file__).resolve().parents[3] / "worlds"
RUMOR = "c:wagon-burned"


async def _belief(store: PostgresEventStore, branch: str, actor: str) -> tuple[float, str | None]:
    held = [b for b in await store.beliefs_of(branch, actor) if b.claim_id == RUMOR]
    return (held[0].confidence, held[0].learned_from) if held else (0.0, None)


async def test_the_world_moved_while_you_were_away(store: PostgresEventStore) -> None:
    world = (await import_pack(store, parse_pack(WORLDS / "ashfall"))).world
    branch = world.main_branch_id
    before = (await store.get_branch(branch)).head_commit  # type: ignore[union-attr]
    # nobody has heard the rumor, the feud is cold, the salt-road plot sleeps
    assert await _belief(store, branch, "a:weck") == (0.0, None)
    # ...and the narrator can't tell it either, however the Saltborn come up (review: with the
    # Saltborn as its subject it was a scene rumor from day 0)
    day0 = await assemble_recall(store, branch, "I ask Brannoc about the Saltborn raiders", 8)
    assert RUMOR not in {c.claim_id for c in [*day0.claims, *day0.belief_claims]}
    assert {t.thread_id: t.state for t in await store.list_threads(branch)}["t:salt-road"] == (
        "dormant"
    )

    engine = Engine(store, ProviderRouter(bindings={}, default=StubProvider()))
    await engine.agenda_tick(branch, 30)  # a month away: four weekly boundaries crossed

    # the skip and the world's reaction are separate commits, the reaction caused by module rules
    head, skip = await store.lineage(branch, 2)
    assert "TimeAdvanced" in skip.event_types
    assert {"CounterChanged", "BeliefChanged", "ThreadStateChanged"} <= set(head.event_types)
    # each weekly agenda fires once per skip, however many weeks it spans
    assert await store.get_counter(branch, "f:saltborn", "war_heat") == 1
    assert {t.thread_id: t.state for t in await store.list_threads(branch)}["t:salt-road"] == (
        "offered"
    )
    # the rumor travelled inland, fading with each retelling, and stopped at the floor
    assert await _belief(store, branch, "a:weck") == (0.9, None)  # saw it
    assert await _belief(store, branch, "a:hesk") == (0.495, "a:weck")
    assert await _belief(store, branch, "a:brannoc") == (0.272, "a:hesk")  # the next region
    assert await _belief(store, branch, "a:ysolde") == (0.0, None)  # 0.15 < the 0.2 floor

    # at the Lantern, the regular retells it as a rumor, not as fact
    recall = await assemble_recall(store, branch, "I ask Brannoc what news came up the road", 8)
    assert RUMOR in {c.claim_id for c in recall.belief_claims}
    narrator = "\n".join(m.content for m in build_narrator_messages(recall, "I ask Brannoc"))
    assert "Brannoc has heard a rumor: Saltborn raiders burned a salt wagon" in narrator

    # the next week the feud heats again, but the rumor isn't told all over again (review: it
    # re-witnessed and re-spread the whole chain every skip)
    await engine.agenda_tick(branch, 7)
    (again,) = await store.lineage(branch, 1)
    assert "BeliefChanged" not in again.event_types
    assert await store.get_counter(branch, "f:saltborn", "war_heat") == 2

    # a timeline forked before the skip never moved
    fork = await store.fork_branch(world.world_id, before, "stayed-home")
    assert await _belief(store, fork.branch_id, "a:brannoc") == (0.0, None)
    assert await store.get_counter(fork.branch_id, "f:saltborn", "war_heat") == 0

    # and the whole of it travels with the world: export, verify, import (rebuilt by replay)
    imported = await store.import_world(await store.export_world(world.world_id))
    assert await _belief(store, imported.main_branch_id, "a:brannoc") == (0.272, "a:hesk")
    edges = await store.list_edges(imported.main_branch_id, "knows")
    assert {("a:weck", "a:hesk"), ("a:hesk", "a:weck")} <= {(e.src, e.dst) for e in edges}


def test_a_knows_ref_to_nobody_is_refused(tmp_path: Path) -> None:
    # D-76: an edge to nobody would be accepted and do nothing, silently cutting a rumor's path.
    pack = tmp_path / "ashfall"
    shutil.copytree(WORLDS / "ashfall", pack)
    actors = pack / "entities" / "actors.yaml"
    actors.write_text(actors.read_text().replace('knows: ["a:hesk"]', 'knows: ["a:hesq"]'))
    with pytest.raises(PackError, match="a:hesq"):
        parse_pack(pack)


def test_an_agenda_written_into_rules_yaml_is_refused(tmp_path: Path) -> None:
    # Found authoring this content: rules.yaml reads only `rules`, so an `agendas:` list written
    # there was accepted and never ran. The loader now names the file it belongs in.
    pack = tmp_path / "ashfall"
    shutil.copytree(WORLDS / "ashfall", pack)
    agendas = (pack / "agendas.yaml").read_text().split("\n", 1)[1]  # without its version line
    (pack / "agendas.yaml").unlink()
    with (pack / "rules.yaml").open("a") as f:
        f.write(agendas)
    with pytest.raises(PackError, match=r"`agendas` goes in agendas\.yaml"):
        parse_pack(pack)


def test_rule_files_may_leave_the_version_to_each_other(tmp_path: Path) -> None:
    # Review: before D-76 an agendas.yaml with no version line took rules.yaml's; keep that.
    pack = tmp_path / "ashfall"
    shutil.copytree(WORLDS / "ashfall", pack)
    agendas = pack / "agendas.yaml"
    agendas.write_text(agendas.read_text().split("\n", 1)[1])  # drop `rules_api_version: 7`
    assert parse_pack(pack).rule_pack.agendas  # type: ignore[union-attr]
    agendas.write_text("rules_api_version: 6\n" + agendas.read_text())
    with pytest.raises(PackError, match=r"\(7 and 6\)"):
        parse_pack(pack)


def test_a_rule_file_with_odd_keys_is_a_pack_error(tmp_path: Path) -> None:
    # Review: two non-string top-level keys made the stray-key check raise TypeError (a 500).
    pack = tmp_path / "ashfall"
    shutil.copytree(WORLDS / "ashfall", pack)
    with (pack / "rules.yaml").open("a") as f:
        f.write("1: x\nfoo: y\n")
    with pytest.raises(PackError, match="doesn't read"):
        parse_pack(pack)
