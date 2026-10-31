"""Entry point for the `uro` command. Command surface per docs/08-api-and-sessions.md.

Phase 0 subset: version, db migrate, world new, play.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

import typer
from dotenv import find_dotenv, load_dotenv
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.pipeline.engine import Engine
from uro_core.ports.model_registry import ROLES
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets import registry
from uro_core.rulesets.base import CharSpec, Ruleset
from uro_core.rulesets.rng import Rng
from uro_core.timeline.models import STARTING_GEAR, World

from uro_cli.wiring import (
    build_router,
    build_router_from_registry,
    build_ruleset,
    build_store,
    connect_store,
    parse_role_models,
)

# Load a local `.env` (found from the CURRENT WORKING DIR, walking up) into the environment before
# any command reads it — so `URO_SECRET_KEY` / provider keys / `URO_DATABASE_URL` need only live in
# `.env`, not be exported by hand. `usecwd=True` so it finds the `.env` where you RUN `uro`, not the
# installed package dir. Precedence preserved: an exported env var wins (dotenv never overrides). No
# `.env` → find_dotenv returns "" → a harmless no-op.
load_dotenv(find_dotenv(usecwd=True))

app = typer.Typer(no_args_is_help=True, help="Uro Engine — reference client.")
db_app = typer.Typer(no_args_is_help=True, help="Database management.")
world_app = typer.Typer(no_args_is_help=True, help="World and campaign management.")
branch_app = typer.Typer(no_args_is_help=True, help="Branch and timeline management (docs/03).")
campaign_app = typer.Typer(no_args_is_help=True, help="Campaign lifecycle over branches.")
codex_app = typer.Typer(
    no_args_is_help=True, help="Player codex — out-of-world notes that survive a fork (docs/18 B8)."
)
token_app = typer.Typer(
    no_args_is_help=True, help="Runtime session tokens against a running server (docs/18 B10)."
)
provider_app = typer.Typer(
    no_args_is_help=True,
    help="Model connections — the DB-backed LLM provider registry (D-47, docs/20).",
)
credential_app = typer.Typer(
    no_args_is_help=True,
    help="Stored credentials — the encrypted API keys / OAuth logins that connections link to.",
)
outbox_app = typer.Typer(
    no_args_is_help=True,
    help="The transactional outbox — publish committed beats to other processes (D-57).",
)
ruleset_app = typer.Typer(no_args_is_help=True, help="The built-in ruleset registry (docs/06).")
policy_app = typer.Typer(
    no_args_is_help=True,
    help="Which emergent world state play may create — the instance's extraction policy (D-49).",
)
app.add_typer(db_app, name="db")
app.add_typer(world_app, name="world")
app.add_typer(branch_app, name="branch")
app.add_typer(campaign_app, name="campaign")
app.add_typer(codex_app, name="codex")
app.add_typer(token_app, name="token")
app.add_typer(provider_app, name="provider")
provider_app.add_typer(credential_app, name="credential")
app.add_typer(outbox_app, name="outbox")
app.add_typer(ruleset_app, name="ruleset")
app.add_typer(policy_app, name="extraction-policy")

# The engine roles a connection can back (resolved by the ProviderRouter) — shared with the server.
_ROLES = tuple(sorted(ROLES))

PARTICIPANT = "player-1"  # Phase 0 is single-player; participants arrive in Phase 5.


def _run_async(coro_factory) -> None:  # type: ignore[no-untyped-def]
    """Run an async command, turning config/credential/lookup errors into a clean message."""
    try:
        asyncio.run(coro_factory())
    except (RuntimeError, ValueError, KeyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _resolve_router(store: PostgresEventStore, provider: str, model: str | None):  # type: ignore[no-untyped-def]
    """Resolve the ProviderRouter for `serve`: the DB model-connection registry (D-47) wins if it
    has usable bindings, else the `--provider`/`uro.toml` seed. This first DB touch also serves as
    the preflight. Returns (router, source_label). Raises DatabaseUnavailable if the DB is down and
    RuntimeError/ValueError if a CONFIGURED registry can't be built (bad KEK, missing key)."""

    async def _read() -> ProviderRouter | None:
        await connect_store(store)
        try:
            return await build_router_from_registry(store)
        finally:
            await store.close()

    registry_router = asyncio.run(_read())
    if registry_router is not None:
        return registry_router, "DB model-connection registry (D-47)"
    return build_router(provider, model), f"seed (provider={provider})"


async def _world_or_exit(store: PostgresEventStore, ident: str) -> World:
    """Resolve a world by id or name, or exit 1 with a clean message. World names aren't unique
    (importing a pack twice makes two): an ambiguous name is refused with the ids to choose from,
    never resolved to an arbitrary one."""
    world = await store.get_world(ident)
    if world is not None:
        return world
    named = [w for w in await store.list_worlds() if w.name == ident]
    if len(named) > 1:
        ids = ", ".join(w.world_id for w in named)
        typer.echo(f"{len(named)} worlds are named {ident!r} — give an id: {ids}", err=True)
        raise typer.Exit(1)
    if not named:
        typer.echo(f"no such world: {ident}", err=True)
        raise typer.Exit(1)
    return named[0]


def _build_pc_sheet(ruleset: Ruleset) -> dict[str, Any]:
    """A default character sheet from the world's bound ruleset (with the world's settings), so
    every PC can be checked (docs/06) — a PbtA world mints a PbtA sheet."""
    return ruleset.new_character(CharSpec(), Rng(0))


@app.callback()
def main() -> None:
    """Uro Engine — play, dry-run, and dev tools against the engine."""


@app.command()
def version() -> None:
    """Print engine and client versions."""
    import uro_core

    import uro_cli

    typer.echo(f"uro-cli {uro_cli.__version__} / uro-core {uro_core.__version__}")


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending database migrations."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            applied = await store.migrate()
        finally:
            await store.close()
        if applied:
            typer.echo(f"applied: {', '.join(applied)}")
        else:
            typer.echo("already up to date")

    _run_async(_run)


@world_app.command("new")
def world_new(
    name: str,
    ruleset: str = typer.Option(
        "", "--ruleset", help="a built-in ruleset id (`uro ruleset list`); default uro-basic"
    ),
) -> None:
    """Create a world (+ its main branch) and a ready-to-play campaign with a default,
    sheeted PC. Prints the campaign id."""

    async def _run() -> None:
        rs = build_ruleset(ruleset)  # an unknown id fails here, before any write
        sheet, ruleset_id = _build_pc_sheet(rs), rs.id
        store = build_store()
        await connect_store(store)
        try:
            world = await store.create_world(name, ruleset_id=ruleset)
            campaign = await store.start_campaign(
                world.world_id,
                world.main_branch_id,
                participant_id=PARTICIPANT,
                new_pc_name="Adventurer",
                pc_sheet=sheet,
                starting_items=list(STARTING_GEAR),
                ruleset_id=ruleset_id,
            )
        finally:
            await store.close()
        typer.echo(f"world:    {world.world_id}  ({name})")
        typer.echo(f"campaign: {campaign.campaign_id}  (PC: Adventurer)")
        typer.echo(f"\nplay it:  uro play {campaign.campaign_id}")

    _run_async(_run)


@world_app.command("validate")
def world_validate(path: str) -> None:
    """Parse a world pack and report its sufficiency (docs/09) — the creator loop, no import."""
    from uro_core.errors import PackError
    from uro_core.worldpack.create import ruleset_problem
    from uro_core.worldpack.parse import parse_pack
    from uro_core.worldpack.sufficiency import check_sufficiency

    try:
        pack = parse_pack(path)
    except PackError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    report = check_sufficiency(pack)
    typer.echo(
        f"world: {pack.manifest.name}  "
        f"({len(pack.places)} places, {len(pack.actors)} actors, "
        f"{len(pack.factions)} factions, {len(pack.threads)} conflict seeds)"
    )
    typer.echo(f"grade: {report.grade.upper()}")
    for d in report.dimensions:
        typer.echo(f"  {'ok ' if d.ok else 'GAP'} {d.name:<10} {d.detail}")
    # Catch a bad ruleset id or settings at VALIDATE, not at `campaign new` (phase-6 review, D-62).
    problem = ruleset_problem(pack)
    rid = pack.manifest.ruleset.id
    typer.echo(f"  ok  ruleset    {rid}" if problem is None else f"  GAP ruleset    {problem}")
    if report.grade != "runnable":
        typer.echo("\ngaps to fix (or run backfill):")
        for g in report.gaps:
            typer.echo(f"  - {g}")


@world_app.command("create")
def world_create(
    path: str,
    backfill: bool = typer.Option(
        False, "--backfill", help="AI-fill gaps before import (committed, tagged ai_backfill)"
    ),
    provider: str = typer.Option("openai", help="provider for --backfill"),
    model: str = typer.Option(None, help="model id for the worldsmith role (with --backfill)"),
) -> None:
    """Import a world pack (docs/09): validate, then commit the authored (and, with --backfill,
    AI-filled) seeds as a new world."""

    async def _run() -> None:
        from uro_core.errors import PackError
        from uro_core.worldpack.create import import_pack
        from uro_core.worldpack.parse import parse_pack
        from uro_core.worldpack.sufficiency import check_sufficiency

        store = build_store()
        try:
            pack = parse_pack(path)
            # build the provider only when a gap needs it: a runnable pack needs no key
            needs_fill = backfill and check_sufficiency(pack).grade != "runnable"
            router = build_router(provider, model) if needs_fill else None
            await connect_store(store)
            result = await import_pack(store, pack, backfill_router=router)
        except PackError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        finally:
            await store.close()
        world = result.world
        typer.echo(f"world: {world.world_id}  ({world.name}, grade {result.grade})")
        for a in result.backfilled:
            typer.echo(f"  backfilled + committed: {a}")
        typer.echo(f"seed history:  uro world seed {path} --seed 42")

    _run_async(_run)


