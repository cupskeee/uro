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


def _run_async(coro_factory) -> None:  # type: ignore[no-untyped-def]
    """Run an async command, turning config/credential errors into a clean message."""
    try:
        asyncio.run(coro_factory())
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


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
