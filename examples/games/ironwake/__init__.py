"""IRONWAKE — a grid-tactics mercenary company game built ON the Uro engine.

Battle Brothers / Darkest Dungeon, but the world remembers: the game runs its own seeded,
deterministic skirmishes (game/), reports every outcome to Uro through the Chronicler contract
(world/chronicle.py), and lets Uro decide what the world believes — who really died, which feats
became rumors, how far the tale traveled, and what was refused.

IRONWAKE consumes `uro_core` as a read-only dependency (Posture A embed by default; `--posture
server` drives the same contract over HTTP/WS). It is also a forcing function: every place the
engine surface refused, surprised, or forced state into game code is logged at the call site
(frictionlog.py) and assembled into GAP_REPORT.md.

Run it (from `examples/games/`):  uv run python -m ironwake.cli play --seed 7
"""