@world_app.command("seed")
def world_seed(
    path: str,
    seed: int = typer.Option(42, "--seed", help="RNG seed for History"),
    world: str = typer.Option(
        None, "--world", help="the world to seed (id or name; default: the pack's name)"
    ),
) -> None:
    """Run History seeding on the world imported from <path>: layer seed-dependent dynasties and
    wars on top of the authored geography (docs/09). Same pack + a different seed → a different
    history on identical geography."""

    async def _run() -> None:
        from uro_core.errors import PackError
        from uro_core.worldpack.create import seed_world
        from uro_core.worldpack.parse import parse_pack

        store = build_store()
        try:
            pack = parse_pack(path)
            await connect_store(store)
            target = await _world_or_exit(store, world or pack.manifest.name)
            result = await seed_world(store, target, pack, seed)
        except PackError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        finally:
            await store.close()
        typer.echo(f"seeded {target.name!r} with seed {seed} → commit {result.commit_id}")
        typer.echo(
            f"  {result.dynasties} dynasties, {result.wars} wars (on the pack's authored geography)"
        )

    _run_async(_run)


@world_app.command("backfill")
def world_backfill(
    path: str,
    provider: str = typer.Option("openai", help="stub | local | openai | anthropic"),
    model: str = typer.Option(None, help="model id for the worldsmith role"),
) -> None:
    """Offer to fill a thin pack's gaps with AI-generated, provenance-tagged seeds (docs/09).
    Opt-in; prints what WOULD be added (does not rewrite the pack)."""

    async def _run() -> None:
        from uro_core.errors import PackError
        from uro_core.worldpack.backfill import backfill_gaps
        from uro_core.worldpack.parse import parse_pack
        from uro_core.worldpack.sufficiency import check_sufficiency

        try:
            pack = parse_pack(path)
        except PackError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        before = check_sufficiency(pack)
        if before.grade == "runnable":
            typer.echo("pack is already runnable — nothing to backfill")
            return
        augmented, added = await backfill_gaps(pack, build_router(provider, model), report=before)
        after = check_sufficiency(augmented)
        typer.echo(f"backfill: {before.grade} → {after.grade}")
        for a in added:
            typer.echo(f"  + {a}")
        if not added:
            typer.echo("  (model produced nothing usable — gaps remain)")

    _run_async(_run)


@world_app.command("probe")
def world_probe(
    path: str,
    provider: str = typer.Option("openai", help="stub | local | openai | anthropic"),
    model: str = typer.Option(None, help="model id for the bound roles"),
    tries: int = typer.Option(3, help="structured-output attempts"),
) -> None:
    """Probe whether the bound models can deliver what the world declares (docs/04) — a
    compatibility report, not enforcement."""

    async def _run() -> None:
        from uro_core.engines.probe import run_probes
        from uro_core.errors import PackError
        from uro_core.worldpack.parse import parse_pack

        try:
            pack = parse_pack(path)
        except PackError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        report = await run_probes(pack.manifest, build_router(provider, model), tries=tries)
        typer.echo(f"probe report for {report.world}: {'OK' if report.ok else 'ISSUES'}")
        for r in report.results:
            gate = f"  (gates {r.gate_for})" if r.gate_for else ""
            typer.echo(f"  [{r.status.upper():4}] {r.name}: {r.detail}{gate}")

    _run_async(_run)


@world_app.command("export")
def world_export(
    world: str,
    out: str = typer.Option(..., "-o", "--out", help="output bundle path (e.g. ashfall.uwp)"),
) -> None:
    """Export a world's whole log to a portable, hash-chain-verified bundle (docs/08)."""

    async def _run() -> None:
        from pathlib import Path

        store = build_store()
        await connect_store(store)
        try:
            w = await _world_or_exit(store, world)
            bundle = await store.export_world(w.world_id)
        finally:
            await store.close()
        Path(out).write_text(bundle.model_dump_json(indent=2))
        typer.echo(f"exported {w.name!r}: {len(bundle.commits)} commits → {out}")

    _run_async(_run)


@world_app.command("import")
def world_import(path: str) -> None:
    """Import a world bundle (docs/08): verify its hash chain, then instantiate it as a fresh
    world. A tampered bundle is rejected before anything is written."""

    async def _run() -> None:
        from pathlib import Path

        from uro_core.errors import ExportError
        from uro_core.export import WorldBundle

        bundle = WorldBundle.model_validate_json(Path(path).read_text())
        store = build_store()
        await connect_store(store)
        try:
            world = await store.import_world(bundle)
        except ExportError as exc:
            typer.echo(f"error: bundle failed verification — {exc}", err=True)
            raise typer.Exit(1) from exc
        finally:
            await store.close()
        typer.echo(f"imported {world.name!r} (chain verified) → world {world.world_id}")
        typer.echo(f"continue it:  uro campaign new {world.name} --branch main")

    _run_async(_run)


