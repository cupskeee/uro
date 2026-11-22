"""The character routes over REST (D-112): a campaign started with its own game and a built PC, a
refused spec located problem by problem, previews, a player reading and rebuilding only their own
character, and the freeze at the first beat. Against the real store, like test_rest_management."""

from collections.abc import AsyncIterator

import httpx
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import beat_resolved
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_server.app import create_app, engine_deps

_OP, _BOB = "tok-op", "tok-bob"


class _Stub:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "x"

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _client(store: PostgresEventStore) -> httpx.AsyncClient:
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()))
    deps = engine_deps(store, engine, {_OP: "alice", _BOB: "bob"}, admin_tokens={_OP})
    app = create_app(deps)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _q(path: str, tok: str = _OP) -> str:
    return f"{path}{'&' if '?' in path else '?'}token={tok}"


async def test_characters_over_rest(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Rest-{new_id()}")
    w = world.world_id
    async with _client(store) as client:
        # the ruleset listing publishes what a character spec may hold
        listed = (await client.get(_q("/rulesets"))).json()["rulesets"]
        pool = next(r for r in listed if r["id"] == "uro-pool")
        assert "traits" in pool["spec_schema"]["properties"]

        # preview under the game a new campaign would name: ok, then a located refusal
        r = await client.post(
            _q(f"/worlds/{w}/character-preview"),
            json={"spec": {"traits": {"might": 3}}, "ruleset": "uro-pool"},
        )
        assert r.status_code == 200 and r.json()["ok"] and r.json()["sheet"]["traits"]["might"] == 3
        r = await client.post(
            _q(f"/worlds/{w}/character-preview"),
            json={"spec": {"traits": {"luck": 1}}, "ruleset": "uro-pool"},
        )
        body = r.json()
        assert (
            r.status_code == 200
            and not body["ok"]
            and body["problems"][0]["loc"] == ["traits", "luck"]
        )

        # start with the campaign's own game; a bad spec is a 400 with problems, nothing made
        r = await client.post(
            _q(f"/worlds/{w}/campaigns"),
            json={
                "participant": "alice",
                "new_pc_name": "Ash",
                "ruleset": "uro-pool",
                "ruleset_config": {"trait_cap": 4},
                "spec": {"traits": {"might": 9}},
            },
        )
        assert r.status_code == 400
        assert r.json()["detail"]["problems"] == [
            {"loc": ["traits", "might"], "message": "at most 4 here"}
        ]
        r = await client.post(
            _q(f"/worlds/{w}/campaigns"),
            json={
                "participant": "alice",
                "new_pc_name": "Ash",
                "ruleset": "uro-pool",
                "ruleset_config": {"trait_cap": 4},
                "spec": {"traits": {"might": 4}},
            },
        )
        assert r.status_code == 200, r.text
        cid = r.json()["campaign_id"]
        shown = (await client.get(_q(f"/campaigns/{cid}"))).json()
        assert shown["ruleset_id"] == "uro-pool" and shown["ruleset_config"] == {"trait_cap": 4}

        # bob joins with his own build, previewed first under the campaign's game
        r = await client.post(
            _q(f"/campaigns/{cid}/character/preview", _BOB), json={"spec": {"traits": {"wits": 3}}}
        )
        assert r.json()["ok"] and r.json()["ruleset"] == "uro-pool"
        r = await client.post(
            _q(f"/campaigns/{cid}/join", _BOB),
            json={"participant": "bob", "new_pc_name": "Bryn", "spec": {"traits": {"wits": 3}}},
        )
        assert r.status_code == 200, r.text

        # each reads only their own character
        mine = (await client.get(_q(f"/campaigns/{cid}/character", _BOB))).json()
        assert (
            mine["name"] == "Bryn" and mine["sheet"]["traits"]["wits"] == 3 and mine["build_open"]
        )
        r = await client.get(_q(f"/campaigns/{cid}/character?participant=alice", _BOB))
        assert r.status_code == 403
        theirs = (await client.get(_q(f"/campaigns/{cid}/character?participant=bob"))).json()
        assert theirs["actor_id"] == mine["actor_id"]  # an operator may read anyone's

        # rebuild before the story starts; refused after the first beat, with why
        r = await client.patch(
            _q(f"/campaigns/{cid}/character", _BOB),
            json={"spec": {"traits": {"nerve": 3}}, "name": "Brynn"},
        )
        assert r.status_code == 200 and r.json()["name"] == "Brynn"
        r = await client.patch(
            _q(f"/campaigns/{cid}/character", _BOB), json={"participant": "alice", "name": "X"}
        )
        assert r.status_code == 403
        branch = shown["branch_id"]
        await store.append_beat(
            branch,
            [
                beat_resolved(
                    beat_id=new_id(), participant_id="bob", intent_text="go", narration="."
                )
            ],
        )
        r = await client.patch(_q(f"/campaigns/{cid}/character", _BOB), json={"name": "Late"})
        assert r.status_code == 409 and "story has started" in r.json()["detail"]
        assert not (await client.get(_q(f"/campaigns/{cid}/character", _BOB))).json()["build_open"]


async def test_a_character_in_play_says_how_its_ratings_read(store: PostgresEventStore) -> None:
    """D-119: a game that keeps a rating as a step says how it reads (`+3`, a word), on the
    player's own character, so a client shows the same thing its creation preview showed. A
    ruleset with nothing to add says nothing."""
    world = await store.create_world(f"Reads-{new_id()}")
    game = {
        "definition": {
            "scale": {"min": 0, "max": 2, "words": ["poor", "fair", "fine"]},
            "ratings": [{"key": "str", "label": "Strength"}, {"key": "wit", "label": "Wit"}],
        },
        "resolver": {"kind": "d20"},
    }
    async with _client(store) as client:
        preview = (
            await client.post(
                _q(f"/worlds/{world.world_id}/character-preview"),
                json={"spec": {"ratings": {"str": 2}}, "ruleset": "uro-composable",
                      "ruleset_config": game},
            )
        ).json()  # fmt: skip
        assert preview["ok"], preview
        started = await client.post(
            _q(f"/worlds/{world.world_id}/campaigns"),
            json={"participant": "alice", "new_pc_name": "Ada", "spec": {"ratings": {"str": 2}},
                  "ruleset": "uro-composable", "ruleset_config": game},
        )  # fmt: skip
        assert started.status_code == 200, started.text
        own = (await client.get(_q(f"/campaigns/{started.json()['campaign_id']}/character"))).json()
        reads = own["reads"]["ratings"]
        assert reads["str"]["step"] == own["sheet"]["ratings"]["str"] == 2
        assert reads["str"]["word"] == "fine" and reads["wit"]["word"] in ("poor", "fair")
        # the very reading the preview's guidance gave for the same step
        assert reads["str"]["reads"] == preview["guidance"]["ratings"]["str"]["reads"]

        other = await store.create_world(f"Plain-{new_id()}")
        plain = await client.post(
            _q(f"/worlds/{other.world_id}/campaigns"),
            json={"participant": "alice", "new_pc_name": "Bo"},
        )
        assert plain.status_code == 200, plain.text
        bo = (await client.get(_q(f"/campaigns/{plain.json()['campaign_id']}/character"))).json()
        assert bo["reads"] is None
