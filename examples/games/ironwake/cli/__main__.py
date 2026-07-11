"""IRONWAKE entry point.

    uv run python -m ironwake.cli play --seed 7                # the full season (embed, stub)
    uv run python -m ironwake.cli play --seed 7 --posture server
    uv run python -m ironwake.cli play --seed 7 --provider openai --model gpt-4o  # opt-in key
    uv run python -m ironwake.cli seed                          # inc-0 verify: seed + read back
    uv run python -m ironwake.cli battle --seed 7 [--verbose]   # inc-1 verify: one headless fight

Run from examples/games/ (so `ironwake` is importable), with Postgres up on host port 5433.
"""

from __future__ import annotations

import argparse
import asyncio


def main() -> None:
    parser = argparse.ArgumentParser(prog="ironwake", description="IRONWAKE — the world remembers")
    sub = parser.add_subparsers(dest="command", required=True)

    play = sub.add_parser("play", help="play a full season")
    play.add_argument("--seed", type=int, default=7)
    play.add_argument("--posture", choices=("embed", "server"), default="embed")
    play.add_argument("--provider", choices=("stub", "openai", "anthropic"), default="stub")
    play.add_argument("--model", default=None)
    play.add_argument("--dsn", default=None)
    play.add_argument("--verbose", action="store_true", help="print every die roll")

    seed_cmd = sub.add_parser("seed", help="seed the Marches and print the world read-back")
    seed_cmd.add_argument("--seed", type=int, default=7)
    seed_cmd.add_argument("--dsn", default=None)

    battle = sub.add_parser("battle", help="one headless deterministic skirmish, no Uro")
    battle.add_argument("--seed", type=int, default=7)
    battle.add_argument("--verbose", action="store_true")

    args = parser.parse_args()
    if args.command == "play":
        from ironwake.cli.season import run_season

        asyncio.run(
            run_season(
                seed=args.seed,
                posture=args.posture,
                provider=args.provider,
                model=args.model,
                dsn=args.dsn,
                verbose=args.verbose,
            )
        )
    elif args.command == "seed":
        asyncio.run(_seed(args.seed, args.dsn))
    elif args.command == "battle":
        _battle(args.seed, args.verbose)


async def _seed(season_seed: int, dsn: str | None) -> None:
    from ironwake.world.setup import describe_world, seed_world
    from ironwake.world.uro import DEFAULT_DSN, UroSession

    session = await UroSession.connect(dsn or DEFAULT_DSN)
    try:
        marches = await seed_world(session.store, season_seed=season_seed)
        print(f"seeded world {marches.world.world_id} (branch {marches.branch_id})")
        print(await describe_world(session.store, marches.branch_id))
    finally:
        await session.close()


def _battle(seed: int, verbose: bool) -> None:
    """Inc-1 verification: a fixed scenario, resolved headless twice — the digests must match."""
    from ironwake.game.scenarios import GRANARY, build_battle
    from ironwake.world.setup import STARTING_MERCS

    def run_once() -> tuple[str, tuple[str, ...]]:
        b = build_battle(
            GRANARY,
            list(STARTING_MERCS),
            [f"a:rb-demo-{i}" for i in range(len(GRANARY.enemies))],
            seed,
        )
        report = b.run()
        return report.digest(), report.log

    digest_a, log = run_once()
    digest_b, _ = run_once()
    if verbose:
        for line in log:
            print(line)
    else:
        for line in log:
            if line.startswith(("==", "--")) or "**" in line or "<<" in line:
                print(line)
    print(f"\ndigest: {digest_a}")
    print(f"same seed, second run: {'IDENTICAL' if digest_a == digest_b else 'DIVERGED (BUG)'}")


if __name__ == "__main__":
    main()
