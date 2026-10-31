"""Plugin rulesets (D-65): an installed package declares an entry point in the `uro.rulesets` group,
and the registry discovers it — checked against the port's contract, never silently dropped, never
replacing a built-in. The real example plugin (examples/plugins/uro-coinflip) plays end to end.
"""

import sys
from collections.abc import AsyncIterator, Callable
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets import registry
from uro_core.rulesets.base import CharSpec
from uro_core.rulesets.rng import Rng

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "uro-coinflip"
COINFLIP = EntryPoint(name="coinflip", value="uro_coinflip:CoinFlip", group="uro.rulesets")


class _EP:
    """An entry point whose load() returns `obj` (or raises) — for the contract failures."""

    def __init__(self, name: str, loader: Callable[[], Any]) -> None:
        self.name, self.value, self._loader = name, f"test:{name}", loader

    def load(self) -> Any:
        return self._loader()


@pytest.fixture
def plugins(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Install these entry points, as if their packages were pip-installed; restored afterwards."""
    monkeypatch.syspath_prepend(str(PLUGIN_DIR))
    monkeypatch.setattr(registry, "_FACTORIES", dict(registry._BUILTINS))
    monkeypatch.setattr(registry, "_BROKEN", {})
    monkeypatch.setattr(registry, "_discovered", False)

    def install(*eps: Any) -> None:
        monkeypatch.setattr(registry, "entry_points", lambda group: list(eps))

    return install


def test_an_installed_plugin_is_discovered_and_takes_settings(plugins) -> None:  # type: ignore[no-untyped-def]
    plugins(COINFLIP)
    assert "coinflip" in registry.available() and registry.broken() == {}
    two = registry.resolve("coinflip", "1", {"coins": 2})
    assert two.config.coins == 2  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="coinflip"):
        registry.resolve("coinflip", "", {"coin": 2})  # its own validation, a typo refused
    sheet = two.new_character(CharSpec(), Rng(0))
    from uro_core.rulesets.base import CheckRequest

    assert two.resolve_check(CheckRequest(sheet=sheet), Rng(1)).outcome in ("heads", "tails")


def test_a_plugin_that_breaks_the_contract_is_reported_not_dropped(plugins) -> None:  # type: ignore[no-untyped-def]
    class _Old:
        id, version, api_version = "old", "1", 0

    class _Named:
        id, version, api_version = "something-else", "1", 1

    def _boom() -> Any:
        raise ImportError("no module named uro_missing")

    plugins(
        _EP("uro-basic", lambda: lambda config: None),  # claims a built-in's id
        _EP("old", lambda: lambda config: _Old()),  # built for another port version
        _EP("named", lambda: lambda config: _Named()),  # id ≠ entry-point name
        _EP("missing", _boom),  # fails to import
    )
    unusable = registry.broken()
    assert set(unusable) == {"uro-basic [test:uro-basic]", "old", "named", "missing"}
    assert "can't replace the built-in" in unusable["uro-basic [test:uro-basic]"]
    assert "port v0" in unusable["old"] and "v1" in unusable["old"]
    assert "'something-else'" in unusable["named"]
    assert "ImportError" in unusable["missing"]
    assert registry.resolve("uro-basic").__class__.__name__ == "UroBasic"  # untouched
    for rid in ("old", "named", "missing"):
        assert rid not in registry.available()
        with pytest.raises(ValueError, match="can't be used"):
            registry.resolve(rid)


def test_a_plugin_missing_a_port_method_is_refused(plugins) -> None:  # type: ignore[no-untyped-def]
    sys.path.insert(0, str(PLUGIN_DIR))
    from uro_coinflip import CoinFlip

    class _NoDowntime(CoinFlip):
        downtime = None  # type: ignore[assignment]

    plugins(_EP("coinflip", lambda: _NoDowntime))
    assert "missing port methods: downtime" in registry.broken()["coinflip"]


def test_uro_ruleset_list_shows_plugins_and_why_one_is_unusable(plugins) -> None:  # type: ignore[no-untyped-def]
    from uro_cli.main import app

    plugins(COINFLIP, _EP("old", lambda: lambda config: type("O", (), {"id": "old"})()))
    out = CliRunner().invoke(app, ["ruleset", "list"]).output
    assert "coinflip@1" in out and "coins" in out
    assert "old  UNUSABLE:" in out


class _Stub:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "The coin spins."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def test_a_world_plays_a_plugin_ruleset_end_to_end(  # type: ignore[no-untyped-def]
    plugins, store: PostgresEventStore
) -> None:
    from uro_core.domain.events import actor_created, sheet_updated

    plugins(COINFLIP)
    world = await store.create_world(
        f"Coins-{new_id()}", ruleset_id="coinflip", ruleset_config={"coins": 3}
    )
    branch = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        branch,
        participant_id="p1",
        new_pc_name="Ash",
        new_pc_id=f"a:ash-{new_id()}",
        pc_sheet={"wins": 0},
        ruleset_id="coinflip",
    )
    pc = await store.campaign_pc(campaign.campaign_id)
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:mark", name="Mark"),
            sheet_updated(actor_id="a:mark", sheet={"wins": 0}, ruleset_id="coinflip"),
        ],
    )
    ruleset = await registry.resolve_for_campaign(store, campaign)
    assert ruleset.config.coins == 3  # type: ignore[attr-defined]  # the world's settings
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=ruleset)
    gamble = BeatPlan(mechanics=[PlanMechanic(affordance="gamble")])
    result = await engine.run_beat(campaign, "p1", "I gamble", plan=gamble)
    assert result.checks == 1 and "3 coin(s)" in result.check_traces[0]

    fight = BeatPlan(
        intent_class="action",
        triggers=["violence"],
        mechanics=[PlanMechanic(affordance="swing", actor=pc, target="a:mark")],
    )
    fought = await engine.run_beat(campaign, "p1", "I swing at Mark", plan=fight)
    async with store.pool.acquire() as conn:
        types = [
            r["event_type"]
            for r in await conn.fetch(
                "SELECT event_type FROM events WHERE commit_id = $1 ORDER BY seq",
                fought.commit_id,
            )
        ]
    assert "EncounterStarted" in types and "EncounterEnded" in types
    wins = (await store.get_sheet(branch, pc))["wins"]  # type: ignore[arg-type,index]
    mark = await store.get_sheet(branch, "a:mark")
    assert wins in (0, 1) and mark == {"wins": 0}  # the PC records a win only if it stood


def test_two_plugins_claiming_one_id_are_both_refused(plugins) -> None:  # type: ignore[no-untyped-def]
    # Review: the last one installed silently won.
    plugins(COINFLIP, EntryPoint(name="coinflip", value="other_pkg:Coin", group="uro.rulesets"))
    assert "coinflip" not in registry.available()
    assert "more than one plugin" in registry.broken()["coinflip"]
    with pytest.raises(ValueError, match="can't be used"):
        registry.resolve("coinflip")


def test_a_plugin_with_a_required_setting_is_usable(plugins) -> None:  # type: ignore[no-untyped-def]
    # Review: discovery built every plugin with empty settings, so a required one made it unusable.
    sys.path.insert(0, str(PLUGIN_DIR))
    from pydantic import BaseModel, ConfigDict
    from uro_coinflip import CoinFlip

    class _Cfg(BaseModel):
        model_config = ConfigDict(extra="forbid")
        deck: str  # required: no default

    class _Tarot(CoinFlip):
        id = "tarot"

        def __init__(self, config: dict | None = None) -> None:  # type: ignore[type-arg]
            self.deck = _Cfg.model_validate(config or {}).deck
            super().__init__({})

    plugins(_EP("tarot", lambda: _Tarot))
    assert "tarot" in registry.available() and registry.broken() == {}
    assert registry.resolve("tarot", "", {"deck": "major"}).deck == "major"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="deck"):
        registry.resolve("tarot")
    out = CliRunner().invoke(__import__("uro_cli.main", fromlist=["app"]).app, ["ruleset", "list"])
    assert "tarot  (can't list with default settings" in out.output  # listed, not a crash


def test_any_factory_error_is_a_clean_error_naming_the_ruleset(plugins) -> None:  # type: ignore[no-untyped-def]
    from uro_core.worldpack.create import ruleset_problem
    from uro_core.worldpack.parse import parse_pack

    sys.path.insert(0, str(PLUGIN_DIR))
    from uro_coinflip import CoinFlip

    class _Picky(CoinFlip):
        id = "picky"

        def __init__(self, config: dict | None = None) -> None:  # type: ignore[type-arg]
            int((config or {}).get("n"))  # type: ignore[arg-type]  # TypeError on None
            super().__init__({})

    plugins(_EP("picky", lambda: _Picky))
    with pytest.raises(ValueError, match=r"'picky' failed to build \(TypeError") as excinfo:
        registry.resolve("picky")
    assert "int()" not in str(excinfo.value)  # the exception's own text stays in the server log
    pack = parse_pack(Path(__file__).resolve().parents[3] / "worlds" / "brinehold")
    pack.manifest.ruleset.id = "picky"
    assert "TypeError" in (ruleset_problem(pack) or "")  # reported by validate, not a crash


def test_an_old_embedder_ruleset_fails_up_front(plugins) -> None:  # type: ignore[no-untyped-def]
    # Review (critic): a pre-D-62 zero-arg register() factory and a pre-D-63 ruleset handed straight
    # to the Engine both failed late and raw. Now: a clean error at resolve / at construction.
    from uro_core.rulesets.uro_pool import UroPool

    plugins()

    class _Mine(UroPool):
        id = "mine"

    registry.register("mine", lambda: _Mine())  # type: ignore[arg-type,misc]
    try:
        with pytest.raises(ValueError, match=r"'mine' failed to build \(TypeError"):
            registry.resolve("mine")
    finally:
        registry._FACTORIES.pop("mine", None)

    class _PreD63(UroPool):
        conclude_encounter = None  # type: ignore[assignment]

    with pytest.raises(ValueError, match="missing port methods: conclude_encounter"):
        Engine(None, ProviderRouter(bindings={}, default=_Stub()), ruleset=_PreD63())  # type: ignore[arg-type]