@app.command()
def play(
    campaign_id: str,
    provider: str = typer.Option("stub", help="stub | local | openai | anthropic"),
    model: str = typer.Option(None, help="model id for local/openai/anthropic providers"),
    bare: bool = typer.Option(
        False,
        help="ablation (T1): raw-transcript GM, no state/recall/extraction. Use a FRESH "
        "campaign — mixing bare and full beats on one corrupts the A/B comparison.",
    ),
    no_mechanics: bool = typer.Option(
        False,
        "--no-mechanics",
        help="narrative-only: full recall + extraction + memory, but NO ruleset/planner "
        "(the thesis test — recall without the mechanics confound). Distinct from --bare.",
    ),
    role_model: list[str] = typer.Option(  # noqa: B008 (typer DI-style default, like every option here)
        None,
        "--role-model",
        help="per-role model override, repeatable: role=spec (spec = 'openai:gpt-4o' or a bare "
        "'gpt-4o' on the default provider). Wins over uro.toml. E.g. --role-model planner=gpt-4o "
        "keeps a cheap default for extraction but a strong planner (docs/04).",
    ),
) -> None:
    """Interactive play loop. Type an action; '/quit' to leave. State persists to Postgres."""
    role_models = parse_role_models(role_model)

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                typer.echo(f"no such campaign: {campaign_id}", err=True)
                raise typer.Exit(1)
            # No ruleset in bare (ablation) OR --no-mechanics (narrative-only) mode — the
            # planner/gate are exactly what bare ablates and what no-mechanics skips.
            engine = Engine(
                store,
                build_router(provider, model, role_models),
                # Rebind the campaign's OWN ruleset (D-30) — a PbtA campaign plays under uro_pbta.
                ruleset=None
                if (bare or no_mechanics)
                else await registry.resolve_for_campaign(store, campaign),
                bare=bare,
            )

            history = await store.recent_beats(campaign.branch_id, 3)
            if history:
                typer.echo("— resuming; recent beats —")
                for beat in history:
                    typer.echo(f"  > {beat.intent_text}")
                    typer.echo(f"    {beat.narration}")
                typer.echo("—")

            while True:
                try:
                    intent = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    typer.echo("")
                    break
                if intent in ("/quit", "/exit"):
                    break
                if not intent:
                    continue
                try:
                    async for chunk in engine.run_beat_stream(campaign, PARTICIPANT, intent):
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    sys.stdout.write("\n")
                except Exception as exc:
                    sys.stdout.write("\n")
                    typer.echo(f"beat failed ({exc}); nothing was saved — try again.", err=True)
        finally:
            await store.close()

    _run_async(_run)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="bind address"),
    port: int = typer.Option(8000, help="bind port"),
    token: list[str] = typer.Option(  # noqa: B008 (typer DI-style default, like every option here)
        None,
        "--token",
        help="bearer token → participant (repeatable). `TOK=participant` names it explicitly (e.g. "
        "--token tok-a=alice); a bare `TOK` maps positionally to player-1..N (docs/08, gap G-16)",
    ),
    admin_token: list[str] = typer.Option(  # noqa: B008
        None,
        "--admin-token",
        help="OPERATOR token TOK[=name] (repeatable, D-39): may seat/mint/revoke for ANOTHER "
        "participant (e.g. a GM seating a party). A plain --token peer may act only for itself.",
    ),
    provider: str = typer.Option("stub", help="stub | local | openai | anthropic"),
    model: str = typer.Option(None, help="model id for the provider"),
    ruleset: str = typer.Option(
        "",
        help="the default ruleset, for campaigns that pin none (default uro-basic); every pinned "
        "campaign plays under its own",
    ),
    arbiter_kind: str = typer.Option(
        None,
        "--arbiter",
        help="turn shape (D-38): party (round-robin) | proposal (propose-then-act) | vote "
        "(consensus). UNSET = auto (solo for 1 token, party for >1). Setting it EXPLICITLY binds "
        "that shape even for a 1-token launch, so a runtime-added player still gets turns (D-39).",
    ),
    cors_origin: list[str] = typer.Option(  # noqa: B008
        None,
        "--cors-origin",
        help="allow a browser origin to call this server, e.g. --cors-origin http://localhost:5173 "
        "(repeatable). REQUIRED for the uro-loom web console (a cross-origin SPA); the browser "
        "blocks calls with no CORS header. Pass '*' to allow any origin (dev only; disables "
        "credentials). Omit for CLI/embedded use.",
    ),
) -> None:
    """Run the Uro server (docs/08): a thin FastAPI shell over the engine. Each --token maps to
    a participant; with no --token, a single 'local' token binds player-1 (the solo dev loop).

    Each campaign plays under the ruleset it pinned, as with `uro play`: one server hosts d20,
    PbtA and Uro Pool campaigns side by side. `--ruleset` sets the default for unpinned ones."""
    import uvicorn
    from uro_core.pipeline.engine import Engine
    from uro_core.session import PartyArbiter, ProposalWindowArbiter, TurnArbiter, VoteArbiter
    from uro_server.app import create_app, engine_deps

    toks = token or ["local"]
    # `TOK=participant` names the participant explicitly (gap G-16 — token order was load-bearing
    # config); a bare `TOK` falls back to positional player-1..N.
    tokens: dict[str, str] = {}
    for i, t in enumerate(toks):
        tok, sep, name = t.partition("=")
        tokens[tok] = name if sep else f"player-{i + 1}"
    # Operator tokens (D-39): resolvable participants like any --token, PLUS flagged admin so they
    # may act for others. A plain --token peer is NOT admin (else any launch peer could impersonate
    # any other — D-39 review). None given → no operator; a new runtime player is seated by an
    # operator or via the direct-DB `uro campaign join`.
    admin_tokens: set[str] = set()
    for j, t in enumerate(admin_token or []):
        tok, sep, name = t.partition("=")
        tokens[tok] = name if sep else f"operator-{j + 1}"
        admin_tokens.add(tok)
    store = build_store()
    # Resolve the ProviderRouter: the DB model-connection registry (D-47) wins if it has usable
    # bindings, else the --provider/uro.toml seed. This first DB touch doubles as the preflight (a
    # down DB → the clean docker-first hint, not a uvicorn startup traceback). A CONFIGURED-but-
    # broken registry (bad KEK, missing key) fails LOUDLY rather than silently serving the stub.
    try:
        router, llm_source = _resolve_router(store, provider, model)
    except (RuntimeError, ValueError) as exc:  # incl. DatabaseUnavailable (a RuntimeError subclass)
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    engine = Engine(store, router, ruleset=build_ruleset(ruleset))
    # Pick the turn shape (OQ-7, D-31/D-38/D-39). UNSET (--arbiter not given) is AUTO: solo for one
    # token (the dev loop → SoloArbiter), party for >1. Set EXPLICITLY, the shape binds regardless
    # of launch token count, so a player minted+joined at RUNTIME still gets real turns (D-39)
    # instead of SoloArbiter's always-ADMITTED. Seat each participant's PC via `uro campaign join`.
    shapes: dict[str, type[TurnArbiter]] = {
        "party": PartyArbiter,
        "proposal": ProposalWindowArbiter,
        "vote": VoteArbiter,
    }
    if arbiter_kind is None:
        arbiter: TurnArbiter | None = PartyArbiter() if len(tokens) > 1 else None
    elif arbiter_kind not in shapes:
        raise typer.BadParameter(
            f"--arbiter must be one of {', '.join(shapes)} (got {arbiter_kind!r})"
        )
    else:
        arbiter = shapes[arbiter_kind]()
    deps = engine_deps(store, engine, tokens, admin_tokens, router=router)
    fastapi_app = create_app(deps, arbiter=arbiter, cors_origins=cors_origin or None)

    @fastapi_app.on_event("startup")
    async def _startup() -> None:
        await connect_store(store)
        if deps.hydrate_tokens is not None:  # load durable runtime tokens (D-39) — AFTER connect
            await deps.hydrate_tokens()

    @fastapi_app.on_event("shutdown")
    async def _shutdown() -> None:
        await store.close()

    # (The DB preflight is folded into _resolve_router above — its connect_store gives the same
    # clean docker-first hint on a down DB, and the pool it opens is closed before uvicorn starts;
    # the startup hook re-opens one on uvicorn's own loop.)
    shape = type(arbiter).__name__ if arbiter is not None else "solo"
    typer.echo(
        f"uro server on ws://{host}:{port}  "
        f"({len(tokens)} token(s), provider={provider}, arbiter={shape})"
    )
    typer.echo(f"  LLM: {llm_source}")
    for t, p in tokens.items():
        typer.echo(f"  token {t!r} → {p}")
    if cors_origin:
        typer.echo(f"  CORS: allowing {', '.join(cors_origin)}")
    uvicorn.run(fastapi_app, host=host, port=port, log_level="warning")


@app.command()
def connect(
    campaign_id: str,
    server: str = typer.Option("http://127.0.0.1:8000", help="server base URL"),
    token: str = typer.Option("local", help="bearer token (maps to a participant server-side)"),
) -> None:
    """HTTP-client play mode (docs/08): connect to a running `uro serve` over WebSocket. Type an
    action to take a beat; '/say <text>' for out-of-world table-talk and '/vote <choice>' to vote
    (D-38, both non-canon — they never commit); '/quit' to leave. Beats from every participant on
    the campaign stream in live."""
    import asyncio
    import json

    import websockets

    ws_base = server.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    uri = f"{ws_base}/campaigns/{campaign_id}/play?token={token}"

    async def _client() -> None:
        try:
            async with websockets.connect(uri) as ws:
                typer.echo(f"connected to {campaign_id} as {token!r}. Type an action; /quit.\n")

                async def receive() -> None:
                    async for raw in ws:
                        msg = json.loads(raw)
                        kind = msg.get("type")
                        if kind == "narration_chunk":
                            typer.echo(msg["text"], nl=False)
                        elif kind == "beat_committed":
                            typer.echo(f"\n  ✓ [{msg['participant_id']}] {msg['intent']!r}\n")
                        elif kind == "not_your_turn":
                            typer.echo("  · not your turn — another player holds it (round-robin)")
                        elif kind == "proposal_opened":
                            typer.echo(
                                f"  · [{msg['participant_id']}] proposes: {msg['text']!r} "
                                "— the turn-holder can enact it"
                            )
                        elif kind == "table_talk":
                            typer.echo(f"  · [{msg['participant_id']}] says: {msg['text']}")
                        elif kind == "vote_tally":
                            typer.echo(
                                f"  · [{msg['participant_id']}] voted {msg['choice']!r} "
                                f"— tally {msg['tally']}"
                            )
                        elif kind == "vote_decided":
                            typer.echo(
                                f"  ✓ vote decided: {msg['choice']!r} — enact it on your turn"
                            )
                        elif kind == "vote_unsupported":
                            typer.echo(
                                "  · voting isn't enabled here (server needs --arbiter vote)"
                            )
                        elif kind == "beat_failed":
                            typer.echo(f"\n  ✗ beat failed: {msg['error']}")
                        elif kind in ("participant_joined", "participant_left"):
                            typer.echo(f"  · {msg['participant_id']} {kind.split('_')[1]}")

                receiver = asyncio.create_task(receive())
                loop = asyncio.get_event_loop()
                while True:
                    intent = (await loop.run_in_executor(None, input, "> ")).strip()
                    if intent in ("/quit", "/exit"):
                        break
                    if intent.startswith("/say "):  # non-canon table-talk (D-38)
                        await ws.send(json.dumps({"type": "table_talk", "text": intent[5:]}))
                    elif intent.startswith("/vote "):  # non-canon consensus vote (D-38)
                        await ws.send(json.dumps({"type": "vote", "choice": intent[6:]}))
                    elif intent:
                        await ws.send(json.dumps({"type": "intent", "text": intent}))
                receiver.cancel()
        except OSError as exc:
            typer.echo(f"could not connect to {server}: {exc} (is `uro serve` running?)", err=True)
            raise typer.Exit(1) from exc

    asyncio.run(_client())


def _server_post(server: str, path: str, token: str, body: dict[str, Any] | None) -> dict[str, Any]:
    """POST JSON (or GET, when `body` is None) to a running `uro serve` — stdlib urllib, no extra
    dep — authed by ?token."""
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"{server.rstrip('/')}{path}?token={urllib.parse.quote(token)}"
    if body is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers={"content-type": "application/json"}
        )
    try:
        with urllib.request.urlopen(req) as resp:
            return dict(json.loads(resp.read()))
    except urllib.error.HTTPError as exc:
        typer.echo(f"server said {exc.code}: {exc.read().decode()[:200]}", err=True)
        raise typer.Exit(1) from exc
    except OSError as exc:
        typer.echo(f"could not reach {server}: {exc} (is `uro serve` running?)", err=True)
        raise typer.Exit(1) from exc


