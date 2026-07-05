"""Entry point for the `uro` command. Command surface per docs/08-api-and-sessions.md.

Phase 0 subset: version, db migrate, world new, play.
"""

from __future__ import annotations

import asyncio
import sys

import typer
from uro_core.pipeline.engine import Engine

from uro_cli.wiring import build_router, build_store

app = typer.Typer(no_args_is_help=True, help="Uro Engine — reference client.")
db_app = typer.Typer(no_args_is_help=True, help="Database management.")
world_app = typer.Typer(no_args_is_help=True, help="World and campaign management.")
app.add_typer(db_app, name="db")
app.add_typer(world_app, name="world")

PARTICIPANT = "player-1"  # Phase 0 is single-player; participants arrive in Phase 5.


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

    asyncio.run(_run())


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

    asyncio.run(_run())


@app.command()
def play(
    campaign_id: str,
    provider: str = typer.Option("stub", help="stub | local | openai"),
    model: str = typer.Option(None, help="model id for local/openai providers"),
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
            engine = Engine(store, build_router(provider, model))

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
                async for chunk in engine.run_beat_stream(campaign, PARTICIPANT, intent):
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                sys.stdout.write("\n")
        finally:
            await store.close()

    asyncio.run(_run())
