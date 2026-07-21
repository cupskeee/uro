"""The FastAPI shell (docs/08): transport, sessions, auth, wiring — NO engine logic.

Everything the engine does is reached through `ServerDeps` (a small port): token→participant
resolution, campaign existence, and a streaming beat runner. Production wires it to a connected
store + Engine (`engine_deps`); tests inject a fake — so the transport (auth, broadcast fan-out,
the WS play channel) is exercised without a live DB or model. The heavy engine path is tested
directly in uro-core.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:  # annotation-only (from __future__ import annotations) — no runtime import
    from uro_core.engines.probe import ProbeReport
    from uro_core.providers.router import ProviderRouter
    from uro_core.timeline.models import Campaign
    from uro_core.worldpack.models import WorldManifest, WorldPack

from fastapi import (
    Body,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketState
from uro_core.adapters.crypto import SecretsUnavailable
from uro_core.errors import ExportError
from uro_core.export import WorldBundle
from uro_core.pipeline.engine import Engine
from uro_core.ports.model_registry import ROLES
from uro_core.ports.projections import EngineStore
from uro_core.session import AdmitDecision, SoloArbiter, TurnArbiter, VoteCoordinator

from uro_server.sessions import SessionHub, TokenRegistry

logger = logging.getLogger("uro_server")


def _bearer_token(request: Request) -> str:
    """Extract a bearer token from the Authorization header, or ''."""
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


_MAX_PACK_BYTES = 20 * 1024 * 1024  # 20 MB — an authored world pack is small; cap untrusted input
_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB — the DECOMPRESSED-size cap (zip-bomb guard)

# The non-omniscient scene projections a PLAYER may read via GET /campaigns/{c}/state (D-45): a
# player token is restricted to these; `claims` (truth values), `beliefs` (hidden), `sheets`,
# `items`, `edges`, `counters`, `snapshots` carry GM ground truth → operator-only. The whole
# epistemic thesis collapses if a player can read the raw truth-tagged log (D-45).
_PLAYER_SAFE_SECTIONS = frozenset({"actors", "threads", "places", "factions", "pcs"})

# The `test`-probe output ceiling. Small enough to be ~free on a chat model (it stops after a
# one-word reply anyway) but large enough that an o-series reasoning model — which spends the budget
# on hidden reasoning tokens BEFORE any output — clears its minimum and can 200 rather than failing
# the very probe this is meant to make pass.
_PROBE_MAX_TOKENS = 256

# A known-good CHAT model to probe when `test` is called without one (D-47 slice 3). See
# `_default_probe_model` — for local/openai_compat the connection's own discovered models are a
# better canary than any pinned default.
_DEFAULT_PROBE_MODEL = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5",
    "local": "llama3.1",
    "stub": "stub-chat",
}


def _default_probe_model(conn: Any) -> str:
    """Pick a canary model for a connection-level liveness probe when the caller passes none.

    Prefer the provider's known-good chat default — NOT `cached_models[0]`: `discover_models`
    returns the list SORTED, so OpenAI's leads with `babbage-002`, a legacy base-completion model
    the chat-probe can't call → a false ✗ (the reported bug). For `local`/`openai_compat` (where
    there is no reliable pinned chat default — the local one may not even be pulled) fall back to
    the connection's OWN first discovered model, which `classify_modality` then routes correctly to
    embed-vs-complete. A precise per-MODEL check lives on each role binding instead.
    """
    if conn.provider in ("local", "openai_compat"):
        for m in conn.cached_models or []:
            mid = m.get("id")
            if mid:
                return str(mid)
    return _DEFAULT_PROBE_MODEL.get(conn.provider, "")


# The multipart pack-UPLOAD routes. FastAPI spools the whole body during form parsing — BEFORE a
# handler's operator gate or the `_safe_extract_pack` cap — so an oversized upload is rejected up
# front by Content-Length (below). NOT `/worlds/import`: that's a JSON bundle, legitimately large.
_PACK_UPLOAD_PATHS = frozenset({"/worlds/validate", "/worlds/backfill", "/worlds/probe"})


def _safe_extract_pack(data: bytes, dest: Path) -> Path:
    """Extract an uploaded pack `.zip` into `dest` (ZIP-SLIP-SAFE) and return the pack root — the
    dir holding `world.toml` (the archive root, or a single top-level subdir). Raises
    HTTPException on a too-large / non-zip / path-escaping / zip-bomb / rootless archive (BE-6).
    The compressed cap alone does NOT bound a zip bomb (a few KB → GBs), so extraction runs
    member-by-member with a cumulative DECOMPRESSED-byte cap on the bytes actually written (never
    trusting the central-directory sizes, which are attacker-controlled)."""
    if len(data) > _MAX_PACK_BYTES:
        raise HTTPException(status_code=413, detail="pack archive too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="upload must be a .zip of the pack") from exc
    dest_root = dest.resolve()
    written = 0
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if not (target == dest_root or dest_root in target.parents):  # zip-slip guard
            raise HTTPException(
                status_code=400, detail=f"unsafe path in archive: {member.filename!r}"
            )
        if member.is_dir():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as out:
            while chunk := src.read(65536):
                written += len(chunk)
                if written > _MAX_UNCOMPRESSED_BYTES:  # zip-bomb: abort mid-stream, don't fill disk
                    raise HTTPException(status_code=413, detail="pack expands too large")
                out.write(chunk)
    if (dest / "world.toml").is_file():
        return dest
    subdirs = [d for d in dest.iterdir() if d.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "world.toml").is_file():
        return subdirs[0]
    raise HTTPException(status_code=400, detail="no world.toml found in the archive")


def _default_sheet(ruleset_id: str, version: str) -> tuple[dict[str, Any], str]:
    """A default character sheet from a world's declared ruleset (docs/06, D-30) — mirrors the
    CLI's `_build_pc_sheet` so a REST-created PC is sheeted+pinned exactly like a CLI-created one
    (else the ruleset's move/harm layer never engages and the WS cross-ruleset guard is bypassed by
    an empty pin). Built via the registry, independent of the server's process-bound ruleset.
    Returns (sheet, resolved ruleset id)."""
    from uro_core.rulesets.base import CharSpec
    from uro_core.rulesets.registry import resolve as _resolve
    from uro_core.rulesets.rng import Rng

    ruleset = _resolve(ruleset_id, version)
    return ruleset.new_character(CharSpec(), Rng(0)), ruleset.id


def _resolve_ruleset_id(ruleset_id: str, version: str) -> str:
    """The resolved ruleset id for a world's declared (id, version) — for pinning a campaign whose
    PC we don't (re)sheet (an adopted actor that already has one)."""
    from uro_core.rulesets.registry import resolve as _resolve

    return _resolve(ruleset_id, version).id


@dataclass
class ServerDeps:
    """The seam between the transport shell and the engine (docs/01: server sees only ports)."""

    resolve_participant: Callable[[str], str | None]  # bearer token → participant_id (None = deny)
    campaign_exists: Callable[[str], Awaitable[bool]]
    run_beat: Callable[
        [str, str, str], AsyncIterator[str]
    ]  # (campaign, participant, intent) → chunks
    # Chronicler mode (D-25): distill an external game's outcome bundle → committed events.
    report_outcome: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # Management surface (docs/18 B3): the EngineStore port (a Protocol — the server still sees only
    # ports) powers the CRUD/read endpoints; `advance_time` runs an engine-backed time-skip+agendas.
    # None → those endpoints return 501 (a fake-deps test that only exercises play/outcome).
    store: EngineStore | None = None
    advance_time: Callable[[str, int], Awaitable[dict[str, Any]]] | None = None
    # Branch-scoped time-skip + downtime agendas (BE-2 fork `--time-skip-days`): engine.agenda_tick
    # on a raw branch id (a fork has no campaign), returning the new in-fiction day. None → fork's
    # optional time-skip leg returns 501.
    advance_branch_time: Callable[[str, int], Awaitable[int]] | None = None
    # Dry-run a beat (BE-5): run the full pipeline and return the would-be events WITHOUT committing
    # (campaign, participant, intent) → serialized events. Intent-only over the network (no client
    # `plan=`, D-37). None → the dry-run endpoint returns 501.
    preview_beat: Callable[[str, str, str], Awaitable[list[dict[str, Any]]]] | None = None
    # Runtime token management (docs/18 B10, D-39): mint/revoke durable session tokens + the admin
    # check, behind the same resolve_participant choke point. None → the token endpoints return 501.
    mint_token: Callable[[str, str], Awaitable[str]] | None = None  # (participant, campaign)→token
    revoke_token: Callable[[str], Awaitable[bool]] | None = None  # (plaintext token) → revoked?
    is_admin: Callable[[str], bool] | None = None  # (token) → operator tier (may act for others)?
    token_campaign: Callable[[str], str | None] | None = None  # minted token → its campaign (scope)
    hydrate_tokens: Callable[[], Awaitable[None]] | None = None  # load durable tokens at startup
    # The bound ruleset registry (BE-10): id@version + sheet shape of each built-in. A pure
    # composition-root lookup (not store-backed), so it's wired here rather than on the store. None
    # → `GET /rulesets` returns 501.
    list_rulesets: Callable[[], list[dict[str, Any]]] | None = None
    # AI world-authoring stages (BE-7): both wrap the process-bound ProviderRouter, so they're
    # provider-shaped (not store-backed) and make LIVE, uncapped LLM calls → operator-only (D-44).
    # `backfill` previews a thin pack's AI gap-fill (augmented pack + human-readable additions);
    # `probe` returns the model-capability report. None (no provider wired) → the endpoints 501.
    backfill: Callable[[WorldPack], Awaitable[tuple[WorldPack, list[str]]]] | None = None
    probe: Callable[[WorldManifest, int], Awaitable[ProbeReport]] | None = None
    # Model-connection registry slice 3/4 (D-47): discover a connection's models (live), probe a
    # connection (a 1-token call), and rebuild the instance router from the registry without a
    # restart. All do provider/live work, so they're composed here (not store-backed). None → 501.
    refresh_models: Callable[[str], Awaitable[list[dict[str, str]]]] | None = None
    test_connection: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None
    reload_router: Callable[[], Awaitable[dict[str, Any]]] | None = None


