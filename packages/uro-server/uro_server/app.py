"""The FastAPI shell (docs/08): transport, sessions, auth, wiring — NO engine logic.

Everything the engine does is reached through `ServerDeps` (a small port): token→participant
resolution, campaign existence, and a streaming beat runner. Production wires it to a connected
store + Engine (`engine_deps`); tests inject a fake — so the transport (auth, broadcast fan-out,
the WS play channel) is exercised without a live DB or model. The heavy engine path is tested
directly in uro-core.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from uro_core.pipeline.engine import Engine
from uro_core.ports.projections import EngineStore
from uro_core.session import AdmitDecision, SoloArbiter, TurnArbiter

from uro_server.sessions import SessionHub


def _bearer_token(request: Request) -> str:
    """Extract a bearer token from the Authorization header, or ''."""
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


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


def engine_deps(store: EngineStore, engine: Engine, tokens: dict[str, str]) -> ServerDeps:
    """Production wiring: a connected store + Engine behind the transport port. `tokens` maps a
    bearer token to a participant_id (docs/08 token mode)."""

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
        commit = await store.append_beat(campaign.branch_id, result.events)
        # Reaction Layer (docs/17, D-33): an EXTERNAL death is the war-story premise — combat is
        # non-lethal (lethal=False), so the Chronicler is the only runtime ActorDied source, and a
        # pack rule triggering on ActorDied must fire here too, not only on the run_beat path.
        # react() is exception-isolated — the outcome beat is already durable.
        await engine.react(campaign, commit.commit_id, result.events)
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

    return ServerDeps(
        resolve_participant=lambda token: tokens.get(token),
        campaign_exists=campaign_exists,
        run_beat=run_beat,
        report_outcome=report_outcome,
        store=store,
        advance_time=advance_time,
    )


def create_app(deps: ServerDeps, *, arbiter: TurnArbiter | None = None) -> FastAPI:
    app = FastAPI(title="Uro Engine server")
    hub = SessionHub()
    arb = arbiter or SoloArbiter()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
        if deps.report_outcome is None:
            raise HTTPException(status_code=501, detail="Chronicler mode not enabled")
        result = await deps.report_outcome(campaign_id, {**bundle, "encounter_id": encounter_id})
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

    def _mgmt() -> EngineStore:
        if deps.store is None:
            raise HTTPException(status_code=501, detail="management surface not enabled")
        return deps.store

    def _require(body: dict[str, Any], key: str) -> Any:
        """A required body field → 400 (not a bare KeyError→500) when a client omits it."""
        if key not in body:
            raise HTTPException(status_code=400, detail=f"missing required field {key!r}")
        return body[key]

    @app.post("/worlds")
    async def create_world(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        _auth(request)
        tone = body.get("tone")
        if isinstance(tone, str):  # allow a bare-string tone from a JSON consumer
            tone = [tone]
        w = await _mgmt().create_world(
            _require(body, "name"), tone=tone, rule_pack=body.get("rule_pack") or None
        )
        return {"world_id": w.world_id, "main_branch_id": w.main_branch_id, "name": w.name}

    @app.get("/worlds")
    async def list_worlds(request: Request) -> list[dict[str, Any]]:
        _auth(request)
        return [w.model_dump() for w in await _mgmt().list_worlds()]

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
        try:
            c = await store.start_campaign(
                world_id,
                world.main_branch_id,
                participant_id=_require(body, "participant"),
                new_pc_name=body.get("new_pc_name"),
                adopt_actor_id=body.get("adopt_actor_id"),
            )
        except ValueError as exc:  # "exactly one of adopt_actor_id / new_pc_name"
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"campaign_id": c.campaign_id, "branch_id": c.branch_id}

    @app.get("/campaigns")
    async def list_campaigns(request: Request) -> list[dict[str, Any]]:
        _auth(request)
        world_id = request.query_params.get("world_id")
        return [c.model_dump() for c in await _mgmt().list_campaigns(world_id)]

    @app.get("/campaigns/{campaign_id}")
    async def get_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        c = await _mgmt().get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        return c.model_dump()

    @app.post("/campaigns/{campaign_id}/join")
    async def join_campaign(
        campaign_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        _auth(request)
        store = _mgmt()
        if await store.get_campaign(campaign_id) is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        try:
            actor_id = await store.bind_pc(
                campaign_id,
                _require(body, "participant"),
                new_pc_name=body.get("new_pc_name"),
                adopt_actor_id=body.get("adopt_actor_id"),
            )
        except ValueError as exc:  # "exactly one of adopt_actor_id / new_pc_name"
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"actor_id": actor_id}

    @app.get("/campaigns/{campaign_id}/roster")
    async def campaign_roster(campaign_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        return {"pcs": await _mgmt().campaign_pcs(campaign_id)}

    @app.get("/campaigns/{campaign_id}/state")
    async def campaign_state(campaign_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        store = _mgmt()
        c = await store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        raw = request.query_params.get("sections") or "actors,threads,places,factions"
        sections = raw.split(",")
        across = await store.query_across([c.branch_id], sections)
        return {"branch_id": c.branch_id, "state": across.get(c.branch_id, {})}

    @app.get("/campaigns/{campaign_id}/chronicle")
    async def campaign_chronicle(campaign_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        store = _mgmt()
        c = await store.get_campaign(campaign_id)
        if c is None:
            raise HTTPException(status_code=404, detail="no such campaign")
        limit = int(request.query_params.get("limit") or 20)
        beats = await store.recent_beats(c.branch_id, limit)
        return {"beats": [b.model_dump() for b in beats]}

    @app.post("/campaigns/{campaign_id}/time-skip")
    async def campaign_time_skip(
        campaign_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),  # noqa: B008
    ) -> dict[str, Any]:
        _auth(request)
        if deps.advance_time is None:
            raise HTTPException(status_code=501, detail="time-skip not enabled")
        try:
            days = int(_require(body, "days"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="days must be an integer") from exc
        return await deps.advance_time(campaign_id, days)

    @app.websocket("/campaigns/{campaign_id}/play")
    async def play(ws: WebSocket, campaign_id: str) -> None:
        # Auth (docs/08 token mode): ?token=… → participant_id. Reject before accept.
        participant = deps.resolve_participant(ws.query_params.get("token", ""))
        if participant is None:
            await ws.close(code=4401)  # unauthorized
            return
        if not await deps.campaign_exists(campaign_id):
            await ws.close(code=4404)  # no such campaign
            return
        await ws.accept()
        queue = hub.subscribe(campaign_id)
        await arb.note_joined(campaign_id, participant)  # add to the arbiter's turn roster (OQ-7)
        await hub.publish(
            campaign_id, {"type": "participant_joined", "participant_id": participant}
        )
        # Fan hub messages out to THIS connection while we read intents from it.
        forward = asyncio.create_task(_forward(ws, queue))
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "intent":
                    await _run_and_broadcast(
                        deps, arb, hub, campaign_id, participant, str(msg.get("text", ""))
                    )
        except WebSocketDisconnect:
            pass
        finally:
            forward.cancel()
            hub.unsubscribe(campaign_id, queue)
            await arb.note_left(campaign_id, participant)  # drop from the turn roster (OQ-7)
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
    if decision != AdmitDecision.ADMITTED:  # REJECTED (or a reserved QUEUED) → no beat now
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
    except Exception as exc:
        # A beat failure (e.g. a ruleset mismatch, a provider error) must NOT crash the WS
        # connection — broadcast a graceful failure and keep the session alive (mirrors the CLI
        # play loop's "beat failed; nothing was saved"). Nothing was committed (pre-commit crash).
        await hub.publish(
            campaign_id,
            {
                "type": "beat_failed",
                "participant_id": participant,
                "intent": text,
                "error": str(exc),
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
