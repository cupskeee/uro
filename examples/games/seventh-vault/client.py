"""The WS crew client — Posture B. One `CrewClient` per crew member, speaking the play
channel's whole frame vocabulary (uro_server/app.py): intent out; beat_started /
narration_chunk / beat_committed / not_your_turn / intent_rejected / beat_failed /
participant_joined / participant_left / outcome_recorded in.

The client is transport + turn discipline ONLY. It holds no canonical game state — committed
truth is read back through the host's library projections (there is no other way; S4).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

_TIMEOUT = 30.0  # generous: a stub beat is fast, but CI machines stall


class CrewClient:
    def __init__(
        self, *, base: str, campaign_id: str, token: str, participant_id: str, role: str
    ) -> None:
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        self.uri = f"{ws_base}/campaigns/{campaign_id}/play?token={token}"
        self.token = token
        self.participant_id = participant_id
        self.role = role
        self.frames: list[dict[str, Any]] = []  # every frame this client ever received
        self._ws: websockets.ClientConnection | None = None
        self._rx: asyncio.Task[None] | None = None
        self._new_frame = asyncio.Event()
        self._commit_cursor = 0  # for next_commit()

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.uri)
        self._rx = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                self.frames.append(json.loads(raw))
                self._new_frame.set()
        except websockets.ConnectionClosed:
            pass
        finally:
            self._new_frame.set()  # wake any waiter so it can time out / see the closure

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._rx is not None:
            await self._rx

    async def send_intent(self, text: str) -> None:
        assert self._ws is not None, "connect() first"
        await self._ws.send(json.dumps({"type": "intent", "text": text}))

    # -- frame observation ---------------------------------------------------------------

    _ERROR_TYPES = ("beat_failed", "intent_rejected")

    def commits(self) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("type") == "beat_committed"]

    def _raise_on_error_frames(self) -> None:
        """A beat_failed/intent_rejected anywhere means the shared arc is broken — the beat every
        waiter is holding for will never commit. Surface the server's error immediately instead of
        burning the full timeout with the diagnosis buried in a frame-type list."""
        for f in self.frames:
            if f.get("type") in self._ERROR_TYPES:
                raise BeatFailedError(
                    f"{self.participant_id}: saw {f.get('type')} for "
                    f"{f.get('participant_id')} ({f.get('error') or f.get('text')!r})"
                )

    async def wait_for(
        self, frame_type: str, *, where: dict[str, Any] | None = None, timeout: float = _TIMEOUT
    ) -> dict[str, Any]:
        """Return the first not-yet-seen frame of `frame_type` matching `where` (field equality),
        waiting for new frames as needed. Raises BeatFailedError on a server error frame (unless
        that error frame is itself what the caller is waiting for, as stress/s6 does)."""
        seen = 0

        def scan() -> dict[str, Any] | None:
            nonlocal seen
            while seen < len(self.frames):
                f = self.frames[seen]
                seen += 1
                if f.get("type") != frame_type:
                    continue
                if where and any(f.get(k) != v for k, v in where.items()):
                    continue
                return f
            return None

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            if (hit := scan()) is not None:
                return hit
            if frame_type not in self._ERROR_TYPES:
                self._raise_on_error_frames()
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"{self.participant_id}: no {frame_type} matching {where} within {timeout}s;"
                    f" saw {[f.get('type') for f in self.frames]}"
                )
            self._new_frame.clear()
            try:
                await asyncio.wait_for(self._new_frame.wait(), timeout=remaining)
            except TimeoutError:
                continue  # loop once more; scan() will fail and raise with context

    async def next_commit(self, timeout: float = _TIMEOUT) -> dict[str, Any]:
        """The next beat_committed in arrival order (each client sees every beat — the shared
        scene). A cursor, not a filter: callers consume commits exactly once, in order."""
        target = self._commit_cursor + 1
        deadline = asyncio.get_event_loop().time() + timeout
        while len(self.commits()) < target:
            self._raise_on_error_frames()
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"{self.participant_id}: commit #{target} never arrived; "
                    f"have {len(self.commits())}"
                )
            self._new_frame.clear()
            try:
                await asyncio.wait_for(self._new_frame.wait(), timeout=remaining)
            except TimeoutError:
                continue
        self._commit_cursor = target
        return self.commits()[target - 1]


class BeatFailedError(RuntimeError):
    """The server reported a failed/rejected beat — the shared scene cannot advance."""