def engine_deps(
    store: EngineStore,
    engine: Engine,
    tokens: dict[str, str],
    admin_tokens: set[str] | None = None,
    *,
    router: ProviderRouter | None = None,
) -> ServerDeps:
    """Production wiring: a connected store + Engine behind the transport port. `tokens` maps a
    bearer token to a participant_id (docs/08 token mode) — ordinary PLAYER credentials; the
    `admin_tokens` subset is the OPERATOR tier (may act for others, D-39 review). Runtime player
    tokens live in a durable, campaign-scoped `TokenRegistry` over the same resolve choke point."""
    registry = TokenRegistry(store, tokens, admin_tokens)

    async def campaign_exists(campaign_id: str) -> bool:
        return (await store.get_campaign(campaign_id)) is not None

    async def run_beat(campaign_id: str, participant: str, text: str) -> AsyncIterator[str]:
        campaign = await store.get_campaign(campaign_id)
        if campaign is None:
            return
        # The server binds ONE ruleset per process (docs/08 deferral); a campaign pinned to a
        # DIFFERENT ruleset would otherwise crash deep in sheet validation. Reject up front with a
        # clear diagnostic instead (D-30 per-campaign binding; the CLI play path rebinds correctly).
        if campaign.ruleset_id and engine.ruleset_id and campaign.ruleset_id != engine.ruleset_id:
            raise ValueError(
                f"campaign {campaign_id} is bound to ruleset {campaign.ruleset_id!r}, but this "
                f"server runs {engine.ruleset_id!r} — start `uro serve --ruleset "
                f"{campaign.ruleset_id}` (one ruleset per server process)"
            )
        async for chunk in engine.run_beat_stream(campaign, participant, text):
            yield chunk

    async def report_outcome(campaign_id: str, bundle_json: dict[str, Any]) -> dict[str, Any]:
        from uro_core.chronicler import OutcomeBundle, distill_outcome_with_receipt

        campaign = await store.get_campaign(campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        bundle = OutcomeBundle.model_validate(bundle_json)
        result = await distill_outcome_with_receipt(store, campaign.branch_id, bundle)
        # Commit + react in ONE call (B1, docs/18): an EXTERNAL death is the war-story premise —
        # combat is non-lethal, so the Chronicler is the only runtime ActorDied source, and a pack
        # rule triggering on ActorDied must fire here too, not only on the run_beat path. Using
        # append_and_react (not a hand-rolled append_beat + react) is exactly what B1 landed for —
        # authored-commit callers shouldn't re-implement the react step. react() is
        # exception-isolated, so the outcome beat is durable even if a rule raises.
        commit = await engine.append_and_react(campaign, result.events)
        # The per-ref receipt (docs/18 B6) tells the reporting game what was applied/downgraded/
        # dropped and why — otherwise a Chronicler consumer couldn't see the D-32 downgrades.
        return {
            "committed_events": len(result.events),
            "commit_id": commit.commit_id,
            "receipt": [r.model_dump() for r in result.receipt],
        }

    async def advance_time(campaign_id: str, days: int) -> dict[str, Any]:
        campaign = await store.get_campaign(campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        await engine.agenda_tick(campaign.branch_id, days)  # time-skip + downtime agendas (D-33)
        world_day = await store.current_world_time(campaign.branch_id)
        return {"branch_id": campaign.branch_id, "world_day": world_day}

    async def advance_branch_time(branch_id: str, days: int) -> int:
        # BE-2 fork `--time-skip-days`: the same helper the CLI `uro branch fork` uses — agenda_tick
        # advances in-fiction time AND fires the world's downtime agenda rules (D-33), no LLM. A
        # rule-less world → a plain time-skip. Returns the new in-fiction day.
        await engine.agenda_tick(branch_id, days)
        return await store.current_world_time(branch_id)

    async def preview_beat(campaign_id: str, participant: str, intent: str) -> list[dict[str, Any]]:
        # BE-5 dry-run: run the full pipeline (plan→narrate→extract) and return the would-be events,
        # committing NOTHING. Intent-only (no `plan=`, D-37 — a network plan would need the D-32
        # ceiling; this path never accepts one). Same one-ruleset-per-process guard as run_beat.
        campaign = await store.get_campaign(campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        if campaign.ruleset_id and engine.ruleset_id and campaign.ruleset_id != engine.ruleset_id:
            raise ValueError(
                f"campaign {campaign_id} is bound to ruleset {campaign.ruleset_id!r}, but this "
                f"server runs {engine.ruleset_id!r} — start `uro serve --ruleset "
                f"{campaign.ruleset_id}` (one ruleset per server process)"
            )
        events = await engine.preview_beat(campaign, participant, intent)
        return [e.model_dump() for e in events]

    def list_rulesets() -> list[dict[str, Any]]:
        # The registry is a COMPOSITION concern (it knows the concrete built-ins) — resolving it
        # here keeps app.py's request handlers from importing concrete rulesets. id@version + the
        # sheet shape a Ruleset viewer needs (BE-10; docs/06, D-30).
        from uro_core.rulesets import registry as ruleset_registry

        out: list[dict[str, Any]] = []
        for rid in ruleset_registry.available():
            rs = ruleset_registry.resolve(rid)
            out.append({"id": rs.id, "version": rs.version, "sheet_schema": rs.sheet_schema()})
        return out

    # BE-7: the AI world-authoring stages bind the SAME process router the Engine holds (serve
    # builds it once, main.py) — one provider per process, exactly like run_beat. Wired only when a
    # router is supplied; a transport-only deployment leaves them None → the endpoints 501.
    backfill: Callable[[WorldPack], Awaitable[tuple[WorldPack, list[str]]]] | None = None
    probe: Callable[[WorldManifest, int], Awaitable[ProbeReport]] | None = None
    if router is not None:
        bound_router = router  # narrow for the closures (mypy: no Optional capture)

        async def backfill(pack: WorldPack) -> tuple[WorldPack, list[str]]:
            from uro_core.worldpack.backfill import backfill_gaps

            return await backfill_gaps(pack, bound_router)

        async def probe(manifest: WorldManifest, tries: int) -> ProbeReport:
            from uro_core.engines.probe import run_probes

            return await run_probes(manifest, bound_router, tries=tries)

    async def _conn_secret(connection_id: str) -> tuple[Any, str | None]:
        conn = await store.get_connection(connection_id)
        if conn is None:
            raise KeyError(connection_id)  # the endpoint maps this to a 404
        access: str | None = None
        if conn.auth_id is not None:
            secret = await store.get_secret(conn.auth_id)
            access = secret[0] if secret is not None else None
        return conn, access

    async def refresh_models(connection_id: str) -> list[dict[str, str]]:
        from uro_core.providers.registry import discover_models

        conn, access = await _conn_secret(connection_id)
        models = await discover_models(conn, access)
        await store.set_connection_models(connection_id, models)
        return models

    async def test_connection(connection_id: str, model: str) -> dict[str, Any]:
        from uro_core.providers.base import CompletionRequest, Message
        from uro_core.providers.registry import classify_modality, provider_from_connection

        conn, access = await _conn_secret(connection_id)
        probe_model = model or _default_probe_model(conn)
        provider = provider_from_connection(conn, probe_model, access)
        try:
            if classify_modality(conn.provider, probe_model) == "embedding":
                await provider.embed(["ping"])
            else:
                await provider.complete(
                    CompletionRequest(
                        messages=[Message(role="user", content="ping")],
                        stage_tag="test",
                        # A ceiling, not a target (see _PROBE_MAX_TOKENS): ~free on a chat model,
                        # but gives a reasoning model room past its hidden-reasoning spend.
                        max_tokens=_PROBE_MAX_TOKENS,
                    )
                )
        except Exception as exc:
            # NEVER echo the raw provider/httpx exception text: it can carry the plaintext key (e.g.
            # "Illegal header value b'Bearer sk-…'"). Report only the exception TYPE (review).
            logger.warning("provider test failed for %s: %s", conn.provider, type(exc).__name__)
            return {
                "ok": False,
                "detail": f"{conn.provider}:{probe_model} failed ({type(exc).__name__})",
            }
        return {"ok": True, "detail": f"{conn.provider}:{probe_model} responded"}

    async def reload_router() -> dict[str, Any]:
        from uro_core.providers.registry import build_router_from_registry

        try:
            new_router = await build_router_from_registry(store)
        except ValueError as exc:  # incomplete registry (bindings but no default) — don't 500 or
            return {
                "reloaded": False,
                "detail": str(exc),
            }  # rebind; leave the running router intact
        if new_router is None:
            return {"reloaded": False, "detail": "registry has no bindings; router unchanged"}
        engine.rebind_router(new_router)
        return {"reloaded": True}

    return ServerDeps(
        resolve_participant=registry.resolve,
        campaign_exists=campaign_exists,
        run_beat=run_beat,
        report_outcome=report_outcome,
        store=store,
        advance_time=advance_time,
        advance_branch_time=advance_branch_time,
        preview_beat=preview_beat,
        mint_token=registry.mint,
        revoke_token=registry.revoke,
        is_admin=registry.is_admin,
        token_campaign=registry.campaign_of,
        hydrate_tokens=registry.hydrate,
        list_rulesets=list_rulesets,
        backfill=backfill,
        probe=probe,
        refresh_models=refresh_models,
        test_connection=test_connection,
        reload_router=reload_router,
    )


def create_app(
    deps: ServerDeps,
    *,
    arbiter: TurnArbiter | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    app = FastAPI(title="Uro Engine server")
    hub = SessionHub()
    arb = arbiter or SoloArbiter()

    # A browser SPA (uro-loom) lives on a DIFFERENT origin than the server, so without CORS the
    # browser blocks every cross-origin call (the request never reaches here — it fails preflight /
    # is dropped client-side). Off by default (the CLI/embed paths don't need it, and a permissive
    # default would be an unsafe surprise); a deployment opts in per allowed origin via
    # `uro serve --cors-origin`. `*` is honored for pure dev (allow-any), but then credentials are
    # disabled per the CORS spec (a wildcard origin cannot carry credentials). Tokens ride the
    # Authorization header (not cookies) today, so `allow_credentials` matters only for a future
    # cookie/BFF deployment (docs/05-bff-design.md) — hence the explicit-origins path keeps it on.
    if cors_origins:
        wildcard = "*" in cors_origins
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"] if wildcard else cors_origins,
            allow_credentials=not wildcard,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _cap_pack_uploads(request: Request, call_next: Any) -> Any:
        """Best-effort early reject of an over-cap multipart pack upload by Content-Length, BEFORE
        FastAPI spools the body (BE-7 hardening). This is a fast-path only, NOT the real bound: a
        chunked or Content-Length-absent upload bypasses it and is still spooled, then caught by the
        post-parse compressed cap (`_MAX_PACK_BYTES`) and the decompressed zip-bomb cap
        (`_MAX_UNCOMPRESSED_BYTES` in `_safe_extract_pack`) — those two are the actual guarantees.
        Scoped to the pack routes; `/worlds/import` (a large JSON bundle) is untouched. In
        production a reverse proxy should also cap the request body."""
        if request.method == "POST" and request.url.path in _PACK_UPLOAD_PATHS:
            cl = request.headers.get("content-length")
            if cl is not None and cl.isdigit() and int(cl) > _MAX_PACK_BYTES:
                return JSONResponse({"detail": "pack archive too large"}, status_code=413)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/usage")
    async def usage(request: Request) -> dict[str, Any]:
        """LLM-call telemetry aggregated by stage (BE-10, docs/07). OPERATOR-only (D-44): it reveals
        model/token/latency cost. The engine EXPOSES metering (docs/00) — it never bills/caps.
        `?stage=` scopes to one engine role. `?world=`/`?campaign=` are **not supported yet** (the
        `llm_calls` rows carry no world/campaign column — see docs/08) → 400, never silently
        ignored, so a consumer never mistakes a global total for a per-world one."""
        _require_operator(request)
        store = _mgmt()
        for unsupported in ("world", "campaign"):
            if request.query_params.get(unsupported):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"filtering usage by {unsupported!r} is not supported yet — the metering "
                        "rows are not keyed by world/campaign (docs/08 deferral)"
                    ),
                )
        stage = request.query_params.get("stage") or None
        rows = await store.usage_by_stage(stage)
        total_calls = sum(int(r["calls"]) for r in rows)
        return {"stage": stage, "total_calls": total_calls, "by_stage": rows}

    @app.get("/rulesets")
    async def rulesets(request: Request) -> dict[str, Any]:
        """The bound ruleset registry (BE-10, docs/06): each built-in's `id`, `version`, and sheet
        shape — what a Ruleset viewer needs. A plain any-authed read (public capability info, no
        world state). 501 if the deployment wired transport-only deps."""
        _auth(request)
        if deps.list_rulesets is None:
            raise HTTPException(status_code=501, detail="ruleset registry not enabled")
        return {"rulesets": deps.list_rulesets()}

    # --- Model-connection registry (D-47, docs/20 — slice 2). The instance-level LLM provider
    # registry over HTTP, so uro-loom / any client configures it (uro-cli edits the DB directly).
    # ALL endpoints are OPERATOR-only (D-44 — provider config is a cost/structural concern), and no
    # read ever returns a secret (list_credentials is metadata; the plaintext leaves only via the
    # wiring layer's get_secret at serve startup). A credential's key arrives as plaintext over the
    # (operator-only, TLS-in-prod) wire and is encrypted at rest under URO_SECRET_KEY.

    @app.get("/providers")
    async def list_providers(request: Request) -> dict[str, Any]:
        """The full registry snapshot — connections, role bindings, and credential METADATA (never
        secrets). Operator-only (D-47)."""
        _require_operator(request)
        store = _mgmt()
        return {
            "connections": [c.model_dump() for c in await store.list_connections()],
            "roles": [b.model_dump() for b in await store.list_role_bindings()],
            "credentials": [c.model_dump() for c in await store.list_credentials()],
        }

    @app.post("/providers")
    async def create_provider(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        """Register a model connection. Optional `auth_id` links an existing credential (validated).
        Operator-only."""
        _require_operator(request)
        store = _mgmt()
        auth_id = body.get("auth_id")
        if auth_id is not None and all(c.id != auth_id for c in await store.list_credentials()):
            raise HTTPException(status_code=400, detail=f"no such credential: {auth_id}")
        cid = await store.add_connection(
            name=str(_require(body, "name")),
            provider=str(_require(body, "provider")),
            base_url=body.get("base_url") or None,
            auth_id=auth_id,
        )
        return {"id": cid}

    @app.patch("/providers/{connection_id}")
    async def update_provider(
        connection_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Enable/disable a connection (`{is_enabled: bool}`). Operator-only."""
        _require_operator(request)
        if "is_enabled" not in body:
            raise HTTPException(status_code=400, detail="nothing to update (expected is_enabled)")
        ok = await _mgmt().set_connection_enabled(connection_id, bool(body["is_enabled"]))
        if not ok:
            raise HTTPException(status_code=404, detail="no such connection")
        return {"updated": True}

    @app.delete("/providers/{connection_id}")
    async def delete_provider(request: Request, connection_id: str) -> dict[str, Any]:
        """Delete a connection (its role bindings cascade → those roles fall back to `default`).
        Operator-only."""
        _require_operator(request)
        return {"deleted": await _mgmt().delete_connection(connection_id)}

    @app.post("/providers/credentials")
    async def create_credential(
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Store a provider credential — the `access_token` arrives as PLAINTEXT and is encrypted
        at rest (URO_SECRET_KEY). Operator-only; 501 if the server has no KEK configured."""
        _require_operator(request)
        try:
            cred_id = await _mgmt().add_credential(
                provider=str(_require(body, "provider")),
                access_token=body.get("access_token"),
                refresh_token=body.get("refresh_token"),
                auth_mode=str(body.get("auth_mode") or "api_key"),
            )
        except SecretsUnavailable as exc:  # no/invalid URO_SECRET_KEY → credential storage disabled
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except ValueError as exc:  # a control char (CR/LF) in the key → clean 400 at ingestion
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": cred_id}

    @app.delete("/providers/credentials/{credential_id}")
    async def delete_credential(request: Request, credential_id: str) -> dict[str, Any]:
        """Delete a credential; linked connections are UNLINKED (auth_id→NULL), not deleted.
        Operator-only."""
        _require_operator(request)
        return {"deleted": await _mgmt().delete_credential(credential_id)}

    @app.put("/providers/roles/{role}")
    async def set_role(
        role: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Bind an engine role to a connection+model. `default` backs any unbound role. Operator."""
        _require_operator(request)
        if role not in ROLES:
            raise HTTPException(
                status_code=400, detail=f"unknown role {role!r}; one of {sorted(ROLES)}"
            )
        store = _mgmt()
        connection_id = str(_require(body, "connection_id"))
        conn = await store.get_connection(connection_id)
        if conn is None:
            raise HTTPException(status_code=400, detail=f"no such connection: {connection_id}")
        model = str(_require(body, "model")).strip()
        if not model:  # a non-browser client could send "" → a silently-broken binding (review)
            raise HTTPException(status_code=400, detail="model must not be empty")
        # The embedder role needs an EMBEDDING model (slice 3). Classify by the provider's naming;
        # reject a chat model, but allow "unknown" (an unclassifiable provider — a live `test` is
        # the definitive check) so we never hard-block a valid but unrecognized endpoint.
        if role == "embedder":
            from uro_core.providers.registry import classify_modality

            if classify_modality(conn.provider, model) == "chat":
                raise HTTPException(
                    status_code=400,
                    detail=f"the embedder role needs an embedding model, not {model!r} "
                    f"(a chat model on provider {conn.provider!r})",
                )
        await store.set_role_binding(role, connection_id, model)
        return {"role": role, "connection_id": connection_id}

    @app.delete("/providers/roles/{role}")
    async def delete_role(request: Request, role: str) -> dict[str, Any]:
        """Remove a role binding (that role falls back to `default`). Operator-only."""
        _require_operator(request)
        return {"deleted": await _mgmt().delete_role_binding(role)}

    @app.post("/providers/{connection_id}/refresh")
    async def refresh_provider(request: Request, connection_id: str) -> dict[str, Any]:
        """Discover the connection's models (a LIVE call to the provider) and cache them with their
        modality (slice 3). Operator-only; 502 on a provider/network failure, 404 if unknown."""
        _require_operator(request)
        if deps.refresh_models is None:
            raise HTTPException(status_code=501, detail="model discovery not enabled")
        try:
            return {"models": await deps.refresh_models(connection_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="no such connection") from exc
        except (httpx.HTTPError, OSError, ValueError) as exc:
            # Generic detail — the exception text can carry the plaintext key (holistic HIGH).
            logger.warning("model discovery failed: %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="model discovery failed") from exc

    @app.post("/providers/{connection_id}/test")
    async def test_provider(
        request: Request,
        connection_id: str,
        body: dict[str, Any] = Body(default={}),  # noqa: B008
    ) -> dict[str, Any]:
        """Probe a connection with a 1-token call (slice 3). Optional `{model}`; returns
        `{ok, detail}` — a provider failure is `ok:false`, not an HTTP error. Operator-only."""
        _require_operator(request)
        if deps.test_connection is None:
            raise HTTPException(status_code=501, detail="connection test not enabled")
        try:
            return await deps.test_connection(connection_id, str(body.get("model") or ""))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="no such connection") from exc

    @app.post("/providers/reload")
    async def reload_providers(request: Request) -> dict[str, Any]:
        """Rebuild this instance's provider router from the registry — no restart (slice 4).
        Operator-only. An empty registry leaves the seed router in place (`reloaded:false`)."""
        _require_operator(request)
        if deps.reload_router is None:
            raise HTTPException(status_code=501, detail="router reload not enabled")
        return await deps.reload_router()

    @app.post("/campaigns/{campaign_id}/encounters/{encounter_id}/outcome")
    async def report_outcome(
        campaign_id: str,
        encounter_id: str,
        request: Request,
        bundle: dict[str, Any] = Body(...),  # noqa: B008 (FastAPI DI-style default)
    ) -> dict[str, Any]:
        """Chronicler mode (D-25): an external game reports an outcome bundle; Uro distills it
        into committed events (feats → witness rumors) and notifies the session. AUTHED — an
        external resolver mutates the timeline, so it needs a trusted token like any client."""
        token = request.query_params.get("token") or _bearer_token(request)
        if deps.resolve_participant(token) is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        # A minted token is campaign-scoped (D-39) — an outcome mutates the timeline, so enforce the
        # scope HERE too, not just the WS play channel (D-41 review: this endpoint left it open).
        # Static operator/legacy tokens (token_campaign → None) are server-wide and pass, intended.
        tok_campaign = deps.token_campaign(token) if deps.token_campaign is not None else None
        if tok_campaign is not None and tok_campaign != campaign_id:
            raise HTTPException(status_code=403, detail="token not valid for this campaign")
        if deps.report_outcome is None:
            raise HTTPException(status_code=501, detail="Chronicler mode not enabled")
        try:
            # A malformed/forged bundle (an unknown field under extra='forbid', a bad `v`) raises a
            # pydantic ValidationError (a ValueError) in report_outcome's model_validate — turn it
            # into a loud 400 like every other mutating endpoint (D-41 review: it was a raw 500).
            result = await deps.report_outcome(
                campaign_id, {**bundle, "encounter_id": encounter_id}
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid outcome bundle: {exc}") from exc
        await hub.publish(
            campaign_id, {"type": "outcome_recorded", "encounter_id": encounter_id, **result}
        )
        return result

    # --- management surface (docs/18 B3): the CRUD/read endpoints a non-Python or non-co-located
    # consumer needs (before this, only WS play + outcome existed → every management op forced the
    # library). All authed like the rest; 501 if the deps carry no store. ---

    def _auth(request: Request) -> None:
        token = request.query_params.get("token") or _bearer_token(request)
        if deps.resolve_participant(token) is None:
            raise HTTPException(status_code=401, detail="unauthorized")

    def _scope(request: Request) -> tuple[str, str | None, bool]:
        """The caller's (raw token, resolved participant, is-admin) — for self-or-admin token
        scoping (D-39). A caller may mint/join/revoke for THEMSELVES; only an operator (bootstrap
        --token) credential may act for another participant. Never 'any valid token → any identity'
        (which, with durable minting, would be a PC takeover via pc_for_participant)."""
        token = request.query_params.get("token") or _bearer_token(request)
        caller = deps.resolve_participant(token)
        admin = deps.is_admin(token) if deps.is_admin is not None else False
        return token, caller, admin

    def _mgmt() -> EngineStore:
        if deps.store is None:
            raise HTTPException(status_code=501, detail="management surface not enabled")
        return deps.store

    def _require(body: dict[str, Any], key: str) -> Any:
        """A required body field → 400 (not a bare KeyError→500) when a client omits it."""
        if key not in body:
            raise HTTPException(status_code=400, detail=f"missing required field {key!r}")
        return body[key]

    def _require_operator(request: Request) -> None:
        """OPERATOR-only gate (D-44): 401 on a missing/invalid token, then 403 unless the caller
        holds the operator tier (`--admin-token`). A *structural write* to a world's branch
        topology — fork a branch, mint a marker — is operator-only; a player token can READ the
        tree/log (`_auth`) but not reshape it. Same `is_admin` choke point as `_scope`."""
        _, caller, admin = _scope(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        if not admin:
            raise HTTPException(status_code=403, detail="operator token required")

    def _reject_foreign_scope(request: Request, campaign_id: str) -> None:
        """A MINTED token is campaign-scoped (D-39): reject it on a DIFFERENT campaign's mutating /
        cost-bearing endpoint — else a token authenticates its participant server-wide (holistic
        review: `time-skip`/`dry-run` left this open, unlike the WS channel + outcome endpoint).
        Static operator/legacy `--token` creds (`token_campaign` → None) stay server-wide."""
        token = request.query_params.get("token") or _bearer_token(request)
        tok_campaign = deps.token_campaign(token) if deps.token_campaign is not None else None
        if tok_campaign is not None and tok_campaign != campaign_id:
            raise HTTPException(status_code=403, detail="token not valid for this campaign")

    def _parse_limit(request: Request, default: int) -> int:
        """Parse `?limit=`: a non-integer OR a NEGATIVE value is bad INPUT → 400 (holistic review: a
        negative limit reached Postgres as `LIMIT -1` and 500'd). `0` is valid (empty page)."""
        try:
            limit = int(request.query_params.get("limit") or default)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="limit must be an integer") from exc
        if limit < 0:
            raise HTTPException(status_code=400, detail="limit must not be negative")
        return limit

    @app.post("/worlds")
    async def create_world(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        # OPERATOR-only (D-46, refining D-44): create_world instantiates a fresh world + main branch
        # — the same structural write as `/worlds/import` (operator-only), so it's gated alike.
        _require_operator(request)
        tone = body.get("tone")
        if isinstance(tone, str):  # allow a bare-string tone from a JSON consumer
            tone = [tone]
        try:
            # create_world validates a Reaction-Layer rule_pack LOUDLY (the "silent pack death" fix,
            # docs/18): a malformed pack from a REST client is bad INPUT → 400, not a 500.
            pack = body.get("rule_pack") or None
            w = await _mgmt().create_world(_require(body, "name"), tone=tone, rule_pack=pack)
        except ValueError as exc:  # pydantic ValidationError (bad rule_pack) subclasses ValueError
            raise HTTPException(status_code=400, detail=f"invalid world: {exc}") from exc
        return {"world_id": w.world_id, "main_branch_id": w.main_branch_id, "name": w.name}

    @app.get("/worlds")
    async def list_worlds(request: Request) -> list[dict[str, Any]]:
        _auth(request)
        return [w.model_dump() for w in await _mgmt().list_worlds()]

    @app.post("/worlds/validate")
    async def validate_world_pack(
        request: Request,
        pack: UploadFile = File(...),  # noqa: B008 (FastAPI DI-style default)
    ) -> dict[str, Any]:
        """Validate an uploaded world pack (a `.zip` of the pack directory) → its sufficiency grade
        + gaps (BE-6, `uro world validate`). PARSE-ONLY: nothing is imported, no world state is
        touched — a plain any-authed read-shaped op, guarded against untrusted input (a size cap + a
        zip-slip-safe extraction). The pack-upload CREATE (a structural write, operator-only D-44)
        is a follow-up."""
        _auth(request)
        from uro_core.errors import PackError
        from uro_core.rulesets import registry
        from uro_core.worldpack.parse import parse_pack
        from uro_core.worldpack.sufficiency import check_sufficiency

        data = await pack.read()
        with tempfile.TemporaryDirectory() as tmp:
            root = _safe_extract_pack(data, Path(tmp))
            try:
                parsed = parse_pack(root)
            except PackError as exc:  # a malformed pack is bad INPUT → 400, not a 500
                raise HTTPException(status_code=400, detail=f"invalid pack: {exc}") from exc
            report = check_sufficiency(parsed)
            rid = parsed.manifest.ruleset.id
            return {
                "name": parsed.manifest.name,
                "grade": report.grade,
                "counts": {
                    "places": len(parsed.places),
                    "actors": len(parsed.actors),
                    "factions": len(parsed.factions),
                    "threads": len(parsed.threads),
                },
                "dimensions": [d.model_dump() for d in report.dimensions],
                "ruleset_id": rid,
                "ruleset_ok": rid in registry.available(),
                "gaps": report.gaps,
            }

    @app.post("/worlds/backfill")
    async def backfill_world_pack(
        request: Request,
        pack: UploadFile = File(...),  # noqa: B008 (FastAPI DI-style default)
    ) -> dict[str, Any]:
        """AI-fill an uploaded thin pack's gaps, PREVIEW-only (BE-7, `uro world backfill`) — return
        the augmented seeds (each tagged `provenance=ai_backfill`) + before/after grade, committing
        NOTHING. **OPERATOR-only (D-44):** it makes a live, uncapped LLM call (the engine exposes
        cost, never caps it — docs/00), so a plain player token can't burn model budget. Like
        `/worlds/validate` this is pack-UPLOAD-shaped, not `/worlds/{w}/`: backfill needs the pack's
        manifest/lore (sufficiency gaps), which a stored world doesn't persist. Committing the
        seeds is `world create --backfill` — it rides the deferred pack-upload CREATE endpoint. The
        PoC backfill fills only the `conflict` dimension."""
        _require_operator(request)
        if deps.backfill is None:
            raise HTTPException(status_code=501, detail="backfill not enabled (no provider wired)")
        from uro_core.errors import PackError, ProviderError
        from uro_core.worldpack.parse import parse_pack
        from uro_core.worldpack.sufficiency import check_sufficiency

        data = await pack.read()
        with tempfile.TemporaryDirectory() as tmp:
            root = _safe_extract_pack(data, Path(tmp))
            try:
                parsed = parse_pack(root)
            except PackError as exc:  # a malformed pack is bad INPUT → 400
                raise HTTPException(status_code=400, detail=f"invalid pack: {exc}") from exc
            before = check_sufficiency(parsed)
            try:
                augmented, added = await deps.backfill(parsed)
            except ProviderError as exc:  # a live-model failure is upstream, not bad input → 502
                raise HTTPException(status_code=502, detail=f"provider error: {exc}") from exc
            after = check_sufficiency(augmented)
            seeds = [t.model_dump() for t in augmented.threads if t.provenance == "ai_backfill"]
            return {
                "name": parsed.manifest.name,
                "before_grade": before.grade,
                "after_grade": after.grade,
                "added": added,
                "seeds": seeds,  # the ai_backfill ThreadSeeds (preview — nothing committed)
            }

    @app.post("/worlds/probe")
    async def probe_world_pack(
        request: Request,
        pack: UploadFile = File(...),  # noqa: B008 (FastAPI DI-style default)
    ) -> dict[str, Any]:
        """Run the model-capability probe suite against an uploaded pack (BE-7, `uro world probe`) →
        a judge-scored report (structured-output gate + content-rating), **warn-not-fail** (D-24): a
        weak/refusing model yields `status=warn|fail`, never an error. The report is a `200` and
        `ok` is the machine verdict. **OPERATOR-only (D-44):** it makes several live LLM calls.
        `?tries=` (default 3, capped) governs only the structured-output probe. Pack-upload-shaped
        (probe reads `manifest.content`, not persisted on a stored world)."""
        _require_operator(request)
        if deps.probe is None:
            raise HTTPException(status_code=501, detail="probe not enabled (no provider wired)")
        try:
            tries = int(request.query_params.get("tries") or 3)
        except ValueError as exc:  # ?tries=abc is bad input → 400
            raise HTTPException(status_code=400, detail="tries must be an integer") from exc
        if tries < 1 or tries > 10:  # cap the live-call fan-out (operator, but still bounded)
            raise HTTPException(status_code=400, detail="tries must be between 1 and 10")
        from uro_core.errors import PackError, ProviderError
        from uro_core.worldpack.parse import parse_pack

        data = await pack.read()
        with tempfile.TemporaryDirectory() as tmp:
            root = _safe_extract_pack(data, Path(tmp))
            try:
                parsed = parse_pack(root)
            except PackError as exc:
                raise HTTPException(status_code=400, detail=f"invalid pack: {exc}") from exc
            try:
                report = await deps.probe(parsed.manifest, tries)
            except ProviderError as exc:
                raise HTTPException(status_code=502, detail=f"provider error: {exc}") from exc
            body = report.model_dump()
            # ok / warnings are @property → not in model_dump(); surface them explicitly as the
            # machine verdict + human summary (warn-not-fail: a failing probe is still a 200).
            body["ok"] = report.ok
            body["warnings"] = report.warnings
            return body

    @app.get("/worlds/{world_id}/branches")
    async def list_world_branches(world_id: str, request: Request) -> dict[str, Any]:
        """The branch tree + markers for a world (docs/03, docs/18 B3). A plain any-authed READ:
        `_auth` only, NO operator gate — D-44 scopes only structural writes / act-for-another to
        `is_admin`; reads stay open. Mirrors `uro branch list`: each branch carries its in-fiction
        `world_day` (default 0 when the branch has no world_time events yet)."""
        _auth(request)
        store = _mgmt()
        if await store.get_world(world_id) is None:
            raise HTTPException(status_code=404, detail="no such world")
        branches = await store.list_branches(world_id)
        days = await store.current_world_time_batch([b.branch_id for b in branches])
        markers = await store.list_markers(world_id)
        return {
            "branches": [
                {**b.model_dump(), "world_day": days.get(b.branch_id, 0)} for b in branches
            ],
            "markers": [m.model_dump() for m in markers],
        }

    @app.post("/worlds/{world_id}/branches")
    async def fork_world_branch(
        world_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Fork a branch from any commit or marker (BE-2, docs/03). OPERATOR-only (D-44 — a fork is
        a structural write: copy-on-fork rebuilds projections + copies memory rows). `from_ref` is a
        marker name OR a raw commit id (markers win on collision). `time_skip_days>0` advances
        in-fiction time on the new branch and fires downtime agenda rules — parity with
        `uro branch fork --time-skip-days`."""
        _require_operator(request)
        store = _mgmt()
        if await store.get_world(world_id) is None:
            raise HTTPException(status_code=404, detail="no such world")
        from_ref = _require(body, "from_ref")
        name = _require(body, "name")
        try:
            days = int(body.get("time_skip_days") or 0)
        except (TypeError, ValueError) as exc:  # a non-numeric time_skip_days is bad input, not 500
            raise HTTPException(
                status_code=400, detail="time_skip_days must be an integer"
            ) from exc
        if days < 0:
            raise HTTPException(status_code=400, detail="time_skip_days must be >= 0")
        try:
            # fork_branch resolves the ref internally: a duplicate branch name → ValueError, an
            # unknown marker/commit → KeyError. Both are bad INPUT → 400 (never a raw 500).
            branch = await store.fork_branch(world_id, from_ref, name)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = (
            branch.model_dump()
        )  # {branch_id, world_id, name, head_commit(fork pt), forked_from}
        if days > 0:
            if deps.advance_branch_time is None:
                raise HTTPException(status_code=501, detail="time-skip not enabled")
            # A separate txn from the fork (same non-atomicity as the CLI): the fork is durable; a
            # failed skip leaves the new branch standing at the fork point.
            result["world_day"] = await deps.advance_branch_time(branch.branch_id, days)
            fresh = await store.get_branch(
                branch.branch_id
            )  # the head advanced past the fork point
            if fresh is not None:
                result["head_commit"] = fresh.head_commit
        return result

    @app.post("/worlds/{world_id}/markers")
    async def create_world_marker(
        world_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Name a branch's current head with an immutable marker (BE-3, docs/03). OPERATOR-only
        (D-44 — a structural ref write). Markers name a branch HEAD, not an arbitrary commit;
        `branch` defaults to `main`. Mirrors `uro branch mark`."""
        _require_operator(request)
        store = _mgmt()
        if await store.get_world(world_id) is None:
            raise HTTPException(status_code=404, detail="no such world")
        name = _require(body, "name")
        branch = str(body.get("branch") or "main")
        b = await store.get_branch_by_name(world_id, branch)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no such branch: {branch}")
        try:
            marker = await store.create_marker(world_id, name, b.branch_id)
        except ValueError as exc:  # duplicate marker name (UNIQUE per world) = bad input
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return marker.model_dump()

    @app.get("/worlds/{world_id}/log")
    async def world_branch_log(world_id: str, request: Request) -> dict[str, Any]:
        """A branch's commit lineage, git-log style (BE-3, docs/03). A plain any-authed READ
        (`_auth`). Head→genesis order; `?branch=` (default `main`), `?limit=` (default 20)."""
        _auth(request)
        store = _mgmt()
        if await store.get_world(world_id) is None:
            raise HTTPException(status_code=404, detail="no such world")
        branch = request.query_params.get("branch") or "main"
        limit = _parse_limit(request, 20)
        b = await store.get_branch_by_name(world_id, branch)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no such branch: {branch}")
        entries = await store.lineage(b.branch_id, limit)
        return {
            "branch": branch,
            "head_depth": b.head_depth,
            "entries": [e.model_dump() for e in entries],
        }

    @app.get("/worlds/{world_id}/events")
    async def world_events(world_id: str, request: Request) -> dict[str, Any]:
        """The raw event log along a branch, filterable (BE-4, docs/12). OPERATOR-only (D-45): the
        raw log carries omniscient truth — `ClaimRecorded` truth-values, hidden beliefs, `caused_by`
        — so it is a GM/operator observability surface, never a player read. `?branch=` (default
        `main`); optional `?type=`, `?entity_ref=`, `?caused_by=`; `?limit=` (default 50)."""
        _require_operator(request)
        store = _mgmt()
        if await store.get_world(world_id) is None:
            raise HTTPException(status_code=404, detail="no such world")
        branch = request.query_params.get("branch") or "main"
        limit = _parse_limit(request, 50)
        b = await store.get_branch_by_name(world_id, branch)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no such branch: {branch}")
        events = await store.branch_events(
            b.branch_id,
            event_type=request.query_params.get("type"),
            entity_ref=request.query_params.get("entity_ref"),
            caused_by=request.query_params.get("caused_by"),
            limit=limit,
        )
        return {"branch": branch, "events": [e.model_dump() for e in events]}

    @app.get("/worlds/{world_id}/commits/{commit_id}")
    async def world_commit_detail(
        world_id: str, commit_id: str, request: Request
    ) -> dict[str, Any]:
        """One commit's ordered events + metadata (BE-4, docs/12). OPERATOR-only (D-45 — raw
        events). 404 if the commit is not in this world."""
        _require_operator(request)
        detail = await _mgmt().commit_detail(world_id, commit_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="no such commit")
        return detail.model_dump()

    @app.get("/worlds/{world_id}/chronicle")
    async def world_chronicle(world_id: str, request: Request) -> dict[str, Any]:
        """A branch's recent beats, world-scoped (BE-10, docs/08) — the world twin of the
        campaign chronicle. OPERATOR-only: unlike the campaign read (a player's own current
        branch), this reads ANY named branch — including sibling what-if forks the player isn't
        in — so it's a GM/operator timeline-inspection surface (same family as `/log`, `/events`).
        `?branch=` (default `main`), `?limit=` (default 20)."""
        _require_operator(request)
        store = _mgmt()
        if await store.get_world(world_id) is None:
            raise HTTPException(status_code=404, detail="no such world")
        branch = str(request.query_params.get("branch") or "main")
        b = await store.get_branch_by_name(world_id, branch)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no such branch: {branch}")
        limit = _parse_limit(request, 20)
        beats = await store.recent_beats(b.branch_id, limit)
        return {"branch": branch, "beats": [bt.model_dump() for bt in beats]}

    @app.get("/worlds/{world_id}/export")
    async def export_world_bundle(world_id: str, request: Request) -> dict[str, Any]:
        """Export the whole world as a portable, hash-chain-verified bundle (BE-8, docs/08) —
        mirrors `uro world export`. OPERATOR-only (D-45): the bundle carries the ENTIRE event log
        (omniscient truth + beliefs), so it's bulk disclosure, never player-facing. The response IS
        the `.uwp` JSON; a consumer saves it verbatim and can re-import it anywhere. The bundle is
        materialized in memory (the log can be large) — cap it at a reverse proxy for production."""
        _require_operator(request)
        store = _mgmt()
        if await store.get_world(world_id) is None:
            raise HTTPException(status_code=404, detail="no such world")
        bundle = await store.export_world(world_id)
        return bundle.model_dump(mode="json")

    @app.post("/worlds/import")
    async def import_world_bundle(
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Import a world bundle (BE-8, docs/08) — mirrors `uro world import`. OPERATOR-only (D-44:
        a structural write that instantiates a fresh world). The bundle's SHA-256 hash chain is
        recomputed and a tampered/altered bundle is rejected with 400 BEFORE anything is written;
        on success the world is re-instantiated with remapped ids and projections rebuilt by replay.
        The bundle rides in the JSON body (buffered in memory, like the CLI reading the `.uwp`
        file) — cap the body at a reverse proxy for production."""
        _require_operator(request)
        store = _mgmt()
        try:
            bundle = WorldBundle.model_validate(body)
        except ValidationError as exc:  # a body that isn't a well-formed bundle is bad input
            raise HTTPException(status_code=400, detail=f"malformed bundle: {exc}") from exc
        try:
            world = await store.import_world(bundle)  # verify_bundle inside → ExportError on tamper
        except ExportError as exc:
            raise HTTPException(
                status_code=400, detail=f"bundle failed verification: {exc}"
            ) from exc
        return world.model_dump()  # {world_id (remapped), name, main_branch_id}

    @app.post("/worlds/{world_id}/campaigns")
    async def create_campaign(
        world_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        _auth(request)
        store = _mgmt()
        world = await store.get_world(world_id)
        if world is None:
            raise HTTPException(status_code=404, detail="no such world")
        participant = _require(body, "participant")
        # Self-or-admin (D-39, holistic review): a caller may start a campaign only for THEMSELVES;
        # only an operator may name another participant — else a player could bind a PCBound naming
        # someone else on the shared main branch (mirrors the `join` guard).
        _, caller, admin = _scope(request)
        if participant != caller and not admin:
            raise HTTPException(
                status_code=403,
                detail="only an operator may start a campaign for another participant",
            )
        new_pc_name = body.get("new_pc_name")
        adopt_actor_id = body.get("adopt_actor_id")
        try:  # a non-int seed (e.g. a JSON array) is TypeError — the block below wouldn't catch it
            seed = int(body.get("seed") or 0)  # docs/18 G-3: reproducible-combat seed
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="seed must be an integer") from exc
        try:
            # Pin the WORLD's declared ruleset + sheet the PC (D-30 + Phase-3), like the CLI `uro
            # campaign new` path — a REST-created PbtA campaign must not silently fall to an empty
            # pin + no sheet (which bypasses the WS cross-ruleset guard and disables mechanics).
            world_rid, world_rver = await store.world_ruleset(world.main_branch_id)
            pc_sheet: dict[str, Any] | None = None
            if new_pc_name is not None or (
                adopt_actor_id is not None
                and await store.get_sheet(world.main_branch_id, adopt_actor_id) is None
            ):
                pc_sheet, ruleset_id = _default_sheet(world_rid, world_rver)
            else:  # adopting an already-sheeted actor: keep its sheet, still pin the ruleset
                ruleset_id = _resolve_ruleset_id(world_rid, world_rver)
            c = await store.start_campaign(
                world_id,
                world.main_branch_id,
                participant_id=participant,
                new_pc_name=new_pc_name,
                adopt_actor_id=adopt_actor_id,
                pc_sheet=pc_sheet,
                ruleset_id=ruleset_id,
                ruleset_version=world_rver,
                seed=seed,
            )
        except (ValueError, KeyError) as exc:  # bad PC choice, or world pins an absent ruleset
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"campaign_id": c.campaign_id, "branch_id": c.branch_id}

    def _campaign_view(c: Campaign, admin: bool) -> dict[str, Any]:
        # The mechanics RNG `seed` makes deterministic combat predictable (holistic review) — it's
        # GM data, not a player read. Expose it only to an operator; strip it for a player token.
        return c.model_dump() if admin else c.model_dump(exclude={"seed"})

    @app.get("/campaigns")
    async def list_campaigns(request: Request) -> list[dict[str, Any]]:
        _auth(request)
        _, _, admin = _scope(request)
        world_id = request.query_params.get("world_id")
        return [_campaign_view(c, admin) for c in await _mgmt().list_campaigns(world_id)]

    @app.get("/campaigns/{campaign_id}")
    async def get_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        _, _, admin = _scope(request)
        c = await _mgmt().get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        return _campaign_view(c, admin)

    @app.post("/campaigns/{campaign_id}/join")
    async def join_campaign(
        campaign_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        _auth(request)
        store = _mgmt()
        campaign = await store.get_campaign(campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        participant = _require(body, "participant")
        # Scoped mint (D-39): a caller may seat/mint for THEMSELVES; only an operator credential may
        # seat another — else a durable token for an arbitrary identity would be a PC takeover.
        _, caller, admin = _scope(request)
        if participant != caller and not admin:
            raise HTTPException(
                status_code=403,
                detail="can only join as yourself; seating another needs an operator --token",
            )
        new_pc_name = body.get("new_pc_name")
        adopt_actor_id = body.get("adopt_actor_id")
        try:
            # Sheet the joining PC from the campaign's ruleset (D-30 + Phase-3), like CLI join.
            pc_sheet: dict[str, Any] | None = None
            if new_pc_name is not None or (
                adopt_actor_id is not None
                and await store.get_sheet(campaign.branch_id, adopt_actor_id) is None
            ):
                pc_sheet, _ = _default_sheet(campaign.ruleset_id, campaign.ruleset_version)
            actor_id = await store.bind_pc(
                campaign_id,
                participant,
                new_pc_name=new_pc_name,
                adopt_actor_id=adopt_actor_id,
                pc_sheet=pc_sheet,
                ruleset_id=campaign.ruleset_id,
            )
        except (ValueError, KeyError) as exc:  # bad PC choice, or an absent pinned ruleset
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result: dict[str, Any] = {"actor_id": actor_id}
        # Mint-on-join (D-39): the blessed path to a LIVE credential — a runtime-added player gets a
        # durable token naming EXACTLY the participant just bound on THIS campaign (no restart, no
        # arbitrary-identity mint). Only when the deps carry a token registry (a store-backed one).
        if deps.mint_token is not None:
            result["token"] = await deps.mint_token(participant, campaign_id)
        return result

    @app.post("/campaigns/{campaign_id}/tokens")
    async def mint_token_endpoint(
        campaign_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Standalone scoped mint (D-39) — the fallback to mint-on-join: issue a durable token for
        an ALREADY-SEATED participant (self-or-admin scope; requires an existing PC binding, so a
        token can't be minted for an unbound identity)."""
        _auth(request)
        if deps.mint_token is None:
            raise HTTPException(status_code=501, detail="runtime token management not enabled")
        participant = _require(body, "participant")
        _, caller, admin = _scope(request)
        if participant != caller and not admin:
            raise HTTPException(
                status_code=403, detail="can only mint for yourself (or as operator)"
            )
        store = _mgmt()
        if await store.pc_for_participant(campaign_id, participant) is None:
            raise HTTPException(
                status_code=400, detail="participant has no PC on this campaign — join first"
            )
        return {"token": await deps.mint_token(participant, campaign_id)}

    @app.post("/campaigns/{campaign_id}/tokens/revoke")
    async def revoke_token_endpoint(
        campaign_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Revoke a token (D-39), self-or-admin scoped. Blocks a NEW connect immediately; a live
        socket survives until it disconnects (auth is checked once before accept — a residual)."""
        _auth(request)
        if deps.revoke_token is None:
            raise HTTPException(status_code=501, detail="runtime token management not enabled")
        target = _require(body, "token")
        _, caller, admin = _scope(request)
        owner = deps.resolve_participant(target)  # whose token is it?
        if owner is not None and owner != caller and not admin:
            raise HTTPException(
                status_code=403, detail="can only revoke your own token (or as operator)"
            )
        return {"revoked": await deps.revoke_token(target)}

    @app.get("/campaigns/{campaign_id}/roster")
    async def campaign_roster(campaign_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        store = _mgmt()
        if await store.get_campaign(campaign_id) is None:  # 404 like every sibling read (was 200)
            raise HTTPException(status_code=404, detail="no such campaign")
        return {"pcs": await store.campaign_pcs(campaign_id)}

    @app.get("/campaigns/{campaign_id}/state")
    async def campaign_state(campaign_id: str, request: Request) -> dict[str, Any]:
        _auth(request)  # 401 on a bad/missing token
        _, _, admin = _scope(request)  # operator tier decides which sections are readable
        store = _mgmt()
        c = await store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        raw = request.query_params.get("sections") or "actors,threads,places,factions"
        sections = raw.split(",")
        # D-45 epistemic boundary: a PLAYER may read only the non-omniscient scene sections; the
        # omniscient projections (claims' truth values, hidden beliefs, sheets/items/edges/counters)
        # are operator-only observability (holistic review — the previous any-authed pass leaked the
        # raw truth-tagged log, the exact bypass D-45's rider named). Operators read anything.
        if not admin:
            forbidden = [s for s in sections if s not in _PLAYER_SAFE_SECTIONS]
            if forbidden:
                raise HTTPException(
                    status_code=403,
                    detail=f"operator token required for: {', '.join(sorted(forbidden))}",
                )
        try:
            # query_across raises on an unknown section (typo-loud, B5) — a client typo is bad
            # INPUT → 400, matching every mutating endpoint (not a bare 500).
            across = await store.query_across([c.branch_id], sections)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"branch_id": c.branch_id, "state": across.get(c.branch_id, {})}

    @app.get("/campaigns/{campaign_id}/chronicle")
    async def campaign_chronicle(campaign_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        store = _mgmt()
        c = await store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        limit = _parse_limit(request, 20)
        beats = await store.recent_beats(c.branch_id, limit)
        return {"beats": [b.model_dump() for b in beats]}

    _MAX_TIMESKIP_DAYS = 100 * 365  # cap the in-fiction jump (a runaway skip fires N agenda rounds)

    @app.post("/campaigns/{campaign_id}/time-skip")
    async def campaign_time_skip(
        campaign_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        # OPERATOR-only (D-46, refining D-44): a time-skip commits TimeAdvanced + fires downtime
        # agenda rules (belief/edge/rumor churn) on the SHARED campaign branch — the same structural
        # timeline write as the operator-only fork time-skip + end_campaign (holistic review).
        _require_operator(request)
        _reject_foreign_scope(request, campaign_id)
        if deps.advance_time is None:
            raise HTTPException(status_code=501, detail="time-skip not enabled")
        try:
            days = int(_require(body, "days"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="days must be an integer") from exc
        if days <= 0:  # store.time_skip rejects this deeper (a 500); catch it here as bad input
            raise HTTPException(status_code=400, detail="days must be a positive integer")
        if days > _MAX_TIMESKIP_DAYS:
            raise HTTPException(status_code=400, detail=f"days must be <= {_MAX_TIMESKIP_DAYS}")
        return await deps.advance_time(campaign_id, days)

    @app.post("/campaigns/{campaign_id}/dry-run")
    async def dry_run_beat(
        campaign_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Dry-run a beat: run the full pipeline and return the events it WOULD commit, writing
        NOTHING (BE-5, mirrors `uro dry-run`). Any-authed — the non-committing twin of a play beat
        (the WS channel is any-authed too); INTENT-ONLY (no client `plan=`, D-37). Acting PC = the
        token's participant (solo fallback if unbound). A minted token is campaign-scoped (D-39):
        it can't dry-run a FOREIGN campaign (holistic review — it burns LLM + reads state)."""
        _auth(request)
        _reject_foreign_scope(request, campaign_id)
        if deps.preview_beat is None:
            raise HTTPException(status_code=501, detail="dry-run not enabled")
        intent = str(_require(body, "intent"))
        if not intent.strip():
            raise HTTPException(status_code=400, detail="intent must be non-empty")
        _, participant, _ = _scope(request)  # token → acting participant (non-None past _auth)
        try:
            events = await deps.preview_beat(campaign_id, participant or "", intent)
        except (ValueError, KeyError) as exc:
            # ValueError: a different-ruleset pin (one ruleset/process). KeyError: an unbound router
            # role (a residual incomplete-registry gap) → clean 400, not a raw 500.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"events": events}

    @app.get("/campaigns/{campaign_id}/consistency")
    async def campaign_consistency(campaign_id: str, request: Request) -> dict[str, Any]:
        """The narrator contradiction-survival proxy (T2, BE-5, `uro consistency`). A plain
        any-authed read — a metric count, not omniscient truth. `ratio` = consistent/total
        (1.0 when there are no claims yet)."""
        _auth(request)
        store = _mgmt()
        c = await store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        consistent, total = await store.fact_consistency(c.branch_id)
        return {
            "consistent": consistent,
            "total": total,
            "ratio": consistent / total if total else 1.0,
        }

    @app.post("/campaigns/{campaign_id}/end")
    async def end_campaign_endpoint(
        campaign_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """End a campaign: release its PCs to NPCs and mark + snapshot the closing commit as a fork
        root (BE-9, `uro campaign end`). OPERATOR-only (D-44 — a timeline lifecycle write). Body:
        `{marker, outcome?}`."""
        _require_operator(request)
        store = _mgmt()
        if await store.get_campaign(campaign_id) is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        marker = _require(body, "marker")
        try:
            m = await store.end_campaign(
                campaign_id, marker, outcome=str(body.get("outcome") or "")
            )
        except (ValueError, KeyError) as exc:  # dup marker name / already ended = bad input
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return m.model_dump()

    @app.get("/campaigns/{campaign_id}/codex")
    async def list_codex(campaign_id: str, request: Request) -> dict[str, Any]:
        """A participant's out-of-world notes for this campaign's world (BE-9, `uro codex list`).
        SELF-or-admin (D-39): a caller reads their OWN codex; an operator may read another's
        (`?participant=`). The codex is world-scoped and fork-surviving (D-36); never canon."""
        _auth(request)
        store = _mgmt()
        c = await store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        _, caller, admin = _scope(request)
        target = request.query_params.get("participant") or caller
        if target != caller and not admin:
            raise HTTPException(
                status_code=403, detail="can only read your own codex (or as operator)"
            )
        notes = await store.participant_notes(target or "", c.world_id)
        return {"participant": target, "notes": [n.model_dump() for n in notes]}

    @app.post("/campaigns/{campaign_id}/codex")
    async def add_codex(
        campaign_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        """Record an out-of-world player note that SURVIVES a fork (BE-9, `uro codex add`; D-36).
        SELF-or-admin (D-39). Body: `{text, participant?, key?, pinned?, refs?}`. Never canon, never
        an NPC belief — surfaces only to the narrator as the player's private recollection."""
        _auth(request)
        store = _mgmt()
        c = await store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        text = str(_require(body, "text"))
        _, caller, admin = _scope(request)
        target = str(body.get("participant") or caller or "")
        if target != caller and not admin:
            raise HTTPException(
                status_code=403, detail="can only write your own codex (or as operator)"
            )
        key = await store.participant_remember(
            target,
            c.world_id,
            text,
            key=body.get("key"),
            pinned=bool(body.get("pinned", False)),
            entity_refs=body.get("refs") or [],
        )
        return {"participant": target, "key": key}

    @app.websocket("/campaigns/{campaign_id}/play")
    async def play(ws: WebSocket, campaign_id: str) -> None:
        # Auth (docs/08 token mode): ?token=… → participant_id. Reject before accept.
        token = ws.query_params.get("token", "")
        participant = deps.resolve_participant(token)
        if participant is None:
            await ws.close(code=4401)  # unauthorized
            return
        if not await deps.campaign_exists(campaign_id):
            await ws.close(code=4404)  # no such campaign
            return
        # A MINTED token is scoped to the campaign it was minted for (D-39 review): reject it on a
        # DIFFERENT campaign's play channel — else a token authenticates its participant server-wide
        # and could drive another campaign's PC (cross-campaign hijack). Static operator/legacy
        # --token creds are intentionally server-wide (token_campaign → None) and unaffected.
        tok_campaign = deps.token_campaign(token) if deps.token_campaign is not None else None
        if tok_campaign is not None and tok_campaign != campaign_id:
            await ws.close(code=4403)  # token not valid for this campaign
            return
        await ws.accept()
        # Everything fallible (a DB round-trip in pc_seats) runs INSIDE the try, so a transient
        # failure can't orphan the hub subscription / turn-roster entry (D-39 review: an unguarded
        # pre-try failure leaked the queue forever). The finally cleans up whatever was set up.
        queue: asyncio.Queue[dict[str, Any]] | None = None
        forward: asyncio.Task[None] | None = None
        joined = False
        try:
            queue = hub.subscribe(campaign_id)
            # Seed the arbiter's ring in DURABLE bind order (D-39, G-18) so reconnect/restart re-
            # forms the SAME order regardless of connect race; None (fake-deps/no store) → append.
            seats = await deps.store.pc_seats(campaign_id) if deps.store is not None else None
            await arb.note_joined(campaign_id, participant, seats)  # add to the turn roster (OQ-7)
            joined = True
            await hub.publish(
                campaign_id, {"type": "participant_joined", "participant_id": participant}
            )
            forward = asyncio.create_task(_forward(ws, queue))  # fan hub → this socket
            while True:
                msg = await ws.receive_json()
                mtype = msg.get("type")
                if mtype == "intent":
                    await _run_and_broadcast(
                        deps, arb, hub, campaign_id, participant, str(msg.get("text", ""))
                    )
                elif mtype == "table_talk":
                    # The NON-CANON coordination lane (D-38): out-of-world debate/proposals. It only
                    # calls hub.publish and returns — it NEVER reaches _run_and_broadcast → run_beat
                    # → append_beat, so by CONSTRUCTION it cannot move the branch head or mint an
                    # event. This structural guarantee (not a policy) lets the party propose,
                    # debate, and vote without burning a canonical beat.
                    talk = str(msg.get("text", ""))
                    if talk.strip():
                        await hub.publish(
                            campaign_id,
                            {"type": "table_talk", "participant_id": participant, "text": talk},
                        )
                elif mtype == "vote":
                    # A consensus/vote arbiter (D-38, G-11) tallies votes in session-only state and
                    # broadcasts the running tally on the non-canon lane; a DECIDED vote is
                    # announced but still enacted as an ordinary beat by the turn-holder
                    # (take_pending deferred). If THIS server's arbiter has no vote shape, say so
                    # rather than silently swallow it (D-38 review) — the CLI advertises /vote
                    # unconditionally and can't know the server's arbiter.
                    choice = str(msg.get("choice", ""))
                    if not isinstance(arb, VoteCoordinator):
                        await hub.publish(
                            campaign_id,
                            {"type": "vote_unsupported", "participant_id": participant},
                        )
                    elif choice.strip():
                        outcome = await arb.cast_vote(campaign_id, participant, choice)
                        await hub.publish(
                            campaign_id,
                            {
                                "type": "vote_tally",
                                "participant_id": participant,
                                "choice": choice,
                                "tally": outcome.tally,
                            },
                        )
                        if outcome.decided is not None:
                            await hub.publish(
                                campaign_id,
                                {"type": "vote_decided", "choice": outcome.decided},
                            )
        except WebSocketDisconnect:
            pass
        finally:
            if forward is not None:
                forward.cancel()
            if queue is not None:
                hub.unsubscribe(campaign_id, queue)
            if joined:  # only tear down a roster entry we created (guards a pre-join failure)
                await arb.note_left(campaign_id, participant)  # drop from the turn roster (OQ-7)
                # A departure can COMPLETE a pending vote round — the last holdout LEFT instead of
                # voting — which cast_vote never re-checks (D-38 review). Recompute and announce it,
                # else the round would silently never resolve.
                if isinstance(arb, VoteCoordinator):
                    pending = await arb.resolve_pending(campaign_id)
                    if pending is not None and pending.decided is not None:
                        await hub.publish(
                            campaign_id, {"type": "vote_decided", "choice": pending.decided}
                        )
                await hub.publish(
                    campaign_id, {"type": "participant_left", "participant_id": participant}
                )

    return app


async def _forward(ws: WebSocket, queue: asyncio.Queue[dict[str, object]]) -> None:
    """Pump this connection's queue to its socket until cancelled."""
    while True:
        message = await queue.get()
        if ws.application_state != WebSocketState.CONNECTED:
            return
        await ws.send_json(message)


async def _run_and_broadcast(
    deps: ServerDeps,
    arbiter: TurnArbiter,
    hub: SessionHub,
    campaign_id: str,
    participant: str,
    text: str,
) -> None:
    """Admit the intent, run the beat, and broadcast its narration + commit to the whole session
    (docs/08 broadcast-shaped output) — so every connected client sees the SAME beat."""
    if not text.strip():
        return
    decision = await arbiter.admit(campaign_id, participant, text)
    if decision == AdmitDecision.NOT_YOUR_TURN:
        # Valid intent, but another participant holds the turn (round-robin, OQ-7) — tell the
        # client to hold; it is NOT rejected, so a client may retry when the token rotates.
        await hub.publish(
            campaign_id, {"type": "not_your_turn", "participant_id": participant, "text": text}
        )
        return
    if decision == AdmitDecision.QUEUED:
        # A proposal-window arbiter HELD this non-holder intent as a PROPOSAL (D-38, G-10): surface
        # it to the whole table — no beat, no rotation — so the party can debate it on the
        # lane and the holder can enact it on their turn. NOT a rejection (the client may see it
        # acted on next); NOT a silent not_your_turn (a proposal is a first-class, visible event).
        await hub.publish(
            campaign_id, {"type": "proposal_opened", "participant_id": participant, "text": text}
        )
        return
    if decision != AdmitDecision.ADMITTED:  # REJECTED → no beat now
        await hub.publish(
            campaign_id, {"type": "intent_rejected", "participant_id": participant, "text": text}
        )
        return
    await hub.publish(
        campaign_id, {"type": "beat_started", "participant_id": participant, "intent": text}
    )
    chunks: list[str] = []
    try:
        async for chunk in deps.run_beat(campaign_id, participant, text):
            chunks.append(chunk)
            await hub.publish(
                campaign_id,
                {"type": "narration_chunk", "participant_id": participant, "text": chunk},
            )
    except Exception:
        # A beat failure (e.g. a ruleset mismatch, a provider error) must NOT crash the WS
        # connection — broadcast a graceful failure and keep the session alive (mirrors the CLI
        # play loop's "beat failed; nothing was saved"). Nothing was committed (pre-commit crash).
        # The raw exception is logged server-side, NOT fanned out to every client (holistic review:
        # str(exc) can carry internal detail — an info-disclosure across participants).
        logger.exception("beat failed for participant %s on campaign %s", participant, campaign_id)
        await hub.publish(
            campaign_id,
            {
                "type": "beat_failed",
                "participant_id": participant,
                "intent": text,
                "error": "beat failed; nothing was saved",
            },
        )
        # A failed turn STILL yields the token (cross-phase review P7xP3): otherwise a
        # deterministically-failing holder (e.g. an unbound participant) would wedge the whole
        # party forever. The trade-off — a transient failure costs a turn — is acceptable; the
        # player gets the token back next round.
        await arbiter.beat_committed(campaign_id, participant, "")
        return
    await hub.publish(
        campaign_id,
        {
            "type": "beat_committed",
            "participant_id": participant,
            "intent": text,
            "narration": "".join(chunks).strip(),
        },
    )
    # The beat committed — let the arbiter rotate the turn token to the next participant (OQ-7).
    await arbiter.beat_committed(campaign_id, participant, "")
