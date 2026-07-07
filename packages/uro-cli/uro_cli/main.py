"""Entry point for the `uro` command. Command surface per docs/08-api-and-sessions.md.

Phase 0 subset: version, db migrate, world new, play.
"""

from __future__ import annotations

import asyncio
import sys

import typer
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.pipeline.engine import Engine
from uro_core.timeline.models import World

from uro_cli.wiring import build_router, build_store

app = typer.Typer(no_args_is_help=True, help="Uro Engine — reference client.")
db_app = typer.Typer(no_args_is_help=True, help="Database management.")
world_app = typer.Typer(no_args_is_help=True, help="World and campaign management.")
branch_app = typer.Typer(no_args_is_help=True, help="Branch and timeline management (docs/03).")
app.add_typer(db_app, name="db")
app.add_typer(world_app, name="world")
app.add_typer(branch_app, name="branch")

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
    """Create a world (+ its main branch) and a campaign to play it. Prints the campaign id."""

    async def _run() -> None:
        store = build_store()
        await store.connect()
        try:
            world = await store.create_world(name)
            campaign = await store.create_campaign(world.world_id, world.main_branch_id)
        finally:
            await store.close()
        typer.echo(f"world:    {world.world_id}  ({name})")
        typer.echo(f"campaign: {campaign.campaign_id}")
        typer.echo(f"\nplay it:  uro play {campaign.campaign_id}")

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
) -> None:
    """Interactive play loop. Type an action; '/quit' to leave. State persists to Postgres."""

    async def _run() -> None:
        store = build_store()
        await store.connect()
        try:
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                typer.echo(f"no such campaign: {campaign_id}", err=True)
                raise typer.Exit(1)
            engine = Engine(store, build_router(provider, model), bare=bare)

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
) -> None:
    """Fork a new branch from any commit or marker (docs/03: branch anywhere)."""

    async def _run() -> None:
        store = build_store()
        await store.connect()
        try:
            w = await _world_or_exit(store, world)
            branch = await store.fork_branch(w.world_id, at, name)
        finally:
            await store.close()
        forked = branch.forked_from[:8] if branch.forked_from else "-"
        typer.echo(f"forked branch {name!r}: {branch.branch_id}")
        typer.echo(f"  from {forked}  (head = {forked})")

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
