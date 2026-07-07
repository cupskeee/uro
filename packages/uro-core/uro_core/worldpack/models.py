"""World-pack data models (docs/09). Pydantic — the manifest + seed schemas ARE the format.

A pack is a directory: `world.toml` (manifest), `entities/*.yaml` (seed places/factions/
actors/threads/claims), `lore/*.md` (freeform), `prompts/*.j2` (template overrides). These
models validate the parsed content; parse.py assembles a `WorldPack` from a directory.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Rating = Literal["none", "mild", "mature", "explicit"]
PlaceKind = Literal["region", "settlement", "site"]

# Canonical content-dimension vocabulary shared with the probe (docs/04, 09).
CONTENT_DIMENSIONS: tuple[str, ...] = ("violence", "horror", "sexual_content", "profanity")


# --- manifest (world.toml) ---


class ContentDecl(BaseModel):
    rating: Rating = "mild"  # intensity CEILING
    enabled: list[str] = Field(default_factory=list)  # categories in play
    disabled: list[str] = Field(default_factory=list)  # categories excluded (declaration only)


class CalendarDecl(BaseModel):
    days_per_year: int = 360
    seasons: list[str] = Field(default_factory=list)
    epoch_label: str = ""


class RulesetDecl(BaseModel):
    id: str = "uro-basic"
    version: str = ">=0"
    config: dict[str, Any] = Field(default_factory=dict)


class HistoryDecl(BaseModel):
    seed_era: str = ""
    simulate_years: int = 0


class WorldManifest(BaseModel):
    name: str
    tone: list[str] = Field(default_factory=list)
    generate_population: bool = False  # the "generate freely" flag (docs/09 sufficiency)
    content: ContentDecl = Field(default_factory=ContentDecl)
    calendar: CalendarDecl = Field(default_factory=CalendarDecl)
    ruleset: RulesetDecl = Field(default_factory=RulesetDecl)
    history: HistoryDecl = Field(default_factory=HistoryDecl)
    llm_roles: dict[str, str] = Field(default_factory=dict)  # [llm.roles] — suggestions


# --- seed entities (entities/*.yaml) ---


class PlaceSeed(BaseModel):
    id: str
    name: str
    kind: PlaceKind = "site"
    description: str = ""
    parent: str | None = None  # located_in ref (region ⊃ settlement ⊃ site)


class FactionSeed(BaseModel):
    id: str
    name: str
    kind: str = "faction"  # "religion" for a religion (docs/02)
    description: str = ""
    at_war_with: list[str] = Field(default_factory=list)  # faction ids → at_war_with edges


class ActorSeed(BaseModel):
    id: str
    name: str
    tier: int = Field(default=1, ge=0, le=3)
    role: str = ""
    aliases: list[str] = Field(default_factory=list)
    faction: str | None = None  # member_of ref
    location: str | None = None  # located_in ref


class ThreadSeed(BaseModel):
    """A conflict seed / tension hook (docs/09 sufficiency) — something to play about."""

    id: str
    stakes: str
    state: Literal["dormant", "offered", "active"] = "dormant"
    provenance: str = "author"  # "ai_backfill" for machine-generated seeds (docs/09)


class ClaimSeed(BaseModel):
    id: str
    statement: str
    truth: Literal["true", "false", "unknown"] = "true"
    subject_refs: list[str] = Field(default_factory=list)


class WorldPack(BaseModel):
    """A parsed, validated pack ready for sufficiency-checking and import."""

    manifest: WorldManifest
    places: list[PlaceSeed] = Field(default_factory=list)
    factions: list[FactionSeed] = Field(default_factory=list)
    actors: list[ActorSeed] = Field(default_factory=list)
    threads: list[ThreadSeed] = Field(default_factory=list)
    claims: list[ClaimSeed] = Field(default_factory=list)
    lore: dict[str, str] = Field(default_factory=dict)  # relative path → markdown
    prompts: dict[str, str] = Field(default_factory=dict)  # filename → template override
