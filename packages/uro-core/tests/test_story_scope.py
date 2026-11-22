"""D-117: a branch's state as its told story has shown it.

`story_scope` narrows the scene sections to what the saved beats named or made, so a player's
client can draw its panels from one read with a player's token, without holding the whole world
and hiding most of it. Pure over the branch's beats and rows: a fork tells the same story."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.characters import start_campaign
from uro_core.domain.events import (
    actor_created,
    faction_created,
    place_created,
    thread_created,
)
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.timeline.beats import BeatCreated, BeatRecord
from uro_core.timeline.story import story_scope, story_state
from uro_server.app import create_app, engine_deps


def _record(narration: str, created: list[tuple[str, str, str]] | None = None) -> BeatRecord:
    return BeatRecord.model_validate(
        {
            "beat": {
                "beat_id": new_id(),
                "participant_id": "p1",
                "intent_text": "I ask about Lord Vane",  # the player's own words show nothing
                "narration": narration,
            },
            "commit_id": new_id(),
            "created": [BeatCreated(kind=k, id=i, name=n) for k, i, n in created or []],
        }
    )


def _ids(rows: list[dict[str, Any]], key: str) -> list[str]:
    return [r[key] for r in rows]


def test_the_story_shows_what_its_beats_named_or_made() -> None:
    state: dict[str, list[dict[str, Any]]] = {
        "actors": [
            {"actor_id": "a:pc", "name": "Ada", "aliases": []},
            {"actor_id": "a:mera", "name": "Mera", "aliases": ["the barkeep"]},
            {"actor_id": "a:odessa", "name": "Odessa Marr", "aliases": []},
            {"actor_id": "a:vane", "name": "Lord Vane", "aliases": []},
            {"actor_id": "a:ann", "name": "Ann", "aliases": []},  # only inside another word
            {"actor_id": "a:made", "name": "A Stranger", "aliases": []},  # a beat made them
            {"actor_id": "a:blank", "name": "", "aliases": [""]},
        ],
        "places": [
            {"place_id": "p:bell", "name": "Hollow Bell"},
            {"place_id": "p:keep", "name": "Vane Keep"},
        ],
        "factions": [
            {"faction_id": "f:wardens", "name": "Ash Wardens"},
            {"faction_id": "f:court", "name": "The Sable Court"},
        ],
        "threads": [
            {"thread_id": "t:1", "state": "active"},
            {"thread_id": "t:2", "state": "dormant"},
            {"thread_id": "t:3", "state": "resolved"},
            {"thread_id": "t:4", "state": "offered"},
        ],
        "claims": [{"claim_id": "c:1"}],
    }
    records = [
        _record("In HOLLOW BELL, the barkeep wipes a cup. Hannah and the planners wait."),
        _record("Odessa Marr's rider serves the ash wardens.", [("actor", "a:made", "A Stranger")]),
    ]
    shown = story_scope(state, records, {"a:pc"})
    assert _ids(shown["actors"], "actor_id") == ["a:pc", "a:mera", "a:odessa", "a:made"]
    assert _ids(shown["places"], "place_id") == ["p:bell"]
    assert _ids(shown["factions"], "faction_id") == ["f:wardens"]
    assert _ids(shown["threads"], "thread_id") == ["t:1", "t:3", "t:4"]
    assert shown["claims"] == state["claims"]  # other sections pass through
    assert shown["actors"][1] == state["actors"][1]  # rows keep their shape
    # before any beat, only who is at the table and the threads in play
    start = story_scope(state, [], {"a:pc"})
    assert _ids(start["actors"], "actor_id") == ["a:pc"]
    assert start["places"] == [] and start["factions"] == []
    assert story_scope({"threads": state["threads"]}, [], set()).keys() == {"threads"}


class _Narrator:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "Mera looks up from the bar at Hollow Bell."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def test_a_players_token_reads_the_story_and_a_fork_tells_it_the_same(
    store: PostgresEventStore,
) -> None:
    world = await store.create_world(f"Story-{new_id()}")
    c = await start_campaign(
        store, world.world_id, world.main_branch_id, participant_id="alice", new_pc_name="Ada"
    )
    await store.append_beat(
        c.branch_id,
        [
            actor_created(actor_id="a:mera", name="Mera", role="barkeep"),
            actor_created(actor_id="a:vane", name="Lord Vane", tier=3),
            place_created(place_id="p:bell", name="Hollow Bell"),
            place_created(place_id="p:keep", name="Vane Keep"),
            faction_created(faction_id="f:court", name="The Sable Court"),
            thread_created(thread_id="t:kilns", stakes="Who keeps the kilns lit", state="active"),
            thread_created(thread_id="t:regent", stakes="The Regent's return"),  # dormant
        ],
    )
    sections = ["actors", "places", "factions", "threads", "pcs"]
    pc = await store.pc_for_participant(c.campaign_id, "alice")
    before = await story_state(store, c.branch_id, sections)
    assert _ids(before["actors"], "actor_id") == [pc]
    assert before["places"] == [] and len(before["pcs"]) == 1
    assert _ids(before["threads"], "thread_id") == ["t:kilns"]

    engine = Engine(store, ProviderRouter(bindings={}, default=_Narrator()))
    await engine.run_beat(c, "alice", "I ask who knows Lord Vane")
    app = create_app(engine_deps(store, engine, {"op": "gm", "pleb": "alice"}, admin_tokens={"op"}))
    url = f"/campaigns/{c.campaign_id}/state?sections={','.join(sections)}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as h:
        whole = (await h.get(f"{url}&token=pleb")).json()
        assert whole["scope"] == "world"
        assert {"a:mera", "a:vane"} <= set(_ids(whole["state"]["actors"], "actor_id"))
        told = (await h.get(f"{url}&scope=story&token=pleb")).json()
        assert told["scope"] == "story"
        assert sorted(_ids(told["state"]["actors"], "actor_id")) == sorted([pc, "a:mera"])
        assert _ids(told["state"]["places"], "place_id") == ["p:bell"]
        assert told["state"]["factions"] == []
        assert _ids(told["state"]["threads"], "thread_id") == ["t:kilns"]
        assert len(told["state"]["pcs"]) == 1
        # a player's rows are still the player's: no danger word, no build
        assert all("threat" not in a and "spec" not in a for a in told["state"]["actors"])
        # the operator sections stay the operator's in either scope, and a typo is a 400
        assert (await h.get(f"{url},claims&scope=story&token=pleb")).status_code == 403
        assert (await h.get(f"{url}&scope=mine&token=pleb")).status_code == 400
        assert (await h.get(f"{url},nope&scope=story&token=op")).status_code == 400

    head = (await store.get_branch(c.branch_id)).head_commit  # type: ignore[union-attr]
    fork = await store.fork_branch(c.world_id, head, f"what-if-{new_id()}")  # type: ignore[arg-type]
    forked = await story_state(store, fork.branch_id, sections)
    for section, key in (("actors", "actor_id"), ("places", "place_id"), ("threads", "thread_id")):
        assert _ids(forked[section], key) == _ids(told["state"][section], key)