@outbox_app.command("status")
def outbox_status() -> None:
    """How many committed beats are waiting to be published."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            typer.echo(f"pending: {await store.outbox_pending()}")
        finally:
            await store.close()

    _run_async(_run)


@outbox_app.command("relay")
def outbox_relay(
    nats_url: str = typer.Option(..., "--nats", help="NATS server URL, e.g. nats://localhost:4222"),
    stream: str = typer.Option("URO", help="JetStream stream (created if missing)"),
    subject_prefix: str = typer.Option("uro", help="subjects are <prefix>.<world>.<branch>"),
    once: bool = typer.Option(False, "--once", help="drain what is pending, then exit"),
    interval: float = typer.Option(1.0, help="seconds between polls (without --once)"),
    batch: int = typer.Option(100, min=1, help="commits claimed per transaction"),
) -> None:
    """Publish committed beats to NATS JetStream (D-57). At-least-once from the outbox; JetStream
    drops a re-sent commit (Nats-Msg-Id = commit id). Run ONE relay to keep each branch in commit
    order (there is no global order across branches); several are safe — no row is published
    twice — but may interleave a branch. A publish error is retried with backoff (`--once` exits
    instead)."""
    try:
        from uro_core.adapters.nats import NatsJetStreamPublisher
    except ImportError as exc:
        typer.echo("the NATS relay needs `pip install 'uro-core[nats]'`", err=True)
        raise typer.Exit(1) from exc
    from uro_core.outbox import relay_forever, relay_outbox

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        publisher = NatsJetStreamPublisher(nats_url, stream=stream, subject_prefix=subject_prefix)
        await publisher.connect()
        try:
            if once:  # a one-shot drain: an error exits non-zero (right for scripts and cron)
                sent = await relay_outbox(store, publisher, limit=batch)
                typer.echo(f"published {sent} commit(s)")
                return
            # the daemon: a publish error is logged and retried with backoff, never fatal
            await relay_forever(
                store,
                publisher,
                interval=interval,
                limit=batch,
                on_published=lambda n: typer.echo(f"published {n} commit(s)"),
            )
        finally:
            await publisher.close()
            await store.close()

    _run_async(_run)


@token_app.command("mint")
def token_mint(
    campaign_id: str,
    participant: str = typer.Option(
        ..., help="participant to mint for (yourself, or anyone with an operator token)"
    ),
    server: str = typer.Option("http://127.0.0.1:8000", help="server base URL"),
    token: str = typer.Option(..., help="your bearer token (self-service) or an operator --token"),
) -> None:
    """Mint a durable session token for an already-seated participant (docs/18 B10, D-39). Self-or-
    operator scoped; the participant must have a PC on the campaign (`uro campaign join` first)."""
    out = _server_post(
        server, f"/campaigns/{campaign_id}/tokens", token, {"participant": participant}
    )
    typer.echo(out.get("token", out))


@token_app.command("revoke")
def token_revoke(
    campaign_id: str,
    target: str = typer.Option(None, "--target", help="the token to revoke"),
    participant: str = typer.Option(
        None, "--participant", help="revoke every live token this participant holds here"
    ),
    server: str = typer.Option("http://127.0.0.1:8000", help="server base URL"),
    token: str = typer.Option(..., help="your bearer token (own token) or an operator --token"),
) -> None:
    """Revoke a session token (docs/18 B10, D-39), or with --participant all of a participant's —
    so an operator can remove a player without holding their token. Self-or-operator scoped.
    Blocks a new connect, and a live socket is closed at its next message."""
    if (target is None) == (participant is None):
        typer.echo("give exactly one of --target <token> or --participant <id>", err=True)
        raise typer.Exit(1)
    body = {"token": target} if target is not None else {"participant": participant}
    out = _server_post(server, f"/campaigns/{campaign_id}/tokens/revoke", token, body)
    if participant is not None:
        typer.echo(f"revoked {out.get('count', 0)} token(s) for {participant}")
    else:
        typer.echo("revoked" if out.get("revoked") else "no live token matched (already revoked?)")


@token_app.command("list")
def token_list(
    campaign_id: str,
    server: str = typer.Option("http://127.0.0.1:8000", help="server base URL"),
    token: str = typer.Option(..., help="an operator --token (all) or your own token (yours)"),
) -> None:
    """A campaign's runtime tokens — who holds one, when it was minted, whether it's revoked. Never
    the token itself; `token_id` is a hash prefix."""
    out = _server_post(server, f"/campaigns/{campaign_id}/tokens", token, None)
    tokens = out.get("tokens", [])
    if not tokens:
        typer.echo("(no runtime tokens)")
    for t in tokens:
        state = "revoked" if t["revoked"] else "live"
        typer.echo(f"{t['token_id']}  {t['participant_id']:<16} {state:<8} {t['created_at']}")


@branch_app.command("list")
def branch_list(world: str) -> None:
    """List a world's branches (with head depth) and its markers."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            w = await _world_or_exit(store, world)
            branches = await store.list_branches(w.world_id)
            days = await store.current_world_time_batch([b.branch_id for b in branches])
            markers = await store.list_markers(w.world_id)
        finally:
            await store.close()
        typer.echo(f"world: {w.world_id}  ({w.name})")
        typer.echo("branches:")
        for b in branches:
            forked = f"  forked@{b.forked_from}" if b.forked_from else ""
            head = b.head_commit if b.head_commit else "-"
            day = days.get(b.branch_id, 0)
            typer.echo(f"  {b.name:<16} head={head} depth={b.head_depth} day={day}{forked}")
        if markers:
            typer.echo("markers:")
            for m in markers:
                typer.echo(f"  {m.name:<16} → {m.commit_id}")

    _run_async(_run)


@branch_app.command("fork")
def branch_fork(
    world: str,
    at: str = typer.Option(..., "--at", help="marker name or commit id to fork from"),
    name: str = typer.Option(..., "--name", help="name for the new branch"),
    time_skip_days: int = typer.Option(
        0, "--time-skip-days", help="advance in-fiction time on the fork (e.g. 365 = a year later)"
    ),
) -> None:
    """Fork a new branch from any commit or marker (docs/03: branch anywhere)."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            w = await _world_or_exit(store, world)
            branch = await store.fork_branch(w.world_id, at, name)
            if time_skip_days > 0:
                # agenda_tick time-skips AND fires the world's downtime agenda rules (docs/17); it
                # needs no LLM, so a stub router suffices. A rule-less world → a plain time-skip.
                ruleset = await registry.resolve_for_branch(store, branch.branch_id)
                engine = Engine(store, build_router("stub", None), ruleset=ruleset)
                await engine.agenda_tick(branch.branch_id, time_skip_days)
        finally:
            await store.close()
        forked = branch.forked_from if branch.forked_from else "-"
        typer.echo(f"forked branch {name!r}: {branch.branch_id}")
        typer.echo(f"  from {forked}  (head = {forked})")
        if time_skip_days > 0:
            typer.echo(f"  time-skipped {time_skip_days} day(s) on the fork")

    _run_async(_run)


@branch_app.command("mark")
def branch_mark(
    world: str,
    name: str,
    branch: str = typer.Option("main", "--branch", help="branch whose head to mark"),
) -> None:
    """Mark a branch's current head with a name (a fork root, docs/03)."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            w = await _world_or_exit(store, world)
            b = await store.get_branch_by_name(w.world_id, branch)
            if b is None:
                typer.echo(f"no such branch: {branch}", err=True)
                raise typer.Exit(1)
            marker = await store.create_marker(w.world_id, name, b.branch_id)
        finally:
            await store.close()
        typer.echo(f"marker {marker.name!r} → commit {marker.commit_id} on branch {branch!r}")

    _run_async(_run)


@app.command()
def log(
    world: str,
    branch: str = typer.Option("main", "--branch", help="branch to view (default main)"),
    limit: int = typer.Option(20, "--limit", help="max commits to show"),
) -> None:
    """Commit lineage for a branch, git-log style (docs/08). Per-branch — never a merge."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            w = await _world_or_exit(store, world)
            b = await store.get_branch_by_name(w.world_id, branch)
            if b is None:
                typer.echo(f"no such branch: {branch}", err=True)
                raise typer.Exit(1)
            entries = await store.lineage(b.branch_id, limit)
        finally:
            await store.close()
        typer.echo(f"world {w.name}  branch {branch!r}  (head depth {b.head_depth})")
        for e in entries:
            marks = f"  [{', '.join(e.markers)}]" if e.markers else ""
            typer.echo(f"  {e.depth:>4} {e.commit_id}  {e.summary}{marks}")

    _run_async(_run)


def _row(row: dict[str, Any]) -> str:
    """One projection/event row as a compact JSON line (dates and ids stringified)."""
    return json.dumps(row, default=str, ensure_ascii=False)


async def _branch_or_exit(store: PostgresEventStore, world: World, branch: str) -> Any:
    b = await store.get_branch_by_name(world.world_id, branch)
    if b is None:
        typer.echo(f"no such branch: {branch}", err=True)
        raise typer.Exit(1)
    return b


async def _campaign_or_exit(store: PostgresEventStore, campaign_id: str) -> Any:
    c = await store.get_campaign(campaign_id)
    if c is None:
        typer.echo(f"no such campaign: {campaign_id}", err=True)
        raise typer.Exit(1)
    return c


@world_app.command("list")
def world_list() -> None:
    """List every world — the names `uro log`/`uro branch …`/`uro campaign new` take."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            worlds = await store.list_worlds()
        finally:
            await store.close()
        if not worlds:
            typer.echo("(no worlds — create one with `uro world new` or `uro world create`)")
        for w in worlds:
            typer.echo(f"{w.world_id}  {w.name}")

    _run_async(_run)


