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

from uro_cli.wiring import build_router, build_ruleset, build_store

app = typer.Typer(no_args_is_help=True, help="Uro Engine — reference client.")
db_app = typer.Typer(no_args_is_help=True, help="Database management.")
world_app = typer.Typer(no_args_is_help=True, help="World and campaign management.")
branch_app = typer.Typer(no_args_is_help=True, help="Branch and timeline management (docs/03).")
campaign_app = typer.Typer(no_args_is_help=True, help="Campaign lifecycle over branches.")
app.add_typer(db_app, name="db")
app.add_typer(world_app, name="world")
app.add_typer(branch_app, name="branch")
app.add_typer(campaign_app, name="campaign")

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


def _build_pc_sheet() -> tuple[dict[str, Any], str]:
    """A default character sheet from the bound ruleset, so every PC can be checked (docs/06)."""
    ruleset = build_ruleset()
    return ruleset.new_character(CharSpec(), Rng(0)).model_dump(), ruleset.id


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
        from uro_core.worldpack.backfill import backfill_gaps
        from uro_core.worldpack.importer import pack_to_events
        from uro_core.worldpack.parse import parse_pack
        from uro_core.worldpack.sufficiency import check_sufficiency

        try:
            pack = parse_pack(path)
        except PackError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
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
            # No ruleset in bare (ablation) mode — the planner/gate are exactly what it ablates.
            engine = Engine(
                store,
                build_router(provider, model),
                ruleset=None if bare else build_ruleset(),
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
                await store.time_skip(branch.branch_id, time_skip_days)
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
            # Every PC needs a sheet so the mechanics gate can check it (docs/06). A fresh PC
            # gets one; an adopted actor is sheeted only if it lacks one (a re-adopted former
            # PC keeps the sheet carried on its branch).
            pc_sheet = None
            ruleset_id = ""
            if pc is not None or (
                adopt is not None and await store.get_sheet(b.branch_id, adopt) is None
            ):
                pc_sheet, ruleset_id = _build_pc_sheet()
            campaign = await store.start_campaign(
                w.world_id,
                b.branch_id,
                participant_id=participant,
                adopt_actor_id=adopt,
                new_pc_name=pc,
                pc_sheet=pc_sheet,
                starting_items=["a traveler's knife"] if pc is not None else None,
                ruleset_id=ruleset_id,
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


@app.command("dry-run")
def dry_run(
    campaign_id: str,
    intent: str,
    provider: str = typer.Option("stub", help="stub | local | openai | anthropic"),
    model: str = typer.Option(None, help="model id for local/openai/anthropic providers"),
) -> None:
    """Dry-run a beat (docs/09 creator loop): show the events it WOULD commit, without writing.
    Nothing enters the log — the campaign is untouched."""

    async def _run() -> None:
        store = build_store()
        await store.connect()
        try:
            campaign = await store.get_campaign(campaign_id)
            if campaign is None:
                typer.echo(f"no such campaign: {campaign_id}", err=True)
                raise typer.Exit(1)
            engine = Engine(store, build_router(provider, model), ruleset=build_ruleset())
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
