"""A campaign's game and its players' characters (D-112): one implementation both surfaces call
(the CLI and the server, D-60).

- **A campaign may choose its own game.** At start it may name a ruleset and settings, validated by
  that ruleset as a pack's are; the campaign pins both as a snapshot. Named neither, a campaign on a
  fork carries the pin of the story the fork came from (`inherited_ruleset_pin`), and any other
  plays its world's, as before.
- **A character is built from a spec.** The spec is opaque, handed to the pinned ruleset's
  `new_character`; a spec it can't build is refused with its own located problems
  (`CharSpecError`). A preview runs the same build and writes nothing.
- **A build stays open until the story starts.** A PC's spec and name may change until the first
  beat on its branch after it was bound; after that only play changes a sheet. The store checks it
  and commits the change in one transaction (`amend_character`).

This is a composition module (it resolves rulesets through the registry), outside the core ring.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from uro_core.domain.events import CausedBy, actor_renamed, sheet_updated
from uro_core.ports.projections import EngineStore
from uro_core.providers.base import Message
from uro_core.rulesets.base import (
    CharSpec,
    CharSpecError,
    Ruleset,
    Sheet,
    SpecProblem,
    build_character,
    spec_schema_of,
)
from uro_core.rulesets.registry import DEFAULT_RULESET_ID, resolve, resolve_for_campaign
from uro_core.rulesets.rng import Rng
from uro_core.timeline.gear import starting_gear
from uro_core.timeline.models import Campaign, RulesetPin

__all__ = [
    "CharSpecError",
    "CharacterFrozen",
    "CharacterView",
    "Preview",
    "campaign_game",
    "character_sheet",
    "join_campaign",
    "own_character",
    "preview_character",
    "propose_character",
    "rebuild_character",
    "start_campaign",
]


class CharacterFrozen(ValueError):
    """A character's build can no longer change (D-112); the message says why."""


class Game(BaseModel):
    """What a new campaign will play: the ruleset id and version to pin, and the settings it pins
    for itself (`own`, a snapshot), or None to follow its world's."""

    model_config = {"arbitrary_types_allowed": True}

    ruleset: Any  # a bound Ruleset
    pin: RulesetPin
    own: dict[str, Any] | None = None


class Preview(BaseModel):
    """A character build checked without writing anything: the sheet it would make, or why not."""

    ok: bool
    ruleset: str
    sheet: Sheet | None = None
    problems: list[SpecProblem] = Field(default_factory=list)


class CharacterView(BaseModel):
    """A participant's own character (D-112): its sheet as the ruleset shapes it and what it holds,
    and whether its build may still change. Nothing of anyone else's."""

    campaign_id: str
    participant_id: str
    actor_id: str
    name: str
    ruleset: str
    sheet: Sheet | None = None
    holdings: list[dict[str, Any]] = Field(default_factory=list)
    build_open: bool
    frozen_reason: str = ""


async def campaign_game(
    store: EngineStore,
    branch_id: str,
    *,
    ruleset_id: str | None = None,
    ruleset_config: dict[str, Any] | None = None,
) -> Game:
    """The game a new campaign on `branch_id` will play. Given a ruleset or settings, that (the
    settings default to the world's when the ruleset is the world's, else to the ruleset's own);
    given neither, the story a fork came from, else the world's. Invalid settings or an unknown
    ruleset raise as `resolve` does (ValueError / KeyError)."""
    world = await store.world_ruleset(branch_id)
    if ruleset_id is None and ruleset_config is None:
        inherited = await store.inherited_ruleset_pin(branch_id)
        if inherited is not None:
            rs = resolve(inherited.id, inherited.version, inherited.config)
            pin = RulesetPin(id=rs.id, version=inherited.version, config=inherited.config)
            return Game(ruleset=rs, pin=pin, own=dict(inherited.config))
        rs = resolve(world.id, world.version, world.config)
        return Game(
            ruleset=rs, pin=RulesetPin(id=rs.id, version=world.version, config=world.config)
        )
    rid = ruleset_id if ruleset_id is not None else world.id
    same = (rid or DEFAULT_RULESET_ID) == (world.id or DEFAULT_RULESET_ID)
    config = dict(ruleset_config) if ruleset_config is not None else (world.config if same else {})
    rs = resolve(rid, "", config)
    return Game(ruleset=rs, pin=RulesetPin(id=rs.id, version=rs.version, config=config), own=config)


def character_sheet(ruleset: Ruleset, spec: dict[str, Any] | None = None) -> Sheet:
    """A PC's sheet from an opaque spec (None or {} = the ruleset's default character). A spec the
    ruleset can't build raises `CharSpecError` with its problems."""
    if spec is not None and not isinstance(spec, dict):
        raise CharSpecError([SpecProblem(message="a spec is a JSON object")])
    return build_character(ruleset, CharSpec(data=spec or {}), Rng(0))


async def _adopted_sheeted(store: EngineStore, branch_id: str, adopt_actor_id: str | None) -> bool:
    return (
        adopt_actor_id is not None and await store.get_sheet(branch_id, adopt_actor_id) is not None
    )


