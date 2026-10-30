"""The CLI's read commands against the REAL store (parity with the server's reads): world list,
campaign list/show, state, events, show, chronicle, branch diff, usage, ruleset list.

Driven from uro-core (like test_rest_management) because the DB `store` fixture lives here. The
setup is async; each command runs through Typer's CliRunner in a sync test (the CLI owns its event
loop), pointed at the same dedicated test database.
"""

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
