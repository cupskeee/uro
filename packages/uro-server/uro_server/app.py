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
from uro_core.session import AdmitDecision, SoloArbiter, TurnArbiter, VoteCoordinator

from uro_server.sessions import SessionHub, TokenRegistry


def _bearer_token(request: Request) -> str:
    """Extract a bearer token from the Authorization header, or ''."""
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


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
    # Runtime token management (docs/18 B10, D-39): mint/revoke durable session tokens + the admin
    # check, behind the same resolve_participant choke point. None → the token endpoints return 501.
    mint_token: Callable[[str, str], Awaitable[str]] | None = None  # (participant, campaign)→token
    revoke_token: Callable[[str], Awaitable[bool]] | None = None  # (plaintext token) → revoked?
    is_admin: Callable[[str], bool] | None = None  # (token) → operator tier (may act for others)?
    token_campaign: Callable[[str], str | None] | None = None  # minted token → its campaign (scope)
    hydrate_tokens: Callable[[], Awaitable[None]] | None = None  # load durable tokens at startup


def engine_deps(
    store: EngineStore,
    engine: Engine,
    tokens: dict[str, str],
    admin_tokens: set[str] | None = None,
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

    return ServerDeps(
        resolve_participant=registry.resolve,
        campaign_exists=campaign_exists,
        run_beat=run_beat,
        report_outcome=report_outcome,
        store=store,
        advance_time=advance_time,
        mint_token=registry.mint,
        revoke_token=registry.revoke,
        is_admin=registry.is_admin,
        token_campaign=registry.campaign_of,
        hydrate_tokens=registry.hydrate,
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

    @app.post("/worlds")
    async def create_world(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
        _auth(request)
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
        new_pc_name = body.get("new_pc_name")
        adopt_actor_id = body.get("adopt_actor_id")
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
                seed=int(body.get("seed") or 0),  # docs/18 G-3: reproducible-combat seed
            )
        except (ValueError, KeyError) as exc:  # bad PC choice, or world pins an absent ruleset
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
        try:
            limit = int(request.query_params.get("limit") or 20)
        except ValueError as exc:  # ?limit=abc is bad input → 400, not a 500
            raise HTTPException(status_code=400, detail="limit must be an integer") from exc
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
        if days <= 0:  # store.time_skip rejects this deeper (a 500); catch it here as bad input
            raise HTTPException(status_code=400, detail="days must be a positive integer")
        return await deps.advance_time(campaign_id, days)

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
