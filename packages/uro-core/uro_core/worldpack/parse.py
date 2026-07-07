"""Parse a world-pack directory into a validated `WorldPack` (docs/09).

`world.toml` (tomllib) + `entities/*.yaml` (PyYAML) + `lore/*.md` + `prompts/*.j2`. Schema
violations raise `PackError` with an author-actionable message. No LLM here — this is the
deterministic front of the import pipeline; LLM lore-extraction/backfill layer on top (4.4).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from uro_core.errors import PackError
from uro_core.worldpack.models import (
    ActorSeed,
    CalendarDecl,
    ClaimSeed,
    ContentDecl,
    FactionSeed,
    HistoryDecl,
    PlaceSeed,
    RulesetDecl,
    ThreadSeed,
    WorldManifest,
    WorldPack,
)


def parse_pack(root: str | Path) -> WorldPack:
    """Load and validate a pack directory (or an already-extracted `.uwp`)."""
    root = Path(root)
    if not root.is_dir():
        raise PackError(f"{root} is not a directory")
    return WorldPack(
        manifest=_manifest(root / "world.toml"),
        places=_seeds(root, "places.yaml", PlaceSeed),
        factions=_seeds(root, "factions.yaml", FactionSeed),
        actors=_seeds(root, "actors.yaml", ActorSeed),
        threads=_seeds(root, "threads.yaml", ThreadSeed),
        claims=_seeds(root, "claims.yaml", ClaimSeed),
        lore=_read_tree(root / "lore", ".md"),
        prompts=_read_dir(root / "prompts", ".j2"),
    )


def _manifest(path: Path) -> WorldManifest:
    if not path.is_file():
        raise PackError(f"missing world.toml at {path}")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise PackError(f"world.toml is not valid TOML: {exc}") from exc
    world = data.get("world", {})
    if "name" not in world:
        raise PackError("world.toml is missing [world].name")
    try:
        return WorldManifest(
            name=world["name"],
            tone=world.get("tone", []),
            generate_population=world.get("generate_population", False),
            content=ContentDecl(**data.get("content", {})),
            calendar=CalendarDecl(**data.get("calendar", {})),
            ruleset=RulesetDecl(**data.get("ruleset", {})),
            history=HistoryDecl(**data.get("history", {})),
            llm_roles=data.get("llm", {}).get("roles", {}),
        )
    except ValidationError as exc:
        raise PackError(f"world.toml failed validation: {exc}") from exc


def _seeds[T: BaseModel](root: Path, filename: str, model: type[T]) -> list[T]:
    path = root / "entities" / filename
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PackError(f"entities/{filename} must be a YAML list of entities")
    try:
        return [model(**item) for item in raw]
    except (ValidationError, TypeError) as exc:
        raise PackError(f"entities/{filename}: {exc}") from exc


def _read_tree(directory: Path, suffix: str) -> dict[str, str]:
    """All files with `suffix` under `directory` (recursively), keyed by relative path."""
    if not directory.is_dir():
        return {}
    return {
        str(p.relative_to(directory)): p.read_text()
        for p in sorted(directory.rglob(f"*{suffix}"))
        if p.is_file()
    }


def _read_dir(directory: Path, suffix: str) -> dict[str, str]:
    """Files with `suffix` directly in `directory`, keyed by filename."""
    if not directory.is_dir():
        return {}
    return {p.name: p.read_text() for p in sorted(directory.glob(f"*{suffix}")) if p.is_file()}
