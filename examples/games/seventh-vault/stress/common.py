"""Shared rig for the stress battery: a fresh heist world + server + N connected crew clients,
and an evidence sink (everything printed is also written to out/stress/<name>.txt so the GAP
REPORT can cite it)."""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GAME_DIR))

import host  # noqa: E402
from client import CrewClient  # noqa: E402
from world import CREW  # noqa: E402


@dataclass
class Evidence:
    name: str
    lines: list[str] = field(default_factory=list)

    def log(self, line: str) -> None:
        print(f"[{self.name}] {line}")
        self.lines.append(line)

    def flush(self) -> None:
        out = GAME_DIR / "out" / "stress"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{self.name}.txt").write_text("\n".join(self.lines) + "\n")
        print(f"[{self.name}] evidence -> out/stress/{self.name}.txt")


@dataclass
class Rig:
    store: object
    hw: host.HeistWorld
    server: host.ServerHandle
    clients: list[CrewClient]

    async def close(self) -> None:
        for c in self.clients:
            with contextlib.suppress(Exception):  # one bad socket must not skip server/store
                await c.close()
        self.server.stop()
        await self.store.close()  # type: ignore[attr-defined]


async def rig_up(name: str, *, n_clients: int, tokens: list[str] | None = None) -> Rig:
    """A fresh heist world + `uro serve` + the first n_clients crew members connected in CREW
    order (ring order == CREW order). `tokens` overrides the server token list if a probe needs
    a nonstandard set."""
    store = await host.connect_store()
    hw = await host.build_world(store, server_port=host.free_port(), run_tag=f"stress-{name}")
    server = host.start_server(
        port=hw.manifest["server"]["port"], tokens=tokens or [c[1] for c in CREW]
    )
    clients: list[CrewClient] = []
    for seat in hw.manifest["crew"][:n_clients]:
        c = CrewClient(
            base=server.base,
            campaign_id=hw.campaign.campaign_id,
            token=seat["token"],
            participant_id=seat["participant_id"],
            role=seat["role"],
        )
        await c.connect()
        await c.wait_for("participant_joined", where={"participant_id": seat["participant_id"]})
        clients.append(c)
    return Rig(store, hw, server, clients)