def _no_spec_for_a_sheeted_actor() -> CharSpecError:
    return CharSpecError(
        [SpecProblem(message="an adopted character keeps the sheet it already has")]
    )


async def start_campaign(
    store: EngineStore,
    world_id: str,
    branch_id: str,
    *,
    participant_id: str,
    new_pc_name: str | None = None,
    adopt_actor_id: str | None = None,
    spec: dict[str, Any] | None = None,
    ruleset_id: str | None = None,
    ruleset_config: dict[str, Any] | None = None,
    seed: int = 0,
) -> Campaign:
    """Start a campaign whose game is `campaign_game`'s and whose PC is built from `spec`."""
    game = await campaign_game(
        store, branch_id, ruleset_id=ruleset_id, ruleset_config=ruleset_config
    )
    sheeted = await _adopted_sheeted(store, branch_id, adopt_actor_id)
    if spec is not None and sheeted:
        raise _no_spec_for_a_sheeted_actor()
    pc_sheet = None if sheeted else character_sheet(game.ruleset, spec)
    return await store.start_campaign(
        world_id,
        branch_id,
        participant_id=participant_id,
        new_pc_name=new_pc_name,
        adopt_actor_id=adopt_actor_id,
        pc_sheet=pc_sheet,
        starting_items=await starting_gear(store, branch_id) if new_pc_name is not None else None,
        ruleset_id=game.pin.id,
        ruleset_version=game.pin.version,
        seed=seed,
        ruleset_config=game.own,
    )


async def join_campaign(
    store: EngineStore,
    campaign: Campaign,
    *,
    participant_id: str,
    new_pc_name: str | None = None,
    adopt_actor_id: str | None = None,
    spec: dict[str, Any] | None = None,
) -> str:
    """Seat a participant on a campaign with a PC built from `spec` under the campaign's game.
    Returns the PC's actor id (an already-seated participant keeps theirs)."""
    sheeted = await _adopted_sheeted(store, campaign.branch_id, adopt_actor_id)
    if spec is not None and sheeted:
        raise _no_spec_for_a_sheeted_actor()
    pc_sheet = None
    if not sheeted:
        pc_sheet = character_sheet(await resolve_for_campaign(store, campaign), spec)
    return await store.bind_pc(
        campaign.campaign_id,
        participant_id,
        adopt_actor_id=adopt_actor_id,
        new_pc_name=new_pc_name,
        pc_sheet=pc_sheet,
        starting_items=(
            await starting_gear(store, campaign.branch_id) if new_pc_name is not None else None
        ),
        ruleset_id=campaign.ruleset_id,
    )


async def preview_character(
    store: EngineStore,
    *,
    branch_id: str,
    spec: dict[str, Any] | None,
    campaign: Campaign | None = None,
    ruleset_id: str | None = None,
    ruleset_config: dict[str, Any] | None = None,
) -> Preview:
    """The sheet a spec would make, under a campaign's game or the one a new campaign on the branch
    would play, or the ruleset's problems with it. Writes nothing."""
    if campaign is not None:
        ruleset = await resolve_for_campaign(store, campaign)
    else:
        game = await campaign_game(
            store, branch_id, ruleset_id=ruleset_id, ruleset_config=ruleset_config
        )
        ruleset = game.ruleset
    try:
        sheet = character_sheet(ruleset, spec)
    except CharSpecError as exc:
        return Preview(ok=False, ruleset=ruleset.id, problems=exc.problems)
    return Preview(ok=True, ruleset=ruleset.id, sheet=sheet)


async def _pc(store: EngineStore, campaign: Campaign, participant_id: str) -> str:
    actor_id = await store.pc_for_participant(campaign.campaign_id, participant_id)
    if actor_id is None:
        raise KeyError(f"{participant_id!r} has no character in this campaign")
    return actor_id


async def rebuild_character(
    store: EngineStore,
    campaign: Campaign,
    participant_id: str,
    *,
    spec: dict[str, Any] | None = None,
    name: str | None = None,
) -> CharacterView:
    """Replace a participant's character build and/or name while it is open (until the first beat
    after it was bound). A spec the ruleset can't build raises `CharSpecError`; a frozen build
    raises `CharacterFrozen` saying why; no PC raises KeyError."""
    actor_id = await _pc(store, campaign, participant_id)
    if spec is None and name is None:
        raise CharSpecError([SpecProblem(message="nothing to change: give a spec, a name or both")])
    if name is not None and not name.strip():
        raise CharSpecError([SpecProblem(loc=["name"], message="a name can't be empty")])
    ruleset = await resolve_for_campaign(store, campaign)
    cause = CausedBy(kind="player_action", participant_id=participant_id)
    events = []
    if spec is not None:
        sheet = character_sheet(ruleset, spec)
        events.append(
            sheet_updated(actor_id=actor_id, sheet=sheet, ruleset_id=ruleset.id, caused_by=cause)
        )
    if name is not None:
        events.append(actor_renamed(actor_id=actor_id, name=name.strip(), caused_by=cause))
    try:
        await store.amend_character(campaign.branch_id, actor_id, events)
    except ValueError as exc:
        raise CharacterFrozen(str(exc)) from exc
    return await own_character(store, campaign, participant_id)


