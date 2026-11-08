"""The HTTP API's inputs, so `/openapi.json` describes what every endpoint takes as well as what it
returns (`uro_server.schemas`): the query parameters, the JSON request bodies, and the two ways to
send a token.

These declare; they don't validate. Each handler still reads its inputs itself and answers a bad one
with a 400 that names the field (a FastAPI-validated body would answer a 422 in another shape, which
clients already handle). So a declaration here could drift from the code that reads it: a test
(`test_openapi_inputs`) parses every route handler and fails if a handler reads a query parameter or
a body field that isn't declared, or a declaration names a route that doesn't exist.

Body models allow keys they don't list, like the handlers (an unknown key is ignored), except the
whole-document bodies (`WorldBundle`, `RulePack`, `OutcomeBundle`), which are the core's own strict
models.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import models_json_schema
from uro_core.chronicler import OutcomeBundle
from uro_core.export import WorldBundle
from uro_core.worldpack.models import ContentDecl
from uro_core.worldpack.rules import RulePack


class In(BaseModel):
    model_config = ConfigDict(extra="allow")


_EFFORT = Field(
    default=None,
    description="The reasoning effort to send (D-71); checked against what the connection's "
    "provider takes. Absent = the provider's default.",
)


# --- providers (D-47) ---


class ConnectionIn(In):
    name: str
    provider: str = Field(description="The connection kind: `openai`, `anthropic`, `codex`, …")
    base_url: str | None = None
    auth_id: str | None = Field(default=None, description="An existing credential to link.")


class ConnectionPatch(In):
    is_enabled: bool | None = None
    auth_id: str | None = Field(default=None, description="A credential id, or `null` to unlink.")


class CredentialIn(In):
    provider: str
    access_token: str | None = Field(default=None, description="Plaintext; encrypted at rest.")
    refresh_token: str | None = None
    auth_mode: str = "api_key"


class CredentialRotate(In):
    access_token: str
    refresh_token: str | None = None


class RoleBindingIn(In):
    connection_id: str
    model: str
    reasoning_effort: str | None = _EFFORT


class ConnectionTestIn(In):
    model: str | None = Field(
        default=None, description="The model to probe. Absent = a bound or cheap model (D-110)."
    )
    reasoning_effort: str | None = _EFFORT


class ExtractionPolicyPatch(In):
    extract_actors: bool | None = None
    extract_places: bool | None = None
    extract_factions: bool | None = None
    extract_threads: bool | None = None
    extract_claims: bool | None = None


class CodexStart(In):
    name: str = "codex"


class CodexPoll(In):
    login_id: str


# --- worlds and branches ---


class WorldIn(In):
    name: str
    tone: list[str] | str | None = None
    description: str = ""
    content: ContentDecl | None = None
    ruleset: str = Field(default="", description="A ruleset id; absent = none bound.")
    ruleset_version: str = ""
    ruleset_config: dict[str, Any] = Field(default_factory=dict)
    rule_pack: RulePack | None = None


class ForkIn(In):
    name: str
    from_ref: str = Field(description="A marker name or a commit id (a marker wins).")
    time_skip_days: int = Field(default=0, ge=0)


class MarkerIn(In):
    name: str
    branch: str = Field(default="main", description="A branch name or id.")


# --- campaigns ---


_SPEC = Field(
    default=None,
    description="The character build, as the ruleset's `spec_schema` (`GET /rulesets`) describes "
    "it (D-112). Absent = the ruleset's default character.",
)


class CampaignIn(In):
    participant: str
    branch: str = Field(default="main", description="A branch name or id.")
    new_pc_name: str | None = None
    adopt_actor_id: str | None = None
    spec: dict[str, Any] | None = _SPEC
    ruleset: str | None = Field(
        default=None, description="This campaign's own ruleset (D-112). Absent = inherit."
    )
    ruleset_config: dict[str, Any] | None = Field(
        default=None, description="This campaign's own settings, validated by the ruleset."
    )
    seed: int = 0
    own_branch: bool = Field(default=False, description="Fork a playthrough of its own (D-100).")
    from_: str | None = Field(
        default=None, alias="from", description="With `own_branch`: a marker or commit to fork at."
    )
    branch_name: str | None = Field(default=None, description="With `own_branch`: its name.")


class JoinIn(In):
    participant: str
    new_pc_name: str | None = None
    adopt_actor_id: str | None = None
    spec: dict[str, Any] | None = _SPEC


class WorldCharacterPreviewIn(In):
    spec: dict[str, Any] | None = _SPEC
    branch: str = Field(default="main", description="A branch name or id.")
    ruleset: str | None = None
    ruleset_config: dict[str, Any] | None = None


class CampaignCharacterPreviewIn(In):
    spec: dict[str, Any] | None = _SPEC


class CharacterPatch(In):
    spec: dict[str, Any] | None = _SPEC
    name: str | None = None
    participant: str | None = Field(default=None, description="Absent = the caller.")


class MintIn(In):
    participant: str


class RevokeIn(In):
    token: str | None = Field(default=None, description="One token; or give `participant`.")
    participant: str | None = Field(default=None, description="Every token of one participant.")


class ThreadStateIn(In):
    state: Literal["dormant", "offered", "active", "resolved", "dead"]


class TimeSkipIn(In):
    days: int
    downtime: bool = True


class DryRunIn(In):
    intent: str


class EndIn(In):
    marker: str
    outcome: str = ""


class CodexNoteIn(In):
    text: str
    participant: str | None = Field(default=None, description="Absent = the caller.")
    key: str | None = None
    pinned: bool = False
    refs: list[str] = Field(default_factory=list)


BODIES: dict[tuple[str, str], type[BaseModel]] = {
    ("POST", "/providers"): ConnectionIn,
    ("PATCH", "/providers/{connection_id}"): ConnectionPatch,
    ("POST", "/providers/credentials"): CredentialIn,
    ("PATCH", "/providers/credentials/{credential_id}"): CredentialRotate,
    ("PUT", "/providers/roles/{role}"): RoleBindingIn,
    ("POST", "/providers/{connection_id}/test"): ConnectionTestIn,
    ("PATCH", "/extraction-policy"): ExtractionPolicyPatch,
    ("POST", "/providers/codex/start"): CodexStart,
    ("POST", "/providers/codex/poll"): CodexPoll,
    ("POST", "/campaigns/{campaign_id}/encounters/{encounter_id}/outcome"): OutcomeBundle,
    ("POST", "/worlds"): WorldIn,
    ("POST", "/rules/validate"): RulePack,
    ("POST", "/worlds/{world_id}/branches"): ForkIn,
    ("POST", "/worlds/{world_id}/markers"): MarkerIn,
    ("POST", "/worlds/import"): WorldBundle,
    ("POST", "/worlds/{world_id}/campaigns"): CampaignIn,
    ("POST", "/campaigns/{campaign_id}/join"): JoinIn,
    ("POST", "/campaigns/{campaign_id}/tokens"): MintIn,
    ("POST", "/campaigns/{campaign_id}/tokens/revoke"): RevokeIn,
    ("POST", "/campaigns/{campaign_id}/threads/{thread_id}/state"): ThreadStateIn,
    ("POST", "/campaigns/{campaign_id}/time-skip"): TimeSkipIn,
    ("POST", "/campaigns/{campaign_id}/dry-run"): DryRunIn,
    ("POST", "/campaigns/{campaign_id}/end"): EndIn,
    ("POST", "/campaigns/{campaign_id}/codex"): CodexNoteIn,
    ("POST", "/worlds/{world_id}/character-preview"): WorldCharacterPreviewIn,
    ("POST", "/campaigns/{campaign_id}/character/preview"): CampaignCharacterPreviewIn,
    ("PATCH", "/campaigns/{campaign_id}/character"): CharacterPatch,
}


@dataclass(frozen=True)
class Query:
    name: str
    description: str
    type: Literal["string", "integer", "boolean"] = "string"
    required: bool = False
    default: Any = None


_LIMIT = {n: Query("limit", "How many to return (≥ 0).", "integer", default=n) for n in (20, 50)}
_BRANCH = Query("branch", "A branch name or id.", default="main")
_CALL_SCOPE = [
    Query("stage", "Only this engine role's calls (`narrator`, `extractor`, …)."),
    Query("world", "Only this world's calls (a world id)."),
    Query("campaign", "Only the calls on this campaign's branch."),
]
_SECTIONS = Query("sections", "Comma-separated projection sections.")

QUERY: dict[tuple[str, str], list[Query]] = {
    ("GET", "/usage"): _CALL_SCOPE,
    ("GET", "/llm-calls"): [*_CALL_SCOPE, _LIMIT[20]],
    ("GET", "/campaigns"): [Query("world_id", "Only this world's campaigns.")],
    ("POST", "/worlds/create"): [
        Query("backfill", "Fill the pack's gaps first (D-108).", "boolean", default=False)
    ],
    ("POST", "/worlds/{world_id}/seed"): [
        Query("seed", "The history seed.", "integer", default=42)
    ],
    ("POST", "/worlds/probe"): [Query("tries", "Calls per probe.", "integer", default=3)],
    ("GET", "/worlds/{world_id}/log"): [_BRANCH, _LIMIT[20]],
    ("GET", "/worlds/{world_id}/events"): [
        _BRANCH,
        Query("type", "Only this event type (`ActorCreated`, …)."),
        Query("entity_ref", "Only events about this entity."),
        Query("caused_by", "Only events with this cause kind."),
        _LIMIT[50],
    ],
    ("GET", "/worlds/{world_id}/state"): [
        Query(
            "sections",
            "Comma-separated projection sections.",
            default=("actors,threads,places,factions"),
        ),
        Query("at", "A commit id, a prefix of one, or a marker (not with `branch`)."),
        Query("branch", "A branch name or id (not with `at`).", default="main"),
    ],
    ("GET", "/worlds/{world_id}/diff"): [
        Query("a", "The branch to diff from (a name or id).", required=True),
        Query("b", "The branch to diff to (a name or id).", required=True),
        _SECTIONS,
    ],
    ("GET", "/worlds/{world_id}/chronicle"): [_BRANCH, _LIMIT[20]],
    ("GET", "/campaigns/{campaign_id}/state"): [
        Query(
            "sections",
            "Comma-separated projection sections.",
            default=("actors,threads,places,factions"),
        )
    ],
    ("GET", "/campaigns/{campaign_id}/chronicle"): [_LIMIT[20]],
    ("GET", "/campaigns/{campaign_id}/character"): [
        Query(
            "participant", "Whose character (an operator may read anyone's). Absent = the caller."
        )
    ],
    ("GET", "/campaigns/{campaign_id}/codex"): [
        Query("participant", "Whose notes (an operator may read anyone's). Absent = the caller.")
    ],
}

# Routes that take no token: a liveness probe advertises nothing, so it needs none.
UNAUTHENTICATED = frozenset({("GET", "/healthz")})

SECURITY_SCHEMES = {
    "bearer": {"type": "http", "scheme": "bearer", "description": "A `--token` or a minted token."},
    "token": {
        "type": "apiKey",
        "in": "query",
        "name": "token",
        "description": "The same token as a query parameter (what a browser WebSocket can send).",
    },
}


def _renamed(node: Any, names: dict[str, str]) -> Any:
    if isinstance(node, dict):
        out = {k: _renamed(v, names) for k, v in node.items()}
        ref = out.get("$ref")
        if isinstance(ref, str) and ref.rsplit("/", 1)[-1] in names:
            out["$ref"] = f"#/components/schemas/{names[ref.rsplit('/', 1)[-1]]}"
        return out
    if isinstance(node, list):
        return [_renamed(v, names) for v in node]
    return node


def document_inputs(spec: dict[str, Any]) -> dict[str, Any]:
    """`spec` (FastAPI's generated OpenAPI) with every declared input added. A body component
    named like a response component of a different shape (the same core model, read and written
    differently) is published as `<Name>Input`, so neither describes the other."""
    spec = copy.deepcopy(spec)
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    components["securitySchemes"] = SECURITY_SCHEMES
    keyed, defs = models_json_schema(
        [(m, "validation") for m in dict.fromkeys(BODIES.values())],
        ref_template="#/components/schemas/{model}",
    )
    body_defs = defs.get("$defs", {})
    clash = {n: f"{n}Input" for n, d in body_defs.items() if n in schemas and schemas[n] != d}
    for name, schema in body_defs.items():
        schemas[clash.get(name, name)] = _renamed(schema, clash)
    keyed = {k: _renamed(v, clash) for k, v in keyed.items()}
    for path, item in spec.get("paths", {}).items():
        for method, op in item.items():
            key = (method.upper(), path)
            if key not in UNAUTHENTICATED:
                op["security"] = [{"bearer": []}, {"token": []}]
            if key in BODIES:
                ref = keyed[(BODIES[key], "validation")]
                # FastAPI writes `required` only for a body with no default (OpenAPI's own
                # default is false), so a handler's `Body(default={})` stays optional
                required = bool(op.get("requestBody", {}).get("required", False))
                op["requestBody"] = {
                    "required": required,
                    "content": {"application/json": {"schema": ref}},
                }
            params = [p for p in op.get("parameters", []) if p.get("in") != "query"]
            for q in QUERY.get(key, []):
                schema = {"type": q.type}
                if q.default is not None:
                    schema["default"] = q.default
                params.append(
                    {
                        "name": q.name,
                        "in": "query",
                        "required": q.required,
                        "description": q.description,
                        "schema": schema,
                    }
                )
            if params:
                op["parameters"] = params
    return spec
