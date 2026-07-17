"""Entry point for the `uro` command. Command surface per docs/08-api-and-sessions.md.

Phase 0 subset: version, db migrate, world new, play.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import typer
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.pipeline.engine import Engine
from uro_core.rulesets.base import CharSpec
from uro_core.rulesets.rng import Rng
from uro_core.timeline.models import World

from uro_cli.wiring import build_router, build_ruleset, build_store, parse_role_models

app = typer.Typer(no_args_is_help=True, help="Uro Engine — reference client.")
db_app = typer.Typer(no_args_is_help=True, help="Database management.")
world_app = typer.Typer(no_args_is_help=True, help="World and campaign management.")
branch_app = typer.Typer(no_args_is_help=True, help="Branch and timeline management (docs/03).")
campaign_app = typer.Typer(no_args_is_help=True, help="Campaign lifecycle over branches.")
codex_app = typer.Typer(
    no_args_is_help=True, help="Player codex — out-of-world notes that survive a fork (docs/18 B8)."
)
app.add_typer(db_app, name="db")
app.add_typer(world_app, name="world")
app.add_typer(branch_app, name="branch")
app.add_typer(campaign_app, name="campaign")
app.add_typer(codex_app, name="codex")

PARTICIPANT = "player-1"  # Phase 0 is single-player; participants arrive in Phase 5.


def _run_async(coro_factory) -> None:  # type: ignore[no-untyped-def]
    """Run an async command, turning config/credential/lookup errors into a clean message."""
    try:
        asyncio.run(coro_factory())
    except (RuntimeError, ValueError, KeyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def _world_or_exit(store: PostgresEventStore, ident: str) -> World:
    """Resolve a world by name (preferred) or id, or exit 1 with a clean message."""
    world = await store.get_world_by_name(ident) or await store.get_world(ident)
    if world is None:
        typer.echo(f"no such world: {ident}", err=True)
        raise typer.Exit(1)
    return world


def _build_pc_sheet(ruleset_id: str = "", version: str = "") -> tuple[dict[str, Any], str]:
    """A default character sheet from the world's bound ruleset, so every PC can be checked
    (docs/06). `ruleset_id` selects it via the registry — a PbtA world mints a PbtA sheet."""
    ruleset = build_ruleset(ruleset_id, version)
    return ruleset.new_character(CharSpec(), Rng(0)), ruleset.id


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
        await store.connect()
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
def world_new(name: str) -> None:
    """Create a world (+ its main branch) and a ready-to-play campaign with a default,
    sheeted PC. Prints the campaign id."""

    async def _run() -> None:
        store = build_store()
        await store.connect()
        try:
            world = await store.create_world(name)
            sheet, ruleset_id = _build_pc_sheet()
            campaign = await store.start_campaign(
                world.world_id,
                world.main_branch_id,
                participant_id=PARTICIPANT,
                new_pc_name="Adventurer",
                pc_sheet=sheet,
                starting_items=["a traveler's knife"],
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
    from uro_core.rulesets import registry
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
    # Catch a bad ruleset id at VALIDATE, not as a raw KeyError at `campaign new` (phase-6 review).
    rid = pack.manifest.ruleset.id
    if rid in registry.available():
        typer.echo(f"  ok  ruleset    {rid}")
    else:
        typer.echo(f"  GAP ruleset    unknown ruleset {rid!r} (installed: {registry.available()})")
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
        from uro_core.rulesets import registry
        from uro_core.worldpack.backfill import backfill_gaps
        from uro_core.worldpack.importer import pack_to_events
        from uro_core.worldpack.parse import parse_pack
        from uro_core.worldpack.sufficiency import check_sufficiency

        try:
            pack = parse_pack(path)
        except PackError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        # Fail loudly HERE if the pack names a ruleset the build can't resolve — else the world
        # commits fine and only blows up as a raw KeyError at `campaign new` (phase-6 review).
        if pack.manifest.ruleset.id not in registry.available():
            typer.echo(
                f"error: pack declares unknown ruleset {pack.manifest.ruleset.id!r} "
                f"(installed: {registry.available()})",
                err=True,
            )
            raise typer.Exit(1)
        report = check_sufficiency(pack)
        added: list[str] = []
        if backfill and report.grade != "runnable":
            pack, added = await backfill_gaps(pack, build_router(provider, model), report=report)
            report = check_sufficiency(pack)
        if report.grade == "insufficient":
            typer.echo(f"error: pack is INSUFFICIENT to run: {'; '.join(report.gaps)}", err=True)
            raise typer.Exit(1)
        store = build_store()
        await store.connect()
        try:
            world = await store.create_world(
                pack.manifest.name,
                tone=pack.manifest.tone,
                prompt_overrides=pack.prompts,
                ruleset_id=pack.manifest.ruleset.id,
                ruleset_version=pack.manifest.ruleset.version,
                rule_pack=pack.rule_pack.model_dump() if pack.rule_pack else {},
                extra_events=pack_to_events(pack),
            )
        finally:
            await store.close()
        typer.echo(f"world: {world.world_id}  ({pack.manifest.name}, grade {report.grade})")
        for a in added:
            typer.echo(f"  backfilled + committed: {a}")
        typer.echo(f"seed history:  uro world seed {path} --seed 42")

    _run_async(_run)


@world_app.command("seed")
def world_seed(
    path: str, seed: int = typer.Option(42, "--seed", help="RNG seed for History")
) -> None:
    """Run History seeding on the world imported from <path>: layer seed-dependent dynasties and
    wars on top of the authored geography (docs/09). Same pack + a different seed → a different
    history on identical geography."""

    async def _run() -> None:
        from uro_core.engines.history import seed_history
        from uro_core.errors import PackError
        from uro_core.rulesets.rng import Rng
        from uro_core.worldpack.parse import parse_pack

        try:
            pack = parse_pack(path)
        except PackError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        events = seed_history(pack.manifest, Rng(seed))
        store = build_store()
        await store.connect()
        try:
            world = await store.get_world_by_name(pack.manifest.name)
            if world is None:
                typer.echo(
                    f"no imported world named {pack.manifest.name!r} — "
                    f"run `uro world create {path}` first",
                    err=True,
                )
                raise typer.Exit(1)
            commit = await store.append_beat(world.main_branch_id, events)
        finally:
            await store.close()
        dynasties = sum(1 for e in events if e.event_type == "FactionCreated")
        wars = sum(
            1
            for e in events
            if e.event_type == "EdgeAdded" and e.payload.get("rel_type") == "at_war_with"
        )
        typer.echo(
            f"seeded {pack.manifest.name!r} with seed {seed} → commit {commit.commit_id[:8]}"
        )
        typer.echo(f"  {dynasties} dynasties, {wars} wars (on the pack's authored geography)")

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
        await store.connect()
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
        await store.connect()
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
        await store.connect()
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
                else build_ruleset(campaign.ruleset_id, campaign.ruleset_version),
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
    provider: str = typer.Option("stub", help="stub | local | openai | anthropic"),
    model: str = typer.Option(None, help="model id for the provider"),
    ruleset: str = typer.Option(
        "", help="ruleset id for this server process (default: uro-basic via the registry)"
    ),
    arbiter_kind: str = typer.Option(
        "party",
        "--arbiter",
        help="multi-token turn shape (D-38): party (round-robin) | proposal (propose-then-act, "
        "QUEUED) | vote (consensus). Ignored with a single token (solo).",
    ),
) -> None:
    """Run the Uro server (docs/08): a thin FastAPI shell over the engine. Each --token maps to
    a participant; with no --token, a single 'local' token binds player-1 (the solo dev loop).

    NOTE (PoC limitation): the server binds ONE ruleset per process (--ruleset), since its single
    Engine is shared across campaigns. Per-campaign rebinding (as the `play`/`dry-run` CLI paths
    do from campaign.ruleset_id) needs the Engine to resolve the ruleset per beat — deferred."""
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
    store = build_store()
    engine = Engine(store, build_router(provider, model), ruleset=build_ruleset(ruleset))
    # More than one token → a party; pick the turn shape (OQ-7, D-31/D-38). One token → solo
    # (create_app defaults to SoloArbiter). Seat each participant's PC via `uro campaign join`.
    shapes: dict[str, type[TurnArbiter]] = {
        "party": PartyArbiter,
        "proposal": ProposalWindowArbiter,
        "vote": VoteArbiter,
    }
    if arbiter_kind not in shapes:
        raise typer.BadParameter(
            f"--arbiter must be one of {', '.join(shapes)} (got {arbiter_kind!r})"
        )
    arbiter = shapes[arbiter_kind]() if len(tokens) > 1 else None
    fastapi_app = create_app(engine_deps(store, engine, tokens), arbiter=arbiter)

    @fastapi_app.on_event("startup")
    async def _startup() -> None:
        await store.connect()

    @fastapi_app.on_event("shutdown")
    async def _shutdown() -> None:
        await store.close()

    shape = arbiter_kind if len(tokens) > 1 else "solo"
    typer.echo(
        f"uro server on ws://{host}:{port}  "
        f"({len(tokens)} token(s), provider={provider}, arbiter={shape})"
    )
    for t, p in tokens.items():
        typer.echo(f"  token {t!r} → {p}")
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


@branch_app.command("list")
def branch_list(world: str) -> None:
    """List a world's branches (with head depth) and its markers."""

    async def _run() -> None:
        store = build_store()
        await store.connect()
        try:
            w = await _world_or_exit(store, world)
            branches = await store.list_branches(w.world_id)
            markers = await store.list_markers(w.world_id)
        finally:
            await store.close()
        typer.echo(f"world: {w.world_id}  ({w.name})")
        typer.echo("branches:")
        for b in branches:
            forked = f"  forked@{b.forked_from[:8]}" if b.forked_from else ""
            head = b.head_commit[:8] if b.head_commit else "-"
            typer.echo(f"  {b.name:<16} head={head} depth={b.head_depth}{forked}")
        if markers:
            typer.echo("markers:")
            for m in markers:
                typer.echo(f"  {m.name:<16} → {m.commit_id[:8]}")

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
        await store.connect()
        try:
            w = await _world_or_exit(store, world)
            branch = await store.fork_branch(w.world_id, at, name)
            if time_skip_days > 0:
                # agenda_tick time-skips AND fires the world's downtime agenda rules (docs/17); it
                # needs no LLM, so a stub router suffices. A rule-less world → a plain time-skip.
                engine = Engine(store, build_router("stub", None))
                await engine.agenda_tick(branch.branch_id, time_skip_days)
        finally:
            await store.close()
        forked = branch.forked_from[:8] if branch.forked_from else "-"
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
        await store.connect()
        try:
            w = await _world_or_exit(store, world)
            b = await store.get_branch_by_name(w.world_id, branch)
            if b is None:
                typer.echo(f"no such branch: {branch}", err=True)
                raise typer.Exit(1)
            marker = await store.create_marker(w.world_id, name, b.branch_id)
        finally:
            await store.close()
        typer.echo(f"marker {marker.name!r} → commit {marker.commit_id[:8]} on branch {branch!r}")

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
        await store.connect()
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
            typer.echo(f"  {e.depth:>4} {e.commit_id[:8]}  {e.summary}{marks}")

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
        await store.connect()
        try:
            w = await _world_or_exit(store, world)
            b = await store.get_branch_by_name(w.world_id, branch)
            if b is None:
                typer.echo(f"no such branch: {branch}", err=True)
                raise typer.Exit(1)
            # Bind the WORLD's declared ruleset (docs/06, D-30): a PbtA world's campaign is
            # played under uro_pbta, not the hard-coded default. ('' → registry default.)
            world_rid, world_rver = await store.world_ruleset(b.branch_id)
            # Every PC needs a sheet so the mechanics gate can check it. A fresh PC gets one; an
            # adopted actor is sheeted only if it lacks one (a re-adopted PC keeps its sheet).
            pc_sheet = None
            ruleset_id = ""
            if pc is not None or (
                adopt is not None and await store.get_sheet(b.branch_id, adopt) is None
            ):
                pc_sheet, ruleset_id = _build_pc_sheet(world_rid, world_rver)
            else:
                ruleset_id = build_ruleset(world_rid, world_rver).id
            campaign = await store.start_campaign(
                w.world_id,
                b.branch_id,
                participant_id=participant,
                adopt_actor_id=adopt,
                new_pc_name=pc,
                pc_sheet=pc_sheet,
                starting_items=["a traveler's knife"] if pc is not None else None,
                ruleset_id=ruleset_id,
                ruleset_version=world_rver,
                seed=seed,
            )
        finally:
            await store.close()
        who = f"adopted {adopt}" if adopt else f"new PC {pc!r}"
        typer.echo(f"campaign: {campaign.campaign_id}  (branch {branch!r}, {who})")
        typer.echo(f"play it:  uro play {campaign.campaign_id}")

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
        await store.connect()
        try:
            m = await store.end_campaign(campaign_id, marker, outcome=outcome)
        finally:
            await store.close()
        typer.echo(f"campaign ended; marker {m.name!r} → commit {m.commit_id[:8]}")

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
        await store.connect()
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
                pc_sheet, _ = _build_pc_sheet(campaign.ruleset_id, campaign.ruleset_version)
            actor = await store.bind_pc(
                campaign_id,
                participant,
                adopt_actor_id=adopt,
                new_pc_name=pc,
                pc_sheet=pc_sheet,
                starting_items=["a traveler's knife"] if pc is not None else None,
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
        await store.connect()
        try:
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                typer.echo(f"no such campaign: {campaign_id}", err=True)
                raise typer.Exit(1)
            engine = Engine(
                store,
                build_router(provider, model, role_models),
                ruleset=build_ruleset(campaign.ruleset_id, campaign.ruleset_version),
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
        await store.connect()
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
        await store.connect()
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
        await store.connect()
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