async def own_character(
    store: EngineStore, campaign: Campaign, participant_id: str
) -> CharacterView:
    """A participant's own character: its sheet, what it holds, and whether its build is open."""
    actor_id = await _pc(store, campaign, participant_id)
    actor = await store.get_actor(campaign.branch_id, actor_id)
    sheet = await store.get_sheet(campaign.branch_id, actor_id)
    reason = await store.character_frozen(campaign.branch_id, actor_id)
    ruleset = campaign.ruleset_id or (await store.world_ruleset(campaign.branch_id)).id
    return CharacterView(
        campaign_id=campaign.campaign_id,
        participant_id=participant_id,
        actor_id=actor_id,
        name=actor.name if actor is not None else "",
        ruleset=ruleset or DEFAULT_RULESET_ID,
        sheet=sheet,
        holdings=await store.holdings(campaign.branch_id, actor_id),
        build_open=reason is None,
        frozen_reason=reason or "",
    )


def ruleset_info(ruleset: Ruleset) -> dict[str, Any]:
    """What a client needs to build a character for a ruleset: its sheet, spec and settings
    schemas (the spec schema `{}` when the ruleset declares none)."""
    return {
        "id": ruleset.id,
        "version": ruleset.version,
        "sheet_schema": ruleset.sheet_schema(),
        "spec_schema": spec_schema_of(ruleset),
        "config_schema": ruleset.config_schema(),
    }


# --- proposing a character from a description (D-112, Uro Loom request 14) ---

PROPOSAL_ROLE = "worldsmith"  # the authoring role backfill uses (unbound → the router's default)

_PROPOSAL_SYSTEM = """You turn a player's description of their character into a build for the
game's rules. Reply with ONLY a JSON object: {{"name": "<a name, if the description gives or
suggests one, else empty>", "spec": {{...}}}}. The spec follows this JSON Schema, and uses only the
keys it allows:

{schema}

Put in the spec what the description suggests (a strong, slow character: high strength, low
speed); leave out what it doesn't. Keep within every limit the schema states.{tone}"""


class Proposal(BaseModel):
    """A character build proposed from free text (D-112), checked through the same build a campaign
    start runs. Nothing is saved: the player edits `spec` and sends it to start or join."""

    ok: bool
    ruleset: str
    name: str = ""
    spec: dict[str, Any] = Field(default_factory=dict)
    sheet: Sheet | None = None
    problems: list[SpecProblem] = Field(default_factory=list)


def _with_defs_hoisted(spec_schema: dict[str, Any]) -> dict[str, Any]:
    """The reply's schema: {name, spec} with the spec schema nested and its `$defs` at the top, so
    its `#/$defs/…` references still resolve."""
    spec = dict(spec_schema)
    defs = spec.pop("$defs", None)
    out: dict[str, Any] = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "spec": spec},
        "required": ["spec"],
    }
    if defs:
        out["$defs"] = defs
    return out


async def propose_character(
    store: EngineStore,
    router: Any,
    *,
    branch_id: str,
    description: str,
    campaign: Campaign | None = None,
    ruleset_id: str | None = None,
    ruleset_config: dict[str, Any] | None = None,
) -> Proposal:
    """Ask the bound model for a build matching `description` under a campaign's game (or the one a
    new campaign on the branch would play), given the ruleset's spec schema and the world's tone,
    then check it like a preview. A reply that isn't a JSON object is a proposal with a problem,
    never an exception; the call itself may raise ProviderError. The model call lives here, in the
    narrator layer, never in a ruleset."""
    if campaign is not None:
        ruleset = await resolve_for_campaign(store, campaign)
    else:
        ruleset = (
            await campaign_game(
                store, branch_id, ruleset_id=ruleset_id, ruleset_config=ruleset_config
            )
        ).ruleset
    schema = spec_schema_of(ruleset)
    tone, _ = await store.world_style(branch_id)
    system = _PROPOSAL_SYSTEM.format(
        schema=json.dumps(schema or {"type": "object"}, indent=2),
        tone=f"\n\nThe world's tone: {tone}." if tone else "",
    )
    raw = await router.complete(
        PROPOSAL_ROLE,
        [Message(role="system", content=system), Message(role="user", content=description)],
        json_mode=True,
        json_schema=_with_defs_hoisted(schema) if schema.get("type") == "object" else None,
        temperature=0.7,
        branch_id=branch_id,
    )
    try:
        reply = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except ValueError:
        reply = None
    spec = reply.get("spec") if isinstance(reply, dict) else None
    if not isinstance(spec, dict):
        return Proposal(
            ok=False,
            ruleset=ruleset.id,
            problems=[SpecProblem(message="the model's reply held no build; try again")],
        )
    name = reply.get("name") if isinstance(reply.get("name"), str) else ""
    try:
        sheet = character_sheet(ruleset, spec)
    except CharSpecError as exc:
        return Proposal(ok=False, ruleset=ruleset.id, name=name, spec=spec, problems=exc.problems)
    return Proposal(ok=True, ruleset=ruleset.id, name=name, spec=spec, sheet=sheet)
