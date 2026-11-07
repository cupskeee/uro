"""D-105: a pack read without importing it, and the world-rules grammar as data (Uro Loom's
requests 1 and 2). Neither writes anything, so no store is needed."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from starlette.testclient import TestClient
from uro_core.worldpack.parse import parse_pack
from uro_core.worldpack.rules import RULES_API_VERSION
from uro_server.app import ServerDeps, create_app

_WORLDS = Path(__file__).resolve().parents[3] / "worlds"


def _client() -> TestClient:
    async def campaign_exists(campaign_id: str) -> bool:
        return False

    async def run_beat(campaign_id: str, participant: str, text: str):  # type: ignore[no-untyped-def]
        yield ""

    deps = ServerDeps(
        resolve_participant=lambda token: {"tok": "player-1"}.get(token),
        campaign_exists=campaign_exists,
        run_beat=run_beat,
    )
    return TestClient(create_app(deps))


def _zip(src: Path, *, patch: tuple[str, bytes, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                data = f.read_bytes()
                if patch and f.name == patch[0]:
                    data = data.replace(patch[1], patch[2])
                zf.writestr(f.relative_to(src).as_posix(), data)
    return buf.getvalue()


def test_a_pack_reads_back_as_uro_reads_it_without_being_imported() -> None:
    upload = {"pack": ("ashfall.zip", _zip(_WORLDS / "ashfall"), "application/zip")}
    got = _client().post("/worlds/parse?token=tok", files=upload)
    assert got.status_code == 200, got.text
    expected = parse_pack(_WORLDS / "ashfall").model_dump(mode="json")
    assert got.json() == expected
    # defaults the files never wrote are filled in, as Uro would read them
    assert got.json()["manifest"]["clock"] == expected["manifest"]["clock"]
    assert got.json()["rule_pack"]["rules"]


def test_parsing_a_bad_pack_is_a_400_and_needs_a_token() -> None:
    bad = _zip(_WORLDS / "thornwood", patch=("world.toml", b'name = "Thornwood"', b"name = 3"))
    client = _client()
    got = client.post("/worlds/parse?token=tok", files={"pack": ("t.zip", bad, "x/zip")})
    assert got.status_code == 400 and "invalid pack" in got.text
    nobody = client.post("/worlds/parse", files={"pack": ("t.zip", bad, "x/zip")})
    assert nobody.status_code == 401


def test_the_rules_grammar_is_served_as_json_schema() -> None:
    got = _client().get("/rules/schema?token=tok").json()
    assert got["rules_api_version"] == RULES_API_VERSION
    assert RULES_API_VERSION in got["supported_versions"]
    schema = got["rule_pack_schema"]
    assert set(schema["required"]) == {"rules_api_version"}
    assert {"rules", "agendas"} <= set(schema["properties"])


def test_a_rule_pack_is_checked_on_its_own_as_a_world_would_check_it() -> None:
    client = _client()
    good = parse_pack(_WORLDS / "ashfall").rule_pack
    assert good is not None
    ok = client.post("/rules/validate?token=tok", json=good.model_dump(mode="json")).json()
    assert ok == {
        "ok": True,
        "rules": len(good.rules),
        "agendas": len(good.agendas),
        "problems": [],
    }
    # an unknown trigger is located, not a 500
    broken = good.model_dump(mode="json")
    broken["rules"][0]["trigger"]["event"] = "NoSuchEvent"
    bad = client.post("/rules/validate?token=tok", json=broken).json()
    assert bad["ok"] is False and bad["problems"]
    assert bad["problems"][0]["loc"][:2] == ["rules", 0]
    # an id used twice: a world refuses it too
    twice = good.model_dump(mode="json")
    twice["rules"].append(twice["rules"][0])
    dupe = client.post("/rules/validate?token=tok", json=twice).json()
    assert dupe["ok"] is False and "used twice" in dupe["problems"][0]["message"]
    # a version this engine doesn't read, and something that isn't a pack at all
    old = client.post("/rules/validate?token=tok", json={"rules_api_version": 99}).json()
    assert old["ok"] is False
    assert client.post("/rules/validate?token=tok", json=[1, 2]).json()["ok"] is False