@app.command()
def events(
    world: str,
    branch: str = typer.Option("main", "--branch", help="branch to read (default main)"),
    event_type: str = typer.Option(None, "--type", help="only this event type"),
    entity: str = typer.Option(None, "--entity", help="only events touching this entity ref"),
    caused_by: str = typer.Option(None, "--caused-by", help="only this cause kind"),
    limit: int = typer.Option(50, "--limit", min=0, help="max events"),
) -> None:
    """The raw event log along a branch, newest first (docs/12) — omniscient: claim truth values,
    hidden beliefs and sheets included (the operator view, D-45)."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            w = await _world_or_exit(store, world)
            b = await _branch_or_exit(store, w, branch)
            evs = await store.branch_events(
                b.branch_id,
                event_type=event_type,
                entity_ref=entity,
                caused_by=caused_by,
                limit=limit,
            )
        finally:
            await store.close()
        for e in evs:
            refs = f" [{', '.join(e.entity_refs)}]" if e.entity_refs else ""
            typer.echo(f"{e.event_type}{refs}  {_row(e.payload)}")

    _run_async(_run)


@app.command()
def show(
    world: str,
    ref: str = typer.Argument(
        ..., help="a commit id (as `uro log` prints it, or a unique prefix) or a marker"
    ),
) -> None:
    """One commit's ordered events + metadata (the operator view, D-45)."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            w = await _world_or_exit(store, world)
            commit_id = await store.resolve_ref(w.world_id, ref)
            detail = await store.commit_detail(w.world_id, commit_id)
        finally:
            await store.close()
        if detail is None:
            typer.echo(f"no such commit: {ref}", err=True)
            raise typer.Exit(1)
        parent = detail.parent_id or "(genesis)"
        typer.echo(f"commit {detail.commit_id}  depth {detail.depth}  parent {parent}")
        for e in detail.events:
            refs = f" [{', '.join(e.entity_refs)}]" if e.entity_refs else ""
            typer.echo(f"  {e.seq:>3} {e.event_type}{refs}  {_row(e.payload)}")

    _run_async(_run)


@app.command()
def state(
    campaign_id: str = typer.Argument(None, help="a campaign (or pass --world)"),
    world: str = typer.Option(None, "--world", help="read a world branch or past commit instead"),
    branch: str = typer.Option(None, "--branch", help="with --world: a branch head (default main)"),
    at: str = typer.Option(None, "--at", help="with --world: any commit, marker, or id prefix"),
    sections: str = typer.Option(
        "actors,threads,places,factions",
        "--sections",
        help="comma-separated projections, e.g. actors,claims,beliefs,sheets,items,edges,counters",
    ),
) -> None:
    """World state, one JSON row per entity: a campaign's now, or with --world any branch head or
    any past commit (--at materializes it like a fork and discards it — nothing is written). Every
    section, including the omniscient ones (claim truth values, hidden beliefs, sheets): the
    operator view (D-45)."""

    async def _run() -> None:
        if (campaign_id is None) == (world is None):
            typer.echo("give a campaign id, or --world <world> [--branch B | --at REF]", err=True)
            raise typer.Exit(1)
        if at is not None and branch is not None:
            typer.echo("give --at or --branch, not both", err=True)
            raise typer.Exit(1)
        if campaign_id is not None and (at is not None or branch is not None):
            typer.echo(
                "--at/--branch read a world: use --world (a campaign shows its now)", err=True
            )
            raise typer.Exit(1)
        names = [s.strip() for s in sections.split(",") if s.strip()]
        store = build_store()
        await connect_store(store)
        try:
            if campaign_id is not None:
                c = await _campaign_or_exit(store, campaign_id)
                rows = (await store.query_across([c.branch_id], names))[c.branch_id]
            else:
                w = await _world_or_exit(store, world)
                if at is not None:
                    commit_id, rows = await store.state_at(w.world_id, at, names)
                    typer.echo(f"at commit {commit_id}")
                else:
                    b = await _branch_or_exit(store, w, branch or "main")
                    rows = (await store.query_across([b.branch_id], names))[b.branch_id]
        finally:
            await store.close()
        for section, section_rows in rows.items():
            typer.echo(f"{section} ({len(section_rows)}):")
            for r in section_rows:
                typer.echo(f"  {_row(r)}")

    _run_async(_run)


@app.command()
def chronicle(
    campaign_id: str = typer.Argument(None, help="a campaign (or pass --world for any branch)"),
    world: str = typer.Option(None, "--world", help="read a world branch instead of a campaign"),
    branch: str = typer.Option("main", "--branch", help="with --world: the branch (default main)"),
    limit: int = typer.Option(10, "--limit", min=0, help="max beats"),
) -> None:
    """The recent beats of a campaign — or, with --world, of any named branch — oldest first."""

    async def _run() -> None:
        if (campaign_id is None) == (world is None):
            typer.echo("give a campaign id, or --world <world> [--branch]", err=True)
            raise typer.Exit(1)
        store = build_store()
        await connect_store(store)
        try:
            if campaign_id is not None:
                branch_id = (await _campaign_or_exit(store, campaign_id)).branch_id
            else:
                w = await _world_or_exit(store, world)
                branch_id = (await _branch_or_exit(store, w, branch)).branch_id
            beats = await store.recent_beats(branch_id, limit)
        finally:
            await store.close()
        if not beats:
            typer.echo("(no beats yet)")
        for beat in beats:
            typer.echo(f"> [{beat.participant_id}] {beat.intent_text}")
            typer.echo(f"{beat.narration}\n")

    _run_async(_run)


@app.command()
def usage(stage: str = typer.Option(None, "--stage", help="only this pipeline stage")) -> None:
    """LLM-call telemetry by stage: calls, models, tokens (0 until metered), average latency."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            rows = await store.usage_by_stage(stage)
        finally:
            await store.close()
        typer.echo(f"total calls: {sum(int(r['calls']) for r in rows)}")
        for r in rows:
            typer.echo(f"  {_row(r)}")

    _run_async(_run)


@ruleset_app.command("list")
def ruleset_list() -> None:
    """The rulesets a world can declare (`[ruleset] id` in world.toml), each with the settings a
    game may give it under `[ruleset.config]` and their defaults (D-62)."""
    for rid in registry.available():
        rs = registry.resolve(rid)
        typer.echo(
            f"{rs.id}@{rs.version}  sheet: {', '.join(rs.sheet_schema().get('properties', {}))}"
        )
        for key, spec in rs.config_schema().get("properties", {}).items():
            what = spec.get("description", "")
            typer.echo(f"    {key:<24} default {spec.get('default')!s:<5} {what}")


@campaign_app.command("list")
def campaign_list(
    world: str = typer.Option(None, "--world", help="only this world's campaigns (name or id)"),
) -> None:
    """List campaigns with their world, branch, and ruleset."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            world_id = (await _world_or_exit(store, world)).world_id if world else None
            campaigns = await store.list_campaigns(world_id)
            worlds = {w.world_id: w.name for w in await store.list_worlds()}
            branches = {}
            for c in campaigns:
                b = await store.get_branch(c.branch_id)
                branches[c.branch_id] = b.name if b else c.branch_id
        finally:
            await store.close()
        if not campaigns:
            typer.echo("(no campaigns)")
        for c in campaigns:
            typer.echo(
                f"{c.campaign_id}  world={worlds.get(c.world_id, c.world_id)}  "
                f"branch={branches[c.branch_id]}  ruleset={c.ruleset_id or '-'}"
            )

    _run_async(_run)


@campaign_app.command("show")
def campaign_show(campaign_id: str) -> None:
    """One campaign: its world, branch, ruleset, seed, in-fiction day, and seated PCs."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            c = await _campaign_or_exit(store, campaign_id)
            w = await store.get_world(c.world_id)
            b = await store.get_branch(c.branch_id)
            day = await store.current_world_time(c.branch_id)
            seats = []
            for participant in await store.pc_seats(campaign_id):
                pc = await store.pc_for_participant(campaign_id, participant)
                actor = await store.get_actor(c.branch_id, pc) if pc else None
                seats.append((participant, pc, actor.name if actor else "?"))
        finally:
            await store.close()
        typer.echo(f"campaign {c.campaign_id}")
        typer.echo(f"  world    {w.name if w else c.world_id}")
        typer.echo(f"  branch   {b.name if b else c.branch_id}")
        rs = f"{c.ruleset_id}@{c.ruleset_version}" if c.ruleset_id else "-"
        typer.echo(f"  ruleset  {rs}   seed {c.seed}   day {day}")
        typer.echo("  seats:" if seats else "  seats: (none — the campaign has ended)")
        for participant, pc, name in seats:
            typer.echo(f"    {participant} → {pc} ({name})")

    _run_async(_run)


@branch_app.command("diff")
def branch_diff(
    world: str,
    a: str = typer.Argument(..., help="the base branch"),
    b: str = typer.Argument(..., help="the branch to compare against it"),
    sections: str = typer.Option(None, "--sections", help="comma-separated projections"),
) -> None:
    """What differs from branch A to branch B, per projection: + added in B, - removed from A,
    ~ changed (the B-side row). Omniscient (claim truth, beliefs, sheets) — the operator view."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            w = await _world_or_exit(store, world)
            ba = await _branch_or_exit(store, w, a)
            bb = await _branch_or_exit(store, w, b)
            names = [x.strip() for x in sections.split(",") if x.strip()] if sections else None
            diff = await store.diff_branches(ba.branch_id, bb.branch_id, names)
        finally:
            await store.close()
        changed = False
        for section, parts in diff.items():
            rows = [("+", r) for r in parts["added"]] + [("-", r) for r in parts["removed"]]
            rows += [("~", r) for r in parts["changed"]]
            if rows:
                changed = True
                typer.echo(f"{section}:")
                for mark, r in rows:
                    typer.echo(f"  {mark} {_row(r)}")
        if not changed:
            typer.echo(f"no differences between {a!r} and {b!r}")

    _run_async(_run)


