"""D-97: the engine assumes no genre. A pack that is one place is a world; power is a faction or a
person of standing, not a role word; History seeding speaks the pack's own words, and a pack that
brings none can't be seeded. Halcyon Deep (a 2091 deep-sea station) is the non-fantasy check."""

import json
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


def test_ashfalls_seeded_history_is_unchanged() -> None:
    # recorded from the seeder before D-97 moved its words into the pack: byte-identical
    golden = json.loads(
        (Path(__file__).parent / "fixtures" / "ashfall_seed42_pre_d97.json").read_text()
    )
    # D-108 added an empty `provenance` to FactionCreated/ActorCreated (seeding records none):
    # everything else must still match the recording byte for byte.
    now = [
        {
            "type": e.event_type,
            "payload": {k: v for k, v in e.payload.items() if not (k == "provenance" and v == "")},
            "cause": e.caused_by.model_dump(),
        }
        for e in seed_history(parse_pack(WORLDS / "ashfall").manifest, Rng(42))
    ]
    assert json.loads(json.dumps(now, sort_keys=True)) == golden


def test_a_vocabulary_is_checked_and_speaks_the_packs_language(tmp_path: Path) -> None:
    pack = _copy("halcyon", tmp_path)
    toml = pack / "world.toml"
    text = toml.read_text()
    toml.write_text(text.replace('group_name = "{name} Holdings"', 'group_name = "Holdings"'))
    with pytest.raises(PackError, match="must contain"):
        parse_pack(pack)
    toml.write_text(text.replace('"Kuroshio", ', '"", '))
    with pytest.raises(PackError, match="blank"):
        parse_pack(pack)
    spanish = text.replace(
        'group_name = "{name} Holdings"',
        'group_name = "{name} Holdings"\nera_name = "la época de {era}"\n'
        'era_claim = "{era_name} quedó marcada por {calamity}."',
    )
    toml.write_text(spanish)
    events = seed_history(parse_pack(pack).manifest, Rng(42))
    claim = next(e.payload["statement"] for e in events if e.event_type == "ClaimRecorded")
    assert claim.startswith("La época de the first deep stations quedó marcada por ")


def test_simulated_years_need_a_vocabulary_to_count_as_a_past(tmp_path: Path) -> None:
    pack = _copy("halcyon", tmp_path)
    (pack / "lore" / "overview.md").write_text("A station.")  # too little lore to count
    history = next(d for d in check_sufficiency(parse_pack(pack)).dimensions if d.name == "history")
    assert history.ok  # simulate_years + a vocabulary: a past can be seeded
    toml = pack / "world.toml"
    toml.write_text(toml.read_text().split("[history.vocabulary]")[0])
    history = next(d for d in check_sufficiency(parse_pack(pack)).dimensions if d.name == "history")
    assert not history.ok and "vocabulary" in history.detail


def test_starting_gear_is_the_worlds() -> None:
    assert parse_pack(WORLDS / "halcyon").manifest.starting_gear == ["a station access card"]
    assert parse_pack(WORLDS / "ashfall").manifest.starting_gear == ["a traveler's knife"]
