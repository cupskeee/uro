"""World-pack data models (docs/09). Pydantic — the manifest + seed schemas ARE the format.

A pack is a directory: `world.toml` (manifest), `entities/*.yaml` (seed places/factions/
actors/threads/claims), `lore/*.md` (freeform), `prompts/*.j2` (template overrides). These
models validate the parsed content; parse.py assembles a `WorldPack` from a directory.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from uro_core.pipeline.pacing import PacingSettings
from uro_core.worldpack.rules import RulePack

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


class ClockDecl(BaseModel):
    """`[clock]` (D-80): what moves the world's day besides an explicit time-skip. The day itself is
    world state the engine owns; which policies move it is the game's choice."""

    model_config = ConfigDict(extra="forbid")  # a misspelt setting is an error, not a no-op
    from_intent: bool = True  # an intent that passes whole days moves the day (D-77)


class RulesetDecl(BaseModel):
    id: str = "uro-basic"
    version: str = ">=0"
    config: dict[str, Any] = Field(default_factory=dict)


class HistoryVocabulary(BaseModel):
    """`[history.vocabulary]`: the words History seeding builds a past from. The engine ships no
    genre of its own (no kings, no houses): a pack that wants seeding brings its words."""

    model_config = ConfigDict(extra="forbid")
    groups: list[str] = Field(min_length=1)  # names for the power groups seeding creates
    group_name: str = "{name}"  # e.g. "House {name}", "{name} Holdings"
    group_description: str = "Risen in {era}."  # {era}: "the age of <seed_era>"
    leaders: list[str] = Field(min_length=1)  # each group's leader is one of these…
    titles: list[str] = Field(default_factory=list)  # …with one of these before it, if any
    leader_role: str = "leader"
    calamities: list[str] = Field(min_length=1)  # what marked the era ("a great plague")
    # The sentences seeding writes, in the pack's language (the defaults are English): the era's
    # name ({era} = `seed_era`), its name when the pack has no `seed_era`, and the era's claim
    # ({era_name}, {calamity}; its first letter is capitalized).
    era_name: str = "the age of {era}"
    era_unnamed: str = "its past"
    era_claim: str = "{era_name} was marked by {calamity}."

    @model_validator(mode="after")
    def _usable(self) -> HistoryVocabulary:
        lists = {"groups": self.groups, "leaders": self.leaders, "titles": self.titles}
        lists["calamities"] = self.calamities
        blank = [k for k, v in lists.items() if any(not w.strip() for w in v)]
        if blank:
            raise ValueError(f"[history.vocabulary] has a blank entry in {', '.join(blank)}")
        if "{name}" not in self.group_name:
            raise ValueError("[history.vocabulary] group_name must contain {name}")
        return self


class HistoryDecl(BaseModel):
    seed_era: str = ""
    simulate_years: int = 0
    vocabulary: HistoryVocabulary | None = None  # none → the world can't be seeded (D-97)


class WorldManifest(BaseModel):
    name: str
    tone: list[str] = Field(default_factory=list)
    # A short pitch for whoever lists the world (D-104): description only, never sent to a model.
    description: str = ""
    generate_population: bool = False  # the "generate freely" flag (docs/09 sufficiency)
    # `[world] starting_gear` (D-97): items a fresh PC carries in this world; none by default
    starting_gear: list[str] = Field(default_factory=list)
    content: ContentDecl = Field(default_factory=ContentDecl)
    calendar: CalendarDecl = Field(default_factory=CalendarDecl)
    clock: ClockDecl = Field(default_factory=ClockDecl)
    # `[pacing]` (D-95, D-96): whether and when the engine names a quiet plot to the narrator —
    # off unless `quiet_after` (beats) is set; at most once every `every` beats. Unknown keys are
    # refused. The line's wording is the pack's `prompts/pacing.j2` (else the default).
    pacing: PacingSettings = Field(default_factory=PacingSettings)
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
    # Actor ids this actor knows (D-76): acquaintance is mutual, so each becomes a `knows` edge
    # both ways, which is the only relation rumors spread along (engines/actor.propagate_belief).
    knows: list[str] = Field(default_factory=list)


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
    rule_pack: RulePack | None = None  # rules.yaml/agendas.yaml — the Reaction Layer (docs/17)

    @model_validator(mode="after")
    def _knows_refs_resolve(self) -> WorldPack:
        """A `knows` ref must name another actor in the pack (D-76): an edge to nobody would be
        accepted and do nothing, so a typo silently cuts a rumor's path."""
        ids = {a.id for a in self.actors}
        for a in self.actors:
            for other in a.knows:
                if other == a.id:
                    raise ValueError(f"actor {a.id!r} lists itself in `knows`")
                if other not in ids:
                    raise ValueError(f"actor {a.id!r} knows {other!r}, which is not an actor")
        return self
