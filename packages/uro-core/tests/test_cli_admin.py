"""The CLI's admin writes that matched server-only endpoints: extraction policy, connection
enable/disable/refresh/test, standalone credentials, in-place time-skip, and Chronicler outcomes.

Driven against the REAL test database (setup async, each command through CliRunner in a sync test),
like test_cli_reads. The `stub` provider kind keeps refresh/test offline.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from typer.testing import CliRunner
from uro_cli.main import app
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created
from uro_core.domain.ids import new_id


@pytest_asyncio.fixture
async def env(store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("URO_DATABASE_URL", store.dsn)
    monkeypatch.setenv("URO_SECRET_KEY", Fernet.generate_key().decode())
    w = await store.create_world(f"cli-admin-{new_id()}")
    pc = f"a:pc-{new_id()}"
    campaign = await store.start_campaign(
        w.world_id, w.main_branch_id, participant_id="p1", new_pc_name="Ash", new_pc_id=pc
    )
    rat = f"a:rat-{new_id()}"
    await store.append_beat(w.main_branch_id, [actor_created(actor_id=rat, name="Rat", tier=0)])
    conn = await store.add_connection(name=f"stub-{new_id()}", provider="stub")
    yield SimpleNamespace(campaign=campaign.campaign_id, world=w.name, pc=pc, rat=rat, conn=conn)
    await store.delete_connection(conn)


def _uro(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.output


def test_extraction_policy_show_and_set(env: SimpleNamespace) -> None:
    try:
        code, out = _uro("extraction-policy", "set", "--no-places", "--no-threads")
        assert code == 0 and "places    off" in out and "threads   off" in out
        code, out = _uro("extraction-policy", "show")
        assert "places    off" in out and "actors    on" in out  # unnamed ones untouched
        code, out = _uro("extraction-policy", "set")
        assert code == 1  # naming nothing is an error, not a silent no-op
    finally:
        _uro("extraction-policy", "set", "--places", "--threads")  # instance-level: restore


def test_connection_enable_disable_refresh_test(env: SimpleNamespace) -> None:
    code, out = _uro("provider", "disable", env.conn)
    assert code == 0
    code, out = _uro("provider", "list")
    assert f"{env.conn}" in out and "[disabled]" in out
    assert _uro("provider", "enable", env.conn)[0] == 0
    code, out = _uro("provider", "refresh", env.conn)
    assert code == 0 and "stub-chat  (chat)" in out and "stub-embed  (embedding)" in out
    code, out = _uro("provider", "test", env.conn, "--model", "stub-chat")
    assert code == 0 and out.startswith("ok:")
    assert _uro("provider", "disable", "nope")[0] == 1
    assert _uro("provider", "refresh", "nope")[0] == 1


def test_standalone_credential_add_then_link(env: SimpleNamespace) -> None:
    code, out = _uro("provider", "credential", "add", "--provider", "openai", "--api-key", "sk-x")
    assert code == 0 and "sk-x" not in out
    cred = out.split()[1]
    code, out = _uro("provider", "add", "linked", "--provider", "openai", "--credential", cred)
    assert code == 0
    conn = out.split()[1]
    try:
        code, out = _uro("provider", "credential", "rm", cred)
        assert code == 1 and conn in out  # in use, so refused without --force
    finally:
        _uro("provider", "rm", conn)
        _uro("provider", "credential", "rm", cred)


def test_campaign_time_skip_in_place(env: SimpleNamespace) -> None:
    code, out = _uro("campaign", "time-skip", env.campaign, "--days", "3")
    assert code == 0 and "on day 3" in out
    code, out = _uro("campaign", "show", env.campaign)
    assert "day 3" in out  # the campaign's OWN branch moved — no fork
    assert _uro("campaign", "time-skip", env.campaign, "--days", "0")[0] != 0


def test_campaign_outcome_records_a_chronicler_bundle(env: SimpleNamespace, tmp_path: Path) -> None:
    bundle = tmp_path / "outcome.json"
    bundle.write_text(
        json.dumps(
            {"encounter_id": "e1", "participants": [env.pc, env.rat], "casualties": [env.rat]}
        )
    )
    code, out = _uro("campaign", "outcome", env.campaign, str(bundle))
    assert code == 0 and "committed" in out
    code, out = _uro("events", env.world, "--type", "ActorDied")
    assert code == 0 and env.rat in out  # an unprotected T0 combatant really died (D-32)


def test_credential_rotate_and_connection_relink(env: SimpleNamespace) -> None:
    code, out = _uro("provider", "credential", "add", "--provider", "openai", "--api-key", "sk-a")
    first = out.split()[1]
    code, out = _uro("provider", "credential", "add", "--provider", "openai", "--api-key", "sk-b")
    second = out.split()[1]
    code, out = _uro("provider", "add", "relink", "--provider", "openai", "--credential", first)
    conn = out.split()[1]
    try:
        code, out = _uro("provider", "credential", "rotate", first, "--api-key", "sk-a2")
        assert code == 0 and f"used by {conn}" in out and "sk-a2" not in out
        code, out = _uro("provider", "link", conn, second)
        assert code == 0
        assert f"{conn}  relink → openai auth={second}" in _uro("provider", "list")[1]
        assert _uro("provider", "unlink", conn)[0] == 0
        assert f"{conn}  relink → openai (keyless)" in _uro("provider", "list")[1]
        assert _uro("provider", "link", conn, "nope")[0] == 1
        assert _uro("provider", "credential", "rotate", "nope", "--api-key", "x")[0] == 1
    finally:
        _uro("provider", "rm", conn)
        _uro("provider", "credential", "rm", first)
        _uro("provider", "credential", "rm", second)