@campaign_app.command("new")
def campaign_new(
    world: str,
    branch: str = typer.Option("main", "--branch", help="branch to play on (default main)"),
    adopt: str = typer.Option(None, "--adopt", help="adopt an existing actor id as the PC"),
    pc: str = typer.Option(None, "--pc", help="create a fresh PC with this name"),
    participant: str = typer.Option(PARTICIPANT, "--participant", help="participant id"),
    seed: int = typer.Option(
        0, "--seed", help="mechanics RNG seed (docs/18 G-3) — pin it for reproducible combat"
    ),
) -> None:
    """Start a campaign on a branch, binding a PC (adopt an existing actor, or create one)."""

    async def _run() -> None:
        if (adopt is None) == (pc is None):
            typer.echo("provide exactly one of --adopt <actor_id> or --pc <name>", err=True)
            raise typer.Exit(1)
        store = build_store()
        await connect_store(store)
        try:
            w = await _world_or_exit(store, world)
            b = await store.get_branch_by_name(w.world_id, branch)
            if b is None:
                typer.echo(f"no such branch: {branch}", err=True)
                raise typer.Exit(1)
            # Bind the WORLD's declared ruleset (docs/06, D-30): a PbtA world's campaign is
            # played under uro_pbta, not the hard-coded default. ('' → registry default.)
            pin = await store.world_ruleset(b.branch_id)
            rs = await registry.resolve_for_branch(store, b.branch_id)  # with the world's settings
            # Every PC needs a sheet so the mechanics gate can check it. A fresh PC gets one; an
            # adopted actor is sheeted only if it lacks one (a re-adopted PC keeps its sheet).
            pc_sheet = None
            if pc is not None or (
                adopt is not None and await store.get_sheet(b.branch_id, adopt) is None
            ):
                pc_sheet = _build_pc_sheet(rs)
            ruleset_id = rs.id
            campaign = await store.start_campaign(
                w.world_id,
                b.branch_id,
                participant_id=participant,
                adopt_actor_id=adopt,
                new_pc_name=pc,
                pc_sheet=pc_sheet,
                starting_items=list(STARTING_GEAR) if pc is not None else None,
                ruleset_id=ruleset_id,
                ruleset_version=pin.version,
                seed=seed,
            )
        finally:
            await store.close()
        who = f"adopted {adopt}" if adopt else f"new PC {pc!r}"
        typer.echo(f"campaign: {campaign.campaign_id}  (branch {branch!r}, {who})")
        typer.echo(f"play it:  uro play {campaign.campaign_id}")

    _run_async(_run)


@campaign_app.command("time-skip")
def campaign_time_skip(
    campaign_id: str,
    days: int = typer.Option(..., "--days", min=1, help="in-fiction days to advance"),
) -> None:
    """Advance time ON the campaign's own branch and fire the world's downtime agenda rules (D-33).
    To skip time on a NEW branch instead, use `uro branch fork --time-skip-days`."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            c = await _campaign_or_exit(store, campaign_id)
            # downtime + agendas need no LLM, so a stub router suffices; the campaign's own ruleset
            # and settings decide what the downtime means (D-63)
            ruleset = await registry.resolve_for_campaign(store, c)
            engine = Engine(store, build_router("stub", None), ruleset=ruleset)
            await engine.agenda_tick(c.branch_id, days)
            day = await store.current_world_time(c.branch_id)
        finally:
            await store.close()
        typer.echo(f"advanced {days} day(s); the campaign is now on day {day}")

    _run_async(_run)


@campaign_app.command("outcome")
def campaign_outcome(
    campaign_id: str,
    bundle_path: Path = typer.Argument(  # noqa: B008
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="an outcome bundle (JSON) from an external game",
    ),
) -> None:
    """Record an external game's encounter outcome (Chronicler mode, D-25/D-32) — the same
    trust-scoped distillation as the server's outcome endpoint, then the reaction rules."""

    async def _run() -> None:
        from uro_core.chronicler import OutcomeBundle, distill_outcome_with_receipt

        bundle = OutcomeBundle.model_validate_json(bundle_path.read_text())
        store = build_store()
        await connect_store(store)
        try:
            c = await _campaign_or_exit(store, campaign_id)
            result = await distill_outcome_with_receipt(store, c.branch_id, bundle)
            # commit + react in one call (B1): a pack rule on ActorDied must fire here too
            engine = Engine(store, build_router("stub", None))
            commit = await engine.append_and_react(c, result.events)
        finally:
            await store.close()
        typer.echo(f"committed {len(result.events)} event(s) → {commit.commit_id}")
        for r in result.receipt:
            typer.echo(f"  {_row(r.model_dump())}")

    _run_async(_run)


@campaign_app.command("end")
def campaign_end(
    campaign_id: str,
    marker: str = typer.Option(..., "--marker", help="name the closing commit (a fork root)"),
    outcome: str = typer.Option("", "--outcome", help="short outcome note"),
) -> None:
    """End a campaign: release its PCs to world NPCs and mark the closing commit."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            m = await store.end_campaign(campaign_id, marker, outcome=outcome)
        finally:
            await store.close()
        typer.echo(f"campaign ended; marker {m.name!r} → commit {m.commit_id}")

    _run_async(_run)


@campaign_app.command("join")
def campaign_join(
    campaign_id: str,
    participant: str = typer.Option(..., "--participant", help="joining participant id"),
    adopt: str = typer.Option(None, "--adopt", help="adopt an existing actor as this PC"),
    pc: str = typer.Option(None, "--pc", help="create a fresh PC with this name"),
) -> None:
    """Seat an ADDITIONAL participant on a live campaign, binding their OWN PC (docs/08, OQ-7) —
    the party-join path. Round-robin free-roam then rotates turns across the party (`uro serve`
    with multiple --token spins up a PartyArbiter)."""

    async def _run() -> None:
        if (adopt is None) == (pc is None):
            typer.echo("provide exactly one of --adopt <actor_id> or --pc <name>", err=True)
            raise typer.Exit(1)
        store = build_store()
        await connect_store(store)
        try:
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                typer.echo(f"no such campaign: {campaign_id}", err=True)
                raise typer.Exit(1)
            # Sheet a fresh PC (or an adopted actor lacking one) from the campaign's bound ruleset.
            pc_sheet = None
            if pc is not None or (
                adopt is not None and await store.get_sheet(campaign.branch_id, adopt) is None
            ):
                pc_sheet = _build_pc_sheet(await registry.resolve_for_campaign(store, campaign))
            actor = await store.bind_pc(
                campaign_id,
                participant,
                adopt_actor_id=adopt,
                new_pc_name=pc,
                pc_sheet=pc_sheet,
                starting_items=list(STARTING_GEAR) if pc is not None else None,
                ruleset_id=campaign.ruleset_id,
            )
        finally:
            await store.close()
        typer.echo(f"joined: participant {participant!r} → PC {actor}")

    _run_async(_run)


@app.command("dry-run")
def dry_run(
    campaign_id: str,
    intent: str,
    provider: str = typer.Option("stub", help="stub | local | openai | anthropic"),
    model: str = typer.Option(None, help="model id for local/openai/anthropic providers"),
    role_model: list[str] = typer.Option(  # noqa: B008 (typer DI-style default, like every option here)
        None, "--role-model", help="per-role model override, repeatable: role=spec (docs/04)"
    ),
) -> None:
    """Dry-run a beat (docs/09 creator loop): show the events it WOULD commit, without writing.
    Nothing enters the log — the campaign is untouched."""
    role_models = parse_role_models(role_model)

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                typer.echo(f"no such campaign: {campaign_id}", err=True)
                raise typer.Exit(1)
            engine = Engine(
                store,
                build_router(provider, model, role_models),
                ruleset=await registry.resolve_for_campaign(store, campaign),
            )
            events = await engine.preview_beat(campaign, PARTICIPANT, intent)
        finally:
            await store.close()
        typer.echo(f"dry-run {intent!r}: {len(events)} event(s) would commit (nothing written):")
        for e in events:
            refs = f"  → {e.entity_refs}" if e.entity_refs else ""
            typer.echo(f"  {e.event_type}{refs}")

    _run_async(_run)


@app.command()
def consistency(campaign_id: str) -> None:
    """Report the narrator contradiction-survival rate (thesis proxy metric T2).

    Counts narrator-asserted claims that survived the extractor's contradiction gauntlet
    (i.e. were not flagged as contradicting recalled state). This is a PROXY — it only
    catches contradictions the extractor self-flagged, not all narration-vs-truth
    disagreement — best read as a regression trend, not ground-truth verification.
    """

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                typer.echo(f"no such campaign: {campaign_id}", err=True)
                raise typer.Exit(1)
            consistent, total = await store.fact_consistency(campaign.branch_id)
        finally:
            await store.close()
        ratio = consistent / total if total else 1.0
        typer.echo(
            f"T2 (proxy): {consistent}/{total} narrator claims survived the contradiction "
            f"gauntlet ({ratio:.0%}) — regression trend, not ground-truth verification"
        )

    _run_async(_run)


@codex_app.command("add")
def codex_add(
    campaign_id: str,
    text: str,
    participant: str = typer.Option(PARTICIPANT, "--participant", help="whose codex"),
    key: str = typer.Option(
        None, "--key", help="dedup key (re-adding overwrites); default: content hash"
    ),
    pinned: bool = typer.Option(False, "--pinned", help="always surface (vs only when mentioned)"),
    ref: list[str] = typer.Option(  # noqa: B008 (typer DI-style default)
        None,
        "--ref",
        help="entity trigger, repeatable: a term/name that surfaces this note (e.g. vault)",
    ),
) -> None:
    """Record an out-of-world player note (docs/18 B8) that survives a fork — the player's private
    edge across loops/lives. It never becomes canon and no NPC ever knows it."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                typer.echo(f"no such campaign: {campaign_id}", err=True)
                raise typer.Exit(1)
            used = await store.participant_remember(
                participant, campaign.world_id, text, key=key, pinned=pinned, entity_refs=ref or []
            )
        finally:
            await store.close()
        typer.echo(
            f"codex: noted for {participant!r} (key {used!r}){' [pinned]' if pinned else ''}"
        )

    _run_async(_run)


