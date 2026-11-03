"""D-97: the engine assumes no genre. A pack that is one place is a world; power is a faction or a
person of standing, not a role word; History seeding speaks the pack's own words, and a pack that
brings none can't be seeded. Halcyon Deep (a 2091 deep-sea station) is the non-fantasy check."""

import shutil
from pathlib import Path

import pytest
from uro_core.engines.history import seed_history
from uro_core.errors import PackError
from uro_core.rulesets.rng import Rng
from uro_core.worldpack.parse import parse_pack
from uro_core.worldpack.sufficiency import check_sufficiency

WORLDS = Path(__file__).resolve().parents[3] / "worlds"
FANTASY = ("house ", "king", "queen", "duke", "lord", "dynasty", "warden", "reeve")


def _copy(name: str, tmp_path: Path) -> Path:
    dst = tmp_path / name
    shutil.copytree(WORLDS / name, dst)
    return dst


def test_a_world_of_one_place_is_somewhere_to_be(tmp_path: Path) -> None:
    pack = _copy("halcyon", tmp_path)
    places = pack / "entities" / "places.yaml"
    station = places.read_text().split('- id: "p:halcyon"')[1].split("- id:")[0]
    places.write_text('- id: "p:halcyon"' + station.replace('  parent: "p:kaiko"\n', ""))
    report = check_sufficiency(parse_pack(pack))
    geography = next(d for d in report.dimensions if d.name == "geography")
    assert geography.ok and report.grade == "runnable"  # one settlement, no region or site


def test_power_is_a_faction_or_someone_of_standing_not_a_role_word(tmp_path: Path) -> None:
    pack = _copy("halcyon", tmp_path)
    (pack / "entities" / "factions.yaml").write_text("[]\n")
    actors = pack / "entities" / "actors.yaml"
    text = (
        actors.read_text()
        .replace('  faction: "f:tethys"\n', "")
        .replace('  faction: "f:union"\n', "")
    )
    actors.write_text(text)
    power = next(d for d in check_sufficiency(parse_pack(pack)).dimensions if d.name == "power")
    assert power.ok and "2 people of standing" in power.detail  # the director and Dr. Brandt
    actors.write_text(text.replace("tier: 2", "tier: 1"))  # "chief scientist" is not a ruler
    power = next(d for d in check_sufficiency(parse_pack(pack)).dimensions if d.name == "power")
    assert not power.ok


def test_seeding_speaks_the_packs_words(tmp_path: Path) -> None:
    halcyon = parse_pack(WORLDS / "halcyon")
    names = [
        e.payload.get("name", "") or e.payload.get("statement", "")
        for e in seed_history(halcyon.manifest, Rng(42))
        if e.event_type in ("FactionCreated", "ActorCreated", "ClaimRecorded")
    ]
    assert any(n.endswith("Holdings") for n in names)
    assert not any(w in n.lower() for n in names for w in FANTASY)
    for world in ("ashfall", "thornwood", "emberfell", "brinehold"):  # every shipped pack seeds
        assert seed_history(parse_pack(WORLDS / world).manifest, Rng(7))


def test_a_pack_without_a_vocabulary_cannot_be_seeded(tmp_path: Path) -> None:
    pack = _copy("halcyon", tmp_path)
    toml = pack / "world.toml"
    toml.write_text(toml.read_text().split("[history.vocabulary]")[0])
    manifest = parse_pack(pack).manifest
    with pytest.raises(PackError, match="no vocabulary"):
        seed_history(manifest, Rng(42))


def test_halcyon_is_runnable() -> None:
    report = check_sufficiency(parse_pack(WORLDS / "halcyon"))
    assert report.grade == "runnable", report.gaps
