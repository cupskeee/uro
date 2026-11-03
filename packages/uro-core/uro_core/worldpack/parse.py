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
    ClockDecl,
    ContentDecl,
    FactionSeed,
    HistoryDecl,
    PlaceSeed,
    RulesetDecl,
    ThreadSeed,
    WorldManifest,
    WorldPack,
)
from uro_core.worldpack.rules import RulePack


def parse_pack(root: str | Path) -> WorldPack:
    """Load and validate a pack directory (or an already-extracted `.uwp`)."""
    root = Path(root)
    if not root.is_dir():
        raise PackError(f"{root} is not a directory")
    parts = {
        "manifest": _manifest(root / "world.toml"),
        "places": _seeds(root, "places.yaml", PlaceSeed),
        "factions": _seeds(root, "factions.yaml", FactionSeed),
        "actors": _seeds(root, "actors.yaml", ActorSeed),
        "threads": _seeds(root, "threads.yaml", ThreadSeed),
        "claims": _seeds(root, "claims.yaml", ClaimSeed),
        "lore": _read_tree(root / "lore", ".md"),
        "prompts": _read_dir(root / "prompts", ".j2"),
        "rule_pack": _rule_pack(root),
    }
    try:  # the cross-references between seeds (a `knows` naming nobody, D-76)
        return WorldPack(**parts)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise PackError(f"pack failed validation: {exc}") from exc


def _rule_pack(root: Path) -> RulePack | None:
    """Load `rules.yaml` / `agendas.yaml` (beside `world.toml`) into a validated `RulePack`, or
    None if the pack ships neither. Fails LOUD on a schema violation or an unsupported
    `rules_api_version` — an explicit, enforced version pin (decided-OQ #5, docs/17)."""
    rules_path, agendas_path = root / "rules.yaml", root / "agendas.yaml"
    if not rules_path.is_file() and not agendas_path.is_file():
        return None
    data: dict[str, object] = {}
    for path, key in ((rules_path, "rules"), (agendas_path, "agendas")):
        if not path.is_file():
            continue
        raw = _load_yaml(path) or {}
        if not isinstance(raw, dict):
            raise PackError(f"{path.name} must be a YAML mapping (with rules_api_version + {key})")
        # Any other key would be read by nothing: an `agendas:` list written into rules.yaml was
        # accepted and never ran (found authoring D-76's content). Refuse it, naming the right file.
        stray = sorted(set(raw) - {"rules_api_version", key}, key=str)  # YAML keys may be ints
        if stray:
            where = {"rules": "rules.yaml", "agendas": "agendas.yaml"}
            hint = "; ".join(f"`{k}` goes in {where[k]}" for k in stray if k in where)
            raise PackError(
                f"{path.name} has {', '.join(f'`{k}`' for k in stray)}, which it doesn't read"
                + (f" ({hint})" if hint else "")
            )
        # A file may leave the version to the other one (as before D-76); two that both declare one
        # must agree.
        if "rules_api_version" in raw:
            if data.get("rules_api_version") not in (None, raw["rules_api_version"]):
                raise PackError(
                    f"rules.yaml and agendas.yaml declare different rules_api_version "
                    f"({data['rules_api_version']} and {raw['rules_api_version']})"
                )
            data["rules_api_version"] = raw["rules_api_version"]
        else:
            data.setdefault("rules_api_version", None)
        data[key] = raw.get(key, [])
    try:
        # RulePack's model validator enforces the supported-version set (v1..v3; v3 adds multi-ref
        # scopes, D-40) + the closed grammar — one authority for parse, runtime, and import.
        return RulePack(**data)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise PackError(f"rule pack failed validation: {exc}") from exc


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
            clock=ClockDecl(**data.get("clock", {})),
            ruleset=RulesetDecl(**data.get("ruleset", {})),
            history=HistoryDecl(**data.get("history", {})),
            llm_roles=data.get("llm", {}).get("roles", {}),
        )
    except ValidationError as exc:
        raise PackError(f"world.toml failed validation: {exc}") from exc


def _load_yaml(path: Path) -> object:
    """A pack's YAML, with a syntax error reported as a PackError (bad INPUT, never a crash)."""
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PackError(f"{path.name} is not valid YAML: {exc}") from exc


def _seeds[T: BaseModel](root: Path, filename: str, model: type[T]) -> list[T]:
    path = root / "entities" / filename
    if not path.is_file():
        return []
    raw = _load_yaml(path)
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