@codex_app.command("list")
def codex_list(
    campaign_id: str,
    participant: str = typer.Option(PARTICIPANT, "--participant", help="whose codex"),
) -> None:
    """List a participant's out-of-world notes for this campaign's world."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                typer.echo(f"no such campaign: {campaign_id}", err=True)
                raise typer.Exit(1)
            notes = await store.participant_notes(participant, campaign.world_id)
        finally:
            await store.close()
        if not notes:
            typer.echo(f"codex empty for {participant!r}")
            return
        for n in notes:
            tag = (
                " [pinned]"
                if n.pinned
                else (f"  (on: {', '.join(n.entity_refs)})" if n.entity_refs else "")
            )
            typer.echo(f"- {n.text}{tag}")

    _run_async(_run)


# --- Model-connection registry (D-47, docs/20) — direct-DB config of the LLM provider registry.
# A server API mirror (for uro-loom / remote clients) lands in the next slice; today these edit the
# instance's DB directly (operator with DB access), like `uro world`/`uro campaign`.


@provider_app.command("add")
def provider_add(
    name: str = typer.Argument(..., help="a display label for this connection"),
    provider: str = typer.Option(
        ..., "--provider", help="provider kind: openai | anthropic | openai_compat | local | stub"
    ),
    base_url: str = typer.Option(None, "--base-url", help="per-connection endpoint override"),
    api_key: str = typer.Option(
        None,
        "--api-key",
        help="an API key to store ENCRYPTED (needs URO_SECRET_KEY) — creates + links a credential",
    ),
    credential: str = typer.Option(
        None, "--credential", help="reuse an EXISTING credential id instead of --api-key"
    ),
) -> None:
    """Register a model connection (a provider endpoint). With --api-key it also stores an encrypted
    credential and links it; a keyless provider (local/stub) needs neither."""

    async def _run() -> None:
        if api_key and credential:
            raise ValueError("pass either --api-key or --credential, not both")
        store = build_store()
        await connect_store(store)
        try:
            auth_id: str | None = None
            if api_key:
                auth_id = await store.add_credential(provider=provider, access_token=api_key)
            elif credential:
                if all(c.id != credential for c in await store.list_credentials()):
                    raise ValueError(f"no such credential: {credential}")
                auth_id = credential
            cid = await store.add_connection(
                name=name, provider=provider, base_url=base_url or None, auth_id=auth_id
            )
        finally:
            await store.close()
        typer.echo(f"connection: {cid}  ({name} → {provider})")
        if auth_id:
            typer.echo(f"credential: {auth_id}  (encrypted at rest)")
        # Steer toward binding `default` FIRST — a registry with role bindings but no `default`
        # is refused at serve/reload (unbound roles would crash a beat; holistic review).
        typer.echo(f"\nbind the default role:  uro provider bind default {cid} <model>")

    _run_async(_run)


@provider_app.command("list")
def provider_list() -> None:
    """Show the registry: connections, role bindings, and credential metadata (never secrets)."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            connections = await store.list_connections()
            bindings = await store.list_role_bindings()
            credentials = await store.list_credentials()
        finally:
            await store.close()
        typer.echo("connections:")
        if not connections:
            typer.echo("  (none — add one with `uro provider add`)")
        for c in connections:
            base = f" @{c.base_url}" if c.base_url else ""
            auth = f" auth={c.auth_id}" if c.auth_id else " (keyless)"
            disabled = "" if c.is_enabled else " [disabled]"
            typer.echo(f"  {c.id}  {c.name} → {c.provider}{base}{auth}{disabled}")
        typer.echo("role bindings:")
        if not bindings:
            typer.echo("  (none — the server falls back to the --provider/uro.toml seed)")
        for b in bindings:
            typer.echo(f"  {b.role:<9} → {b.connection_id} : {b.model}")
        if credentials:
            typer.echo("credentials:")
            for cr in credentials:
                has = "  (access_token set)" if cr.has_access_token else ""
                typer.echo(f"  {cr.id}  {cr.provider}  {cr.auth_mode}{has}")

    _run_async(_run)


@provider_app.command("rm")
def provider_rm(
    connection_id: str = typer.Argument(..., help="the connection id to delete"),
) -> None:
    """Delete a connection (its role bindings cascade → those roles fall back to `default`). Its
    credential is kept — another connection may share it; remove it with `credential rm`."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            ok = await store.delete_connection(connection_id)
        finally:
            await store.close()
        typer.echo("deleted" if ok else f"no such connection: {connection_id}")

    _run_async(_run)


@credential_app.command("rm")
def provider_credential_rm(
    credential_id: str = typer.Argument(..., help="the credential id to delete"),
    force: bool = typer.Option(
        False, "--force", help="delete even if connections use it (they become unauthenticated)"
    ),
) -> None:
    """Delete a stored credential. Connections linked to it are UNLINKED, not deleted (docs/20), so
    a credential still in use is refused unless --force."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            if all(c.id != credential_id for c in await store.list_credentials()):
                raise ValueError(f"no such credential: {credential_id}")
            linked = [c.id for c in await store.list_connections() if c.auth_id == credential_id]
            if linked and not force:
                raise ValueError(
                    f"credential {credential_id} is used by {', '.join(linked)} — delete those "
                    "connections first, or pass --force to leave them unauthenticated"
                )
            await store.delete_credential(credential_id)
        finally:
            await store.close()
        typer.echo("deleted")
        if linked:
            typer.echo(f"unlinked (now unauthenticated): {', '.join(linked)}")

    _run_async(_run)


@provider_app.command("enable")
def provider_enable(connection_id: str) -> None:
    """Enable a connection (a disabled one is skipped when the router is built)."""
    _set_enabled(connection_id, True)


@provider_app.command("disable")
def provider_disable(connection_id: str) -> None:
    """Disable a connection without deleting it or its role bindings."""
    _set_enabled(connection_id, False)


def _set_enabled(connection_id: str, enabled: bool) -> None:
    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            ok = await store.set_connection_enabled(connection_id, enabled)
        finally:
            await store.close()
        if not ok:
            raise ValueError(f"no such connection: {connection_id}")
        typer.echo(f"{'enabled' if enabled else 'disabled'} {connection_id}")

    _run_async(_run)


@provider_app.command("link")
def provider_link(
    connection_id: str,
    credential_id: str = typer.Argument(..., help="the credential the connection should use"),
) -> None:
    """Point a connection at another stored credential (e.g. after `credential add`)."""
    _set_credential(connection_id, credential_id)


@provider_app.command("unlink")
def provider_unlink(connection_id: str) -> None:
    """Remove a connection's credential, leaving it unauthenticated."""
    _set_credential(connection_id, None)


def _set_credential(connection_id: str, credential_id: str | None) -> None:
    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            ok = await store.set_connection_credential(connection_id, credential_id)
        finally:
            await store.close()
        if not ok:
            raise ValueError(f"no such connection: {connection_id}")
        typer.echo(f"{'linked' if credential_id else 'unlinked'} {connection_id}{_RELOAD_HINT}")

    _run_async(_run)


_RELOAD_HINT = "  (a running server uses it after `uro provider reload`)"


