"""The CLI's read commands against the REAL store (parity with the server's reads): world list,
campaign list/show, state, events, show, chronicle, branch diff, usage, ruleset list.

Driven from uro-core (like test_rest_management) because the DB `store` fixture lives here. The
setup is async; each command runs through Typer's CliRunner in a sync test (the CLI owns its event
loop), pointed at the same dedicated test database.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from typer.testing import CliRunner
from uro_cli.main import app
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, beat_resolved
from uro_core.domain.ids import new_id


@pytest_asyncio.fixture
async def world(store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("URO_DATABASE_URL", store.dsn)  # the CLI opens its own store on it
    w = await store.create_world(f"cli-reads-{new_id()}")
    main = w.main_branch_id
    campaign = await store.start_campaign(
        w.world_id, main, participant_id="p1", new_pc_name="Ash", new_pc_id=f"a:ash-{new_id()}"
    )
    mara = await store.append_beat(
        main,
        [
            actor_created(actor_id="a:mara", name="Mara"),
            beat_resolved(
                beat_id=new_id(),
                participant_id="p1",
                intent_text="I hail the ferrywoman",
                narration="Mara looks up from her nets.",
            ),
        ],
    )
    await store.fork_branch(w.world_id, mara.commit_id, "what-if")
    fork = await store.get_branch_by_name(w.world_id, "what-if")
    assert fork is not None
    await store.append_beat(fork.branch_id, [actor_created(actor_id="a:ghost", name="Ghost")])
    return SimpleNamespace(name=w.name, campaign=campaign.campaign_id, commit=mara.commit_id)


def _uro(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.output


def test_world_and_campaign_listings(world: SimpleNamespace) -> None:
    code, out = _uro("world", "list")
    assert code == 0 and world.name in out
    code, out = _uro("campaign", "list", "--world", world.name)
    assert code == 0 and world.campaign in out and "branch=main" in out
    code, out = _uro("campaign", "show", world.campaign)
    assert code == 0 and "p1 → a:ash-" in out and "(Ash)" in out


def test_state_events_and_show(world: SimpleNamespace) -> None:
    code, out = _uro("state", world.campaign, "--sections", "actors")
    assert code == 0 and '"Mara"' in out and '"Ash"' in out
    code, out = _uro("state", world.campaign, "--sections", "nope")
    assert code == 1 and "unknown section" in out
    code, out = _uro("events", world.name, "--type", "ActorCreated")
    assert code == 0 and "Mara" in out and "BeatResolved" not in out
    code, out = _uro("log", world.name)
    assert code == 0 and world.commit in out  # the FULL id, so it can always be copied back
    code, out = _uro("show", world.name, world.commit)
    assert code == 0 and world.commit in out and "ActorCreated" in out and "BeatResolved" in out
    code, out = _uro("show", world.name, "ZZZZZZZZ")
    assert code == 1


def test_chronicle_for_a_campaign_or_any_branch(world: SimpleNamespace) -> None:
    code, out = _uro("chronicle", world.campaign)
    assert code == 0 and "I hail the ferrywoman" in out and "Mara looks up" in out
    code, out = _uro("chronicle", "--world", world.name, "--branch", "what-if")
    assert code == 0 and "Mara looks up" in out  # the fork shares the history before it
    code, out = _uro("chronicle")
    assert code == 1


def test_branch_diff(world: SimpleNamespace) -> None:
    code, out = _uro("branch", "diff", world.name, "main", "what-if", "--sections", "actors")
    assert code == 0 and "+ " in out and "Ghost" in out
    code, out = _uro("branch", "diff", world.name, "what-if", "main", "--sections", "actors")
    assert code == 0 and "- " in out and "Ghost" in out
    code, out = _uro("branch", "diff", world.name, "main", "main")
    assert code == 0 and "no differences" in out


def test_usage_and_rulesets(world: SimpleNamespace) -> None:
    code, out = _uro("usage")
    assert code == 0 and "total calls:" in out
    code, out = _uro("ruleset", "list")
    assert code == 0 and "uro-basic@0" in out and "uro-pool@0" in out


async def test_resolve_ref_takes_a_unique_commit_prefix(store: PostgresEventStore) -> None:
    w = await store.create_world(f"prefix-{new_id()}")
    commit = await store.append_beat(w.main_branch_id, [actor_created(actor_id="a:x", name="X")])
    # the first 10 characters are a timestamp shared with genesis; 16 reach the random part
    assert await store.resolve_ref(w.world_id, commit.commit_id[:16]) == commit.commit_id
    assert await store.resolve_ref(w.world_id, commit.commit_id[:16].lower()) == commit.commit_id
    # 6 characters span ~17 minutes of timestamp, so genesis and this beat always share them
    with pytest.raises(KeyError, match="ambiguous"):
        await store.resolve_ref(w.world_id, commit.commit_id[:6])
    for not_a_prefix in (commit.commit_id[:5], commit.commit_id[:7] + "%"):
        with pytest.raises(KeyError):
            await store.resolve_ref(w.world_id, not_a_prefix)
    other = await store.create_world(f"prefix-other-{new_id()}")
    with pytest.raises(KeyError):  # a prefix only resolves within its own world
        await store.resolve_ref(other.world_id, commit.commit_id[:10])


def test_world_new_takes_a_ruleset(world: SimpleNamespace) -> None:
    name = f"pool-{new_id()}"
    code, out = _uro("world", "new", name, "--ruleset", "uro-pool")
    assert code == 0, out
    campaign = out.split("campaign: ")[1].split()[0]
    code, out = _uro("campaign", "show", campaign)
    assert "ruleset  uro-pool@" in out
    code, out = _uro("state", campaign, "--sections", "sheets")
    assert '"wounds"' in out  # a Uro Pool sheet
    assert _uro("world", "new", f"x-{new_id()}", "--ruleset", "srd51")[0] == 1


async def test_state_at_any_commit_matches_a_fork_and_leaves_no_trace(
    store: PostgresEventStore,
) -> None:
    # World state at a past commit: the SAME path a fork materializes by, then rolled back.
    store._snapshot_every = 3  # cross a snapshot boundary: restore + replay, not replay alone
    w = await store.create_world(f"state-at-{new_id()}")
    main = w.main_branch_id
    commits = []
    for i in range(7):
        c = await store.append_beat(main, [actor_created(actor_id=f"a:{i}", name=f"N{i}")])
        commits.append(c.commit_id)
    await store.create_marker(w.world_id, "fourth", main)  # names the head: 7 actors

    at, state = await store.state_at(w.world_id, commits[4], ["actors"])
    assert at == commits[4] and sorted(a["actor_id"] for a in state["actors"]) == [
        f"a:{i}" for i in range(5)
    ]
    fork = await store.fork_branch(w.world_id, commits[4], "check")
    forked = (await store.query_across([fork.branch_id], ["actors"]))[fork.branch_id]
    assert state == forked  # identical to what a fork there starts from
    _, by_marker = await store.state_at(w.world_id, "fourth", ["actors"])
    assert len(by_marker["actors"]) == 7

    names = [b.name for b in await store.list_branches(w.world_id)]
    assert sorted(names) == ["check", "main"]  # no scratch branch survived
    with pytest.raises(KeyError):
        await store.state_at(w.world_id, "nope", ["actors"])
    with pytest.raises(ValueError, match="unknown section"):
        await store.state_at(w.world_id, commits[0], ["nope"])


def test_cli_state_of_a_world_at_a_past_commit(world: SimpleNamespace) -> None:
    code, out = _uro("state", "--world", world.name, "--at", world.commit, "--sections", "actors")
    assert code == 0 and f"at commit {world.commit}" in out and '"Mara"' in out
    code, out = _uro("state", "--world", world.name, "--branch", "what-if", "--sections", "actors")
    assert code == 0 and '"Ghost"' in out
    code, out = _uro("state", "--world", world.name, "--sections", "actors")
    assert code == 0 and '"Ghost"' not in out  # main never saw the fork's actor
    assert _uro("state", "--world", world.name, "--at", "x", "--branch", "main")[0] == 1


def test_review_fixes_cli_guards(world: SimpleNamespace, tmp_path: Path) -> None:
    # `uro state <campaign> --at` used to ignore --at and print the campaign's present state
    assert _uro("state", world.campaign, "--at", world.commit)[0] == 1
    # a missing bundle file is a clean usage error, not a traceback
    code, out = _uro("campaign", "outcome", world.campaign, "/nonexistent/bundle.json")
    assert code == 2 and "Traceback" not in out


async def test_an_ambiguous_world_name_is_refused_not_guessed(
    store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # World names aren't unique (import a pack twice): the CLI used to act on an arbitrary one.
    monkeypatch.setenv("URO_DATABASE_URL", store.dsn)
    name = f"twin-{new_id()}"
    a = await store.create_world(name)
    b = await store.create_world(name)
    result = await asyncio.to_thread(CliRunner().invoke, app, ["log", name])
    assert result.exit_code == 1 and a.world_id in result.output and b.world_id in result.output
    by_id = await asyncio.to_thread(CliRunner().invoke, app, ["log", b.world_id])
    assert by_id.exit_code == 0


def test_a_pack_with_broken_yaml_is_a_pack_error(tmp_path: Path) -> None:
    import shutil
    from pathlib import Path

    from uro_core.errors import PackError
    from uro_core.worldpack.parse import parse_pack

    src = Path(__file__).resolve().parents[3] / "worlds" / "ashfall"
    dst = tmp_path / "ashfall"
    shutil.copytree(src, dst)
    (dst / "entities" / "places.yaml").write_text("- name: [unclosed\n")
    with pytest.raises(PackError, match="not valid YAML"):
        parse_pack(dst)


async def test_uro_calls_shows_what_the_extractor_returned(
    store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import AsyncIterator

    from uro_core.pipeline.engine import Engine
    from uro_core.providers.adapters.stub import hashing_embedding
    from uro_core.providers.base import CompletionRequest
    from uro_core.providers.router import ProviderRouter

    class _P:
        async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
            yield "Ilse nods."

        async def complete(self, req: CompletionRequest) -> str:
            return '{"actors": [{"name": "Ilse"}], "claims": []}'

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [hashing_embedding(t) for t in texts]

    monkeypatch.setenv("URO_DATABASE_URL", store.dsn)
    w = await store.create_world(f"calls-{new_id()}")
    campaign = await store.create_campaign(w.world_id, w.main_branch_id)
    await Engine(store, ProviderRouter(bindings={}, default=_P())).run_beat(campaign, "p1", "hi")
    result = await asyncio.to_thread(
        CliRunner().invoke,
        app,
        ["calls", "--stage", "extractor", "--campaign", campaign.campaign_id],
    )
    assert result.exit_code == 0, result.output
    assert "extractor" in result.output and '"name": "Ilse"' in result.output
    usage = await asyncio.to_thread(CliRunner().invoke, app, ["usage", "--world", w.world_id])
    assert "extractor" in usage.output and "narrator" in usage.output