@provider_app.command("refresh")
def provider_refresh(connection_id: str) -> None:
    """Discover a connection's models (a LIVE provider call) and cache them with their modality."""

    async def _run() -> None:
        import httpx  # via uro-core[llm]
        from uro_core.providers.registry import refresh_connection_models

        store = build_store()
        await connect_store(store)
        try:
            if await store.get_connection(connection_id) is None:
                raise ValueError(f"no such connection: {connection_id}")
            try:
                models = await refresh_connection_models(store, connection_id)
            except (httpx.HTTPError, OSError) as exc:
                # The exception text can carry the plaintext key (the server's holistic HIGH):
                # report only its type.
                raise RuntimeError(f"model discovery failed ({type(exc).__name__})") from exc
        finally:
            await store.close()
        typer.echo(f"{len(models)} model(s):")
        for m in models:
            typer.echo(f"  {m['id']}  ({m.get('modality', '?')})")

    _run_async(_run)


@provider_app.command("test")
def provider_test(
    connection_id: str,
    model: str = typer.Option("", "--model", help="the model to probe (default: a known-good one)"),
) -> None:
    """Probe a connection with one tiny call. Exits 1 if the provider refuses it."""

    async def _run() -> None:
        from uro_core.providers.registry import probe_connection

        store = build_store()
        await connect_store(store)
        try:
            if await store.get_connection(connection_id) is None:
                raise ValueError(f"no such connection: {connection_id}")
            result = await probe_connection(store, connection_id, model)
        finally:
            await store.close()
        typer.echo(("ok: " if result["ok"] else "failed: ") + result["detail"])
        if not result["ok"]:
            raise typer.Exit(1)

    _run_async(_run)


@provider_app.command("reload")
def provider_reload(
    server: str = typer.Option("http://127.0.0.1:8000", help="server base URL"),
    token: str = typer.Option(..., help="an operator --admin-token"),
) -> None:
    """Make a RUNNING `uro serve` rebuild its router from the registry. The `uro provider` commands
    edit the database; a server only sees the change after a reload (or a restart)."""
    out = _server_post(server, "/providers/reload", token, {})
    typer.echo("reloaded" if out.get("reloaded") else f"not reloaded: {out.get('detail', out)}")


@credential_app.command("add")
def provider_credential_add(
    provider: str = typer.Option(..., "--provider", help="the provider kind it authenticates"),
    api_key: str = typer.Option(..., "--api-key", help="stored ENCRYPTED (needs URO_SECRET_KEY)"),
) -> None:
    """Store an API key on its own, to link to connections with `provider add --credential`."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            cred = await store.add_credential(provider=provider, access_token=api_key)
        finally:
            await store.close()
        typer.echo(f"credential: {cred}  (encrypted at rest)")

    _run_async(_run)


@credential_app.command("rotate")
def provider_credential_rotate(
    credential_id: str,
    api_key: str = typer.Option(..., "--api-key", help="the new key, stored ENCRYPTED"),
) -> None:
    """Replace a stored key in place: every connection linked to it uses the new key, with no
    connection or role binding rebuilt."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            cred = next((c for c in await store.list_credentials() if c.id == credential_id), None)
            if cred is not None and cred.auth_mode != "api_key":
                # its stored refresh token belongs to the OLD grant, and would restore the old login
                raise ValueError(
                    f"{credential_id} is an OAuth login ({cred.auth_mode}) — log in again instead"
                )
            ok = await store.update_credential_tokens(credential_id, access_token=api_key)
            users = [c.id for c in await store.list_connections() if c.auth_id == credential_id]
        finally:
            await store.close()
        if not ok:
            raise ValueError(f"no such credential: {credential_id}")
        used = f" — used by {', '.join(users)}" if users else " — no connection uses it yet"
        typer.echo(f"rotated {credential_id}{used}{_RELOAD_HINT}")

    _run_async(_run)


@policy_app.command("show")
def policy_show() -> None:
    """Show which emergent categories play may create."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            policy = await store.get_extraction_policy()
        finally:
            await store.close()
        for name, on in policy.model_dump().items():
            typer.echo(f"{name.removeprefix('extract_'):<9} {'on' if on else 'off'}")

    _run_async(_run)


@policy_app.command("set")
def policy_set(
    actors: bool = typer.Option(None, "--actors/--no-actors"),
    places: bool = typer.Option(None, "--places/--no-places"),
    factions: bool = typer.Option(None, "--factions/--no-factions"),
    threads: bool = typer.Option(None, "--threads/--no-threads"),
    claims: bool = typer.Option(
        None, "--claims/--no-claims", help="claims and beliefs; recall degrades without them"
    ),
) -> None:
    """Turn emergent categories on or off; unnamed ones are left as they are."""
    flags = {"actors": actors, "places": places, "factions": factions, "threads": threads}
    flags["claims"] = claims
    updates = {f"extract_{k}": v for k, v in flags.items() if v is not None}

    async def _run() -> None:
        if not updates:
            raise ValueError("name at least one category, e.g. --no-places")
        store = build_store()
        await connect_store(store)
        try:
            policy = (await store.get_extraction_policy()).model_copy(update=updates)
            await store.set_extraction_policy(policy)
        finally:
            await store.close()
        for name, on in policy.model_dump().items():
            typer.echo(f"{name.removeprefix('extract_'):<9} {'on' if on else 'off'}")

    _run_async(_run)


@provider_app.command("bind")
def provider_bind(
    role: str = typer.Argument(..., help=f"one of: {', '.join(_ROLES)}"),
    connection_id: str = typer.Argument(...),
    model: str = typer.Argument(..., help="a model id offered by the connection's provider"),
) -> None:
    """Bind an engine role to a connection + model. `default` backs any unbound role."""

    async def _run() -> None:
        from uro_core.providers.registry import classify_modality

        if role not in _ROLES:
            raise ValueError(f"unknown role {role!r}; one of: {', '.join(_ROLES)}")
        store = build_store()
        await connect_store(store)
        try:
            conn = await store.get_connection(connection_id)
            if conn is None:
                raise ValueError(f"no such connection: {connection_id}")
            # Match the server's embedder-modality guard (D-47): the embedder needs an EMBEDDING
            # model, so reject a chat model here too — else the binding fails only at runtime, and a
            # codex/anthropic model (always "chat") could silently break semantic memory (review).
            if role == "embedder" and classify_modality(conn.provider, model) == "chat":
                raise ValueError(
                    f"the embedder role needs an embedding model, not {model!r} "
                    f"(a chat model on provider {conn.provider!r})"
                )
            await store.set_role_binding(role, connection_id, model)
        finally:
            await store.close()
        typer.echo(f"bound {role} → {connection_id} : {model}")

    _run_async(_run)


@provider_app.command("unbind")
def provider_unbind(role: str = typer.Argument(..., help="the role to unbind")) -> None:
    """Remove a role binding (that role falls back to `default`)."""

    async def _run() -> None:
        store = build_store()
        await connect_store(store)
        try:
            ok = await store.delete_role_binding(role)
        finally:
            await store.close()
        typer.echo("unbound" if ok else f"no binding for role: {role}")

    _run_async(_run)


@provider_app.command("codex-login")
def provider_codex_login(
    name: str = typer.Option("codex", "--name", help="a display label for the connection"),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="don't try to open the authorization page automatically"
    ),
) -> None:
    """Connect a Codex (ChatGPT-subscription) provider via OpenAI's OAuth device login. Prints a
    code to enter on the authorization page, then polls until you approve — no API key. UNOFFICIAL:
    uses a consumer ChatGPT subscription; may breach OpenAI's terms (needs URO_SECRET_KEY)."""

    async def _run() -> None:
        from uro_core.providers import codex_auth

        try:
            dc = await codex_auth.request_device_code()
            typer.echo(f"\n  Enter this code on the OpenAI authorization page:  {dc['user_code']}")
            typer.echo(f"  Authorization page:  {dc['verification_uri']}\n")
            if not no_browser:
                with contextlib.suppress(Exception):  # opening a browser is best-effort
                    webbrowser.open(dc["verification_uri"])
            typer.echo("Waiting for you to authorize with OpenAI… (Ctrl-C to cancel)")

            deadline = time.monotonic() + dc["expires_in"]
            payload = None
            while time.monotonic() < deadline:
                await asyncio.sleep(dc["interval"])
                payload = await codex_auth.poll_device_auth(dc["device_auth_id"], dc["user_code"])
                if payload is not None:
                    break
            if payload is None:
                raise ValueError("the login timed out before you approved the code — try again")
            tokens = await codex_auth.exchange_code(
                payload["authorization_code"], payload["code_verifier"]
            )
        except codex_auth.CodexAuthError as exc:
            raise ValueError(f"codex login failed: {exc}") from exc

        store = build_store()
        await connect_store(store)
        try:
            cred_id = await store.add_credential(
                provider="codex",
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                auth_mode="oauth_device",
            )
            conn_id = await store.add_connection(name=name, provider="codex", auth_id=cred_id)
            models: list[dict[str, str]] = []
            try:
                models = await codex_auth.discover_codex_models(tokens["access_token"])
                await store.set_connection_models(conn_id, models)
            except Exception:
                pass
        finally:
            await store.close()
        typer.echo(f"\nconnected: {conn_id}  ({name} → codex)")
        if models:
            typer.echo("models: " + ", ".join(m["id"] for m in models))
        typer.echo(f"\nbind the default role:  uro provider bind default {conn_id} <model>")

    _run_async(_run)
